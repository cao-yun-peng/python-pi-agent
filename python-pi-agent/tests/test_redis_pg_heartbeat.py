"""Real Redis and PG, manually scheduled heartbeat, no paid model requests."""

import asyncio
import unittest
from uuid import uuid4

from pi_agent_py.api import (
    AppendExecutionMessages,
    ClaimExecutionRequest,
    CommitAssistant,
    CommitPrompt,
    DispatchModel,
    DurableEventIdentity,
    ExecutionStepRequest,
    FinishExecutionRequest,
    FinishRun,
    Message,
    PostgresExecutionStore,
    RunSubmission,
    SessionEventContext,
    execution_operation,
)
from pi_agent_py.errors import LeaseLostError, SessionConflictError
from pi_agent_py.runtime.lease_heartbeat import LeaseHeartbeat, LeaseHeartbeatSettings
from pi_agent_py.storage.redis_lease import RedisLeaseManager, RedisLeaseSettings
from pi_agent_py.testing_clock import ManualLeaseClock
from postgres_fixture import PostgresFixture, _DSN
from test_redis_lease import REDIS_URL, redis_client
from lease_fakes import pump_until


@unittest.skipUnless(
    REDIS_URL and _DSN, "set Redis and PG test endpoints for heartbeat integration"
)
class RedisPGHeartbeatTests(PostgresFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client = await redis_client()
        self.prefix = "pi:heartbeat:test:" + uuid4().hex + ":"
        self.manager = RedisLeaseManager(
            self.client, settings=RedisLeaseSettings(key_prefix=self.prefix)
        )
        self.execution = PostgresExecutionStore(self.pool)
        self.clock = ManualLeaseClock()
        self.settings = LeaseHeartbeatSettings(3, 0.5, 0.25, 0.25, 0.25, 0.1)
        await self.execution.submit_run(
            RunSubmission("s", "r", "hello", "key", "profile", "hash")
        )
        self.lease = await self.manager.acquire("s", "A", "claimA", 10)
        self.receipt = await self.execution.claim_next(
            self.lease,
            5,
            execution_operation("claim", ClaimExecutionRequest(self.lease, 5)),
        )

    async def asyncTearDown(self):
        async for key in self.client.scan_iter(match=self.prefix + "*"):
            await self.client.delete(key)
        await self.client.aclose()
        await super().asyncTearDown()

    async def test_redis_key_loss_does_not_bypass_pg_then_heartbeat_stops_old_worker(
        self,
    ):
        guard = LeaseHeartbeat(
            self.manager,
            self.execution,
            self.lease,
            self.receipt.grant,
            settings=self.settings,
            clock=self.clock,
        )
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def work(scope):
            scope.ensure_active()
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = asyncio.create_task(guard.run(work))
        try:
            await asyncio.wait_for(started.wait(), 5)
            await pump_until(lambda: self.clock.pending_sleeps == 1)
            # Losing the lease key has the same observable state as Redis clearing.
            await self.client.delete(self.manager.key_for_session("s"))
            other = await self.manager.acquire("s", "B", "claimB", 10)
            with self.assertRaises(SessionConflictError):
                await self.execution.claim_next(
                    other,
                    5,
                    execution_operation("blocked", ClaimExecutionRequest(other, 5)),
                )
            self.clock.advance(0.5)
            with self.assertRaises(LeaseLostError):
                await asyncio.wait_for(task, 5)
            self.assertTrue(cancelled.is_set())
            recovered = await self.execution.claim_next(
                other,
                5,
                execution_operation("takeover", ClaimExecutionRequest(other, 5)),
            )
            self.assertEqual(recovered.grant.epoch, self.receipt.grant.epoch + 1)
            old = self.receipt.grant
            req = ExecutionStepRequest(
                old, DispatchModel(), (), 0, self.receipt.run.revision
            )
            with self.assertRaises(SessionConflictError):
                await self.execution.commit_step(
                    old,
                    execution_operation("old-write", req),
                    DispatchModel(),
                    (),
                    expected_session_version=0,
                    expected_revision=self.receipt.run.revision,
                )
            self.assertEqual(
                await self.client.exists(self.manager.key_for_session("s")), 1
            )
        finally:
            await guard.aclose()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.clock.pending_sleeps, 0)
        self.assertEqual(self.clock.pending_timeouts, 0)

    async def test_finalize_stops_heartbeat_before_committing_terminal_run(self):
        guard = LeaseHeartbeat(
            self.manager,
            self.execution,
            self.lease,
            self.receipt.grant,
            settings=self.settings,
            clock=self.clock,
        )

        async def work(scope):
            receipt = self.receipt
            writer = self.execution.bind_writer(scope.grant)
            context = SessionEventContext(run_id="r")
            for name, command, mutations in (
                (
                    "prompt",
                    CommitPrompt(DurableEventIdentity("s", 1, "s:1")),
                    (AppendExecutionMessages((Message("user", "hello"),), context),),
                ),
                ("dispatch", DispatchModel(), ()),
                (
                    "assistant",
                    CommitAssistant(DurableEventIdentity("s", 2, "s:2"), "stop"),
                    (
                        AppendExecutionMessages(
                            (Message("assistant", "done"),), context
                        ),
                    ),
                ),
            ):
                scope.ensure_active()
                request = ExecutionStepRequest(
                    scope.grant,
                    command,
                    mutations,
                    receipt.session.version,
                    receipt.run.revision,
                )
                receipt = await writer.commit_step(
                    execution_operation(name, request),
                    command,
                    mutations,
                    expected_session_version=request.expected_session_version,
                    expected_revision=request.expected_revision,
                )
            return receipt

        async def finalize(receipt, grant):
            self.assertEqual(self.clock.pending_sleeps, 0)
            finish = FinishRun("completed")
            req = FinishExecutionRequest(
                grant, finish, receipt.session.version, receipt.run.revision
            )
            await self.execution.finish_run(
                grant,
                execution_operation("finish", req),
                finish,
                expected_session_version=req.expected_session_version,
                expected_revision=req.expected_revision,
            )

        await guard.run(work, finalize=finalize)
        self.assertEqual(
            (await self.execution.load_execution("s", "r")).state, "completed"
        )
        self.assertEqual(await self.client.exists(self.manager.key_for_session("s")), 0)
        self.assertIsNone(guard.lost_reason)
        self.assertEqual(guard.cleanup_failures, ())
