"""Explicit, checksum-verified transactional PostgreSQL migrations."""

from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
import re

from ..errors import SessionStorageError, SessionValidationError
from ._postgres_driver import PostgresPool, PostgresSettings, postgres_transaction


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str

    def __post_init__(self) -> None:
        if (
            type(self.version) is not int
            or self.version < 1
            or not re.fullmatch(r"\d{3}_[a-z0-9_]+\.sql", self.name)
            or int(self.name[:3]) != self.version
            or not self.sql.strip()
        ):
            raise SessionValidationError("invalid PostgreSQL migration definition")

    @property
    def checksum(self) -> str:
        return sha256(self.sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def load_migrations() -> tuple[Migration, ...]:
    root = files("pi_agent_py.storage").joinpath("migrations")
    return tuple(
        Migration(
            int(resource.name[:3]), resource.name, resource.read_text(encoding="utf-8")
        )
        for resource in sorted(root.iterdir(), key=lambda item: item.name)
        if resource.name.endswith(".sql")
    )


async def apply_migrations(
    pool: PostgresPool,
    migrations: tuple[Migration, ...] | None = None,
    *,
    settings: PostgresSettings = PostgresSettings(),
) -> tuple[int, ...]:
    """Trusted application setup only; no model-supplied SQL. Borrow the pool.

    One DB-wide advisory transaction lock serializes first-time creation too.
    A failed statement rolls back all DDL and version markers in this invocation.
    """
    definitions = load_migrations() if migrations is None else migrations
    if not definitions or tuple(m.version for m in definitions) != tuple(
        range(1, len(definitions) + 1)
    ):
        raise SessionValidationError(
            "migration versions must be contiguous starting at 1"
        )
    async with postgres_transaction(pool, settings) as connection:
        version = await connection.fetchrow(
            "SELECT current_setting('server_version_num')::integer AS version"
        )
        if (
            version is None
            or not isinstance(version["version"], int)
            or version["version"] < 150000
        ):
            raise SessionStorageError("PostgreSQL 15 or newer is required")
        await connection.execute("SELECT pg_advisory_xact_lock(22598583985196628)")
        await connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        recorded = await connection.fetch(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        )
        known = {m.version: m for m in definitions}
        for index, row in enumerate(recorded, 1):
            number = row["version"]
            if not isinstance(number, int) or number not in known:
                raise SessionStorageError("unknown PostgreSQL migration version")
            if number != index:
                raise SessionStorageError("PostgreSQL migration history has a gap")
            if (
                row["name"] != known[number].name
                or row["checksum"] != known[number].checksum
            ):
                raise SessionStorageError("PostgreSQL migration name/checksum mismatch")
        applied = []
        for migration in definitions[len(recorded) :]:
            await connection.execute(migration.sql)
            await connection.execute(
                "INSERT INTO schema_migrations(version,name,checksum) VALUES($1,$2,$3)",
                migration.version,
                migration.name,
                migration.checksum,
            )
            applied.append(migration.version)
        return tuple(applied)
