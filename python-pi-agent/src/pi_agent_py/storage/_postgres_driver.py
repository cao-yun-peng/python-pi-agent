"""Typed asyncpg-compatible borrowed pool boundary; driver import is optional."""

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from math import isfinite
from typing import Protocol

from ..errors import (
    AgentError,
    SessionConflictError,
    SessionCommitUnknownError,
    SessionStorageError,
    SessionValidationError,
)


class PostgresConnection(Protocol):
    def transaction(
        self, *, isolation: str = "read_committed", readonly: bool = False
    ) -> AbstractAsyncContextManager[object]: ...
    async def execute(self, query: str, *args: object) -> str: ...
    async def executemany(
        self, query: str, args: Sequence[tuple[object, ...]]
    ) -> None: ...
    async def fetch(
        self, query: str, *args: object
    ) -> Sequence[Mapping[str, object]]: ...
    async def fetchrow(
        self, query: str, *args: object
    ) -> Mapping[str, object] | None: ...


class PostgresPool(Protocol):
    def acquire(
        self, *, timeout: float
    ) -> AbstractAsyncContextManager[PostgresConnection]: ...


@dataclass(frozen=True, slots=True)
class PostgresSettings:
    acquire_timeout: float = 30.0
    lock_timeout: float = 5.0
    statement_timeout: float = 30.0

    def __post_init__(self) -> None:
        for value in (self.acquire_timeout, self.lock_timeout, self.statement_timeout):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value <= 0
            ):
                raise SessionValidationError(
                    "PostgreSQL timeouts must be finite and positive"
                )


@asynccontextmanager
async def postgres_transaction(
    pool: PostgresPool,
    settings: PostgresSettings,
    *,
    isolation: str = "read_committed",
    readonly: bool = False,
    commit_identity: tuple[str, str] | None = None,
) -> AsyncIterator[PostgresConnection]:
    """Translate after rollback/release; never retry I/O or suppress cancellation."""
    body_completed = False
    try:
        async with pool.acquire(timeout=settings.acquire_timeout) as connection:
            async with connection.transaction(isolation=isolation, readonly=readonly):
                await connection.execute(
                    "SELECT set_config('lock_timeout', $1, true), set_config('statement_timeout', $2, true)",
                    f"{max(1, int(settings.lock_timeout * 1000))}ms",
                    f"{max(1, int(settings.statement_timeout * 1000))}ms",
                )
                yield connection
                body_completed = True
    except AgentError:
        raise
    except Exception as error:
        state = getattr(error, "sqlstate", None)
        constraint = getattr(error, "constraint_name", None)
        if (
            body_completed
            and commit_identity is not None
            and (
                state is None
                or (isinstance(state, str) and state.startswith("08"))
                or state in {"57P01", "57P02", "57P03"}
            )
        ):
            raise SessionCommitUnknownError(*commit_identity) from error
        if state == "23505" and constraint in {
            "sessions_pkey",
            "session_events_pkey",
            "session_events_event_id_key",
        }:
            raise SessionConflictError(
                "PostgreSQL session uniqueness conflict"
            ) from error
        if state in {"22P02", "22P05", "22021", "22003"}:
            raise SessionValidationError(
                "session value cannot be represented by PostgreSQL"
            ) from error
        raise SessionStorageError(
            "PostgreSQL session operation failed; do not blindly retry an uncertain commit"
        ) from error
