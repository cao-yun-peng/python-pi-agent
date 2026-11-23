"""Explicit migration command; optional driver and DSN resolved only at entry."""

import argparse
import asyncio
import importlib
import os
from typing import Protocol, cast

from ..errors import AgentError, ConfigurationError, SessionStorageError
from ._postgres_driver import PostgresPool
from .postgres_migrations import apply_migrations


class _OwnedPool(PostgresPool, Protocol):
    async def close(self) -> None: ...


async def migrate(dsn_env: str) -> tuple[int, ...]:
    dsn = os.environ.get(dsn_env)
    if not dsn:
        raise ConfigurationError(f"set {dsn_env} to the PostgreSQL DSN")
    try:
        driver = importlib.import_module("asyncpg")
    except ImportError as error:
        raise ConfigurationError(
            "install pi-agent-py[postgres] to run migrations"
        ) from error
    try:
        pool = cast(
            _OwnedPool,
            await driver.create_pool(
                dsn, min_size=1, max_size=2, timeout=30, command_timeout=30
            ),
        )
    except Exception as error:
        raise SessionStorageError("unable to open PostgreSQL migration pool") from error
    try:
        return await apply_migrations(pool)
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("migrate",))
    parser.add_argument("--dsn-env", default="PI_AGENT_POSTGRES_DSN")
    args = parser.parse_args()
    try:
        applied = asyncio.run(migrate(args.dsn_env))
    except AgentError as error:
        parser.exit(1, f"{type(error).__name__}: {error}\n")
    print(f"applied_migrations={','.join(map(str, applied)) or 'none'}")


if __name__ == "__main__":
    main()
