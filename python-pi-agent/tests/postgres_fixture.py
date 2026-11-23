"""Isolated PostgreSQL fixture shared by storage and execution tests."""

import asyncio
import importlib
import os
from uuid import uuid4
from pi_agent_py.storage.postgres import PostgresSessionStore

_DSN = os.environ.get("PI_AGENT_POSTGRES_TEST_DSN")


class PostgresFixture:
    async def asyncSetUp(self):
        self.driver = importlib.import_module("asyncpg")
        self.admin = await self.driver.connect(_DSN)
        self.schema = "pi_test_" + uuid4().hex
        await self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.pools = []
        self.pool = await self.new_pool()
        self.store = PostgresSessionStore(self.pool)
        if getattr(self, "legacy_schema", False):
            from pi_agent_py.storage.postgres_migrations import (
                apply_migrations,
                load_migrations,
            )

            await apply_migrations(self.pool, load_migrations()[:1])
        else:
            await self.store.ensure_schema()

    async def new_pool(self):
        pool = await self.driver.create_pool(
            _DSN,
            min_size=1,
            max_size=12,
            server_settings={
                "search_path": self.schema,
                "application_name": self.schema,
            },
        )
        self.pools.append(pool)
        return pool

    async def asyncTearDown(self):
        for pool in self.pools:
            await asyncio.wait_for(pool.close(), timeout=10)
        # schema is a generated fixture identifier in the dedicated test database.
        await self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.admin.close()
