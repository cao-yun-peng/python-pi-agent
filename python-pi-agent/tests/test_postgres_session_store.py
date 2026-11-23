"""Offline adapter errors plus explicitly enabled real PostgreSQL contracts."""

import asyncio
import importlib
import os
import unittest
from contextlib import asynccontextmanager
from uuid import uuid4

from pi_agent_py.domain import Message, QueuedControl, ControlClaim, ControlDelivery
from pi_agent_py.errors import (
    SessionConflictError,
    SessionReplayError,
    SessionStorageError,
    SessionValidationError,
)
from pi_agent_py.storage.postgres import PostgresSessionStore
from pi_agent_py.storage._postgres_driver import PostgresSettings
from pi_agent_py.storage.postgres_migrations import (
    Migration,
    apply_migrations,
    load_migrations,
)
from session_store_contract import SessionStoreContract


_DSN = os.environ.get("PI_AGENT_POSTGRES_TEST_DSN")


class DriverFailure(Exception):
    def __init__(self, state=None, constraint=None):
        self.sqlstate = state
        self.constraint_name = constraint


class FailingPool:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    @asynccontextmanager
    async def acquire(self, *, timeout):
        self.calls += 1
        raise self.error
        yield  # pragma: no cover


class PostgresBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_driver_error_mapping_and_cancellation(self):
        for error, expected in (
            (TimeoutError(), SessionStorageError),
            (OSError(), SessionStorageError),
            (DriverFailure("23505", "sessions_pkey"), SessionConflictError),
            (DriverFailure("23505", "session_events_pkey"), SessionConflictError),
            (DriverFailure("23505", "unexpected_constraint"), SessionStorageError),
            (DriverFailure("22P02"), SessionValidationError),
            (DriverFailure("22P05"), SessionValidationError),
            (asyncio.CancelledError(), asyncio.CancelledError),
        ):
            with self.subTest(error=type(error).__name__):
                store = PostgresSessionStore(FailingPool(error))
                with self.assertRaises(expected) as caught:
                    await store.load("s")
                if not isinstance(error, asyncio.CancelledError):
                    self.assertIs(caught.exception.__cause__, error)

    async def test_constructor_borrows_without_acquiring_or_migrating(self):
        pool = FailingPool(RuntimeError("must not acquire"))
        PostgresSessionStore(pool)
        self.assertEqual(pool.calls, 0)
        for timeout in (0, -1, float("nan"), float("inf"), True):
            with (
                self.subTest(timeout=timeout),
                self.assertRaises(SessionValidationError),
            ):
                PostgresSettings(lock_timeout=timeout)

    async def test_migration_definitions_are_validated_before_pool_use(self):
        pool = FailingPool(RuntimeError("must not acquire"))
        with self.assertRaises(SessionValidationError):
            await apply_migrations(pool, (Migration(2, "002_gap.sql", "SELECT 1"),))
        self.assertEqual(pool.calls, 0)
        migration = load_migrations()[0]
        self.assertEqual(
            migration.checksum,
            Migration(1, migration.name, migration.sql.replace("\n", "\r\n")).checksum,
        )


@unittest.skipUnless(
    _DSN, "set PI_AGENT_POSTGRES_TEST_DSN for isolated PostgreSQL integration"
)
class PostgresSessionStoreTests(SessionStoreContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driver = importlib.import_module("asyncpg")
        self.admin = await self.driver.connect(_DSN)
        self.schema = "pi_test_" + uuid4().hex
        await self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.pools = []
        self.pool = await self.new_pool()
        self.store = PostgresSessionStore(self.pool)
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

    async def make_store(self):
        return self.store

    async def test_reopens_from_independent_pool_and_runtime_uses_store(self):
        from pi_agent_py import (
            AgentComponents,
            AgentSession,
            FakeProvider,
            Finish,
            TextDelta,
        )

        session = AgentSession(
            "s",
            self.store,
            AgentComponents(FakeProvider([[TextDelta("reply"), Finish()]])),
        )
        result = await session.run("hello")
        reopened = PostgresSessionStore(await self.new_pool())
        self.assertEqual((await reopened.load("s")).messages, result.messages)
        await session.close()

    async def test_ten_independent_writers_only_one_same_version_succeeds(self):
        other = PostgresSessionStore(await self.new_pool())
        results = await asyncio.gather(
            *(
                (self.store if i % 2 else other).append(
                    "race", 0, (Message("user", str(i)),)
                )
                for i in range(10)
            ),
            return_exceptions=True,
        )
        self.assertEqual(sum(not isinstance(r, BaseException) for r in results), 1)
        self.assertEqual(sum(isinstance(r, SessionConflictError) for r in results), 9)
        self.assertEqual((await self.store.load("race")).version, 1)

    async def test_mid_batch_driver_failure_rolls_back_new_session_and_events(self):
        await self.pool.execute(
            "CREATE FUNCTION reject_second() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.sequence = 2 THEN RAISE EXCEPTION 'injected'; END IF; RETURN NEW; END $$"
        )
        await self.pool.execute(
            "CREATE TRIGGER fail_batch BEFORE INSERT ON session_events FOR EACH ROW EXECUTE FUNCTION reject_second()"
        )
        with self.assertRaises(SessionStorageError) as caught:
            await self.store.append(
                "s", 0, (Message("user", "a"), Message("assistant", "b"))
            )
        self.assertIsNotNone(caught.exception.__cause__)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM sessions"), 0)
        self.assertEqual(
            await self.pool.fetchval("SELECT count(*) FROM session_events"), 0
        )

    async def test_projection_failure_rolls_back_delivery(self):
        await self.store.record_control(
            "s", 0, "control_enqueued", QueuedControl("q", "follow_up", "next", "r")
        )
        await self.store.record_control(
            "s", 1, "control_claimed", ControlClaim("q", "c", "w", 10)
        )
        await self.pool.execute("DELETE FROM session_controls")
        with self.assertRaises(SessionReplayError):
            await self.store.record_control(
                "s",
                2,
                "control_delivered",
                ControlDelivery("q", "c", Message("user", "next")),
            )
        self.assertEqual((await self.store.load("s")).version, 2)

    async def test_control_id_reuse_after_terminal_matches_replay(self):
        from pi_agent_py.domain import ControlCancellation

        await self.store.record_control(
            "s", 0, "control_enqueued", QueuedControl("q", "follow_up", "one", "r1")
        )
        await self.store.record_control(
            "s", 1, "control_cancelled", ControlCancellation("q", "cancel")
        )
        await self.store.record_control(
            "s", 2, "control_enqueued", QueuedControl("q", "follow_up", "two", "r2")
        )
        self.assertEqual(
            await self.pool.fetchval(
                "SELECT prompt FROM session_controls WHERE control_id='q'"
            ),
            "two",
        )

    async def test_fork_rebuilds_terminal_and_pending_projections(self):
        from pi_agent_py.domain import ControlCancellation

        await self.store.record_control(
            "s", 0, "control_enqueued", QueuedControl("q", "follow_up", "next", "r")
        )
        await self.store.record_control(
            "s",
            1,
            "control_enqueued",
            QueuedControl("done", "follow_up", "finished", "r2"),
        )
        await self.store.record_control(
            "s", 2, "control_cancelled", ControlCancellation("done", "finished")
        )
        await self.store.fork("s", "branch")
        self.assertEqual(
            await self.pool.fetchval(
                "SELECT count(*) FROM session_controls WHERE control_id='q'"
            ),
            2,
        )
        self.assertEqual(
            await self.pool.fetchval(
                "SELECT state FROM session_controls WHERE session_id='branch' AND control_id='done'"
            ),
            "cancelled",
        )

    async def test_concurrent_forks_do_not_overwrite_target(self):
        await self.store.append("s", 0, (Message("user", "original"),))
        other = PostgresSessionStore(await self.new_pool())
        results = await asyncio.gather(
            self.store.fork("s", "branch"),
            other.fork("s", "branch"),
            return_exceptions=True,
        )
        self.assertEqual(
            sum(isinstance(result, SessionConflictError) for result in results), 1
        )
        self.assertEqual((await self.store.load("branch")).version, 1)

    async def test_real_event_unique_violation_maps_conflict_and_rolls_back(self):
        import json

        await self.store.append("s", 0, (Message("user", "existing"),))
        # Occupy an event identity while retaining the database UNIQUE index.
        await self.pool.execute(
            "UPDATE session_events SET payload=jsonb_set(payload,'{event_id}',$1::jsonb) WHERE session_id='s'",
            json.dumps("target:1"),
        )
        with self.assertRaises(SessionConflictError):
            await self.store.append("target", 0, (Message("user", "loser"),))
        self.assertEqual(
            await self.pool.fetchval(
                "SELECT count(*) FROM sessions WHERE session_id='target'"
            ),
            0,
        )

    async def test_distributed_marker_rejects_unprotected_writes(self):
        await self.store.append("s", 0, (Message("user", "original"),))
        await self.pool.execute(
            "UPDATE sessions SET execution_mode='distributed' WHERE session_id='s'"
        )
        with self.assertRaises(SessionConflictError):
            await self.store.append("s", 1, (Message("assistant", "unsafe"),))
        self.assertEqual((await self.store.load("s")).version, 1)

    async def test_metadata_corruption_is_replay_error(self):
        await self.store.append("s", 0, (Message("user", "hi"),))
        await self.pool.execute("UPDATE sessions SET version=5 WHERE session_id='s'")
        with self.assertRaises(SessionReplayError):
            await self.store.load("s")

    async def test_unknown_reads_do_not_create_rows(self):
        await self.store.load("missing")
        await self.store.read_events("missing")
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM sessions"), 0)

    async def test_migrations_repeat_and_concurrent_runners(self):
        await self.pool.execute(
            "DROP TABLE schema_migrations, session_controls, session_events, sessions CASCADE"
        )
        results = await asyncio.gather(*(apply_migrations(self.pool) for _ in range(6)))
        self.assertEqual(sum(result == (1,) for result in results), 1)
        self.assertEqual(
            await self.pool.fetchval("SELECT count(*) FROM schema_migrations"), 1
        )
        self.assertEqual(await apply_migrations(self.pool), ())

    async def test_migration_checksum_and_unknown_version_rejected(self):
        await self.pool.execute("UPDATE schema_migrations SET checksum=repeat('0',64)")
        with self.assertRaisesRegex(SessionStorageError, "checksum"):
            await apply_migrations(self.pool)
        await self.pool.execute(
            "UPDATE schema_migrations SET checksum=$1", load_migrations()[0].checksum
        )
        await self.pool.execute(
            "INSERT INTO schema_migrations(version,name,checksum) VALUES(99,'future',repeat('0',64))"
        )
        with self.assertRaisesRegex(SessionStorageError, "unknown"):
            await apply_migrations(self.pool)

    async def test_failed_migration_rolls_back_ddl_and_version_marker(self):
        broken = Migration(
            2, "002_broken.sql", "CREATE TABLE rollback_probe(id integer); SELECT 1/0;"
        )
        with self.assertRaises(SessionStorageError):
            await apply_migrations(self.pool, (*load_migrations(), broken))
        self.assertIsNone(
            await self.pool.fetchval("SELECT to_regclass('rollback_probe')")
        )
        self.assertEqual(
            await self.pool.fetchval("SELECT count(*) FROM schema_migrations"), 1
        )

    async def test_borrowed_pool_remains_open(self):
        await self.store.load("s")
        self.assertEqual(await self.pool.fetchval("SELECT 1"), 1)

    async def test_lock_timeout_is_storage_error_and_cancel_rolls_back(self):
        from pi_agent_py.storage._postgres_driver import PostgresSettings

        await self.store.append("s", 0, (Message("user", "original"),))
        async with self.pool.acquire() as blocker:
            async with blocker.transaction():
                await blocker.execute(
                    "SELECT version FROM sessions WHERE session_id='s' FOR UPDATE"
                )
                limited = PostgresSessionStore(
                    self.pool, settings=PostgresSettings(lock_timeout=0.001)
                )
                with self.assertRaises(SessionStorageError):
                    await limited.append("s", 1, (Message("assistant", "timeout"),))
                task = asyncio.create_task(
                    self.store.append("s", 1, (Message("assistant", "cancel"),))
                )

                async def wait_for_lock():
                    while not await self.admin.fetchval(
                        "SELECT count(*) FROM pg_stat_activity WHERE application_name=$1 AND wait_event_type='Lock'",
                        self.schema,
                    ):
                        await asyncio.sleep(0)

                await asyncio.wait_for(wait_for_lock(), timeout=5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual((await self.store.load("s")).version, 1)

    async def test_invalid_json_value_rolls_back(self):
        for invalid in (object(), float("nan"), "\x00"):
            with (
                self.subTest(value=type(invalid).__name__),
                self.assertRaises(SessionValidationError),
            ):
                await self.store.append(
                    "s",
                    0,
                    (
                        Message(
                            "tool",
                            "bad",
                            tool_call_id="c",
                            tool_name="read",
                            structured_content={"bad": invalid},
                        ),
                    ),
                )
            self.assertEqual(
                await self.pool.fetchval("SELECT count(*) FROM sessions"), 0
            )
