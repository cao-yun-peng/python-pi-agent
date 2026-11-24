"""Explicit Runtime integration with real Redis and PG, always FakeProvider."""

import asyncio
import unittest
from uuid import uuid4

from pi_agent_py.api import (
    AgentRuntimeBuilder,
    DistributedExecutionSettings,
    Finish,
    TextDelta,
    PostgresExecutionStore,
    RedisLeaseManager,
    RedisLeaseSettings,
    CommitAssistant,
    FunctionTool,
    ToolDefinition,
    ToolOutput,
)
from pi_agent_py.domain import ToolCall
from pi_agent_py.errors import SessionStorageError
from pi_agent_py.testing import FakeProvider, FakeCompactionStrategy
from pi_agent_py.testing_clock import ManualLeaseClock
from postgres_fixture import PostgresFixture, _DSN
from test_redis_lease import REDIS_URL, redis_client


class CrashAfterAssistant:
    """Lose execution after PG has really committed an assistant checkpoint."""

    def __init__(self, store):
        self.store = store
        self.crash = True

    def __getattr__(self, name):
        return getattr(self.store, name)

    def bind_writer(self, grant):
        original = self.store.bind_writer(grant)
        outer = self

        class Writer:
            async def commit_step(self, operation, command, mutations, **kwargs):
                receipt = await original.commit_step(
                    operation, command, mutations, **kwargs
                )
                if outer.crash and isinstance(command, CommitAssistant):
                    outer.crash = False
                    raise SessionStorageError(
                        "simulated process interruption after commit"
                    )
                return receipt

        return Writer()


@unittest.skipUnless(_DSN and REDIS_URL, "set explicit Redis and PG test endpoints")
class PostgresRuntimeTests(PostgresFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client = await redis_client()
        self.prefix = "pi:runtime:test:" + uuid4().hex + ":"
        self.leases = RedisLeaseManager(
            self.client, settings=RedisLeaseSettings(key_prefix=self.prefix)
        )
        self.execution = PostgresExecutionStore(self.pool)
        self.clock = ManualLeaseClock()
        self.runtimes = []

    async def asyncTearDown(self):
        try:
            for runtime in self.runtimes:
                await runtime.aclose()
            async for key in self.client.scan_iter(match=self.prefix + "*"):
                await self.client.delete(key)
            await self.client.aclose()
        finally:
            await super().asyncTearDown()
        self.assertEqual(self.clock.pending_sleeps, 0)
        self.assertEqual(self.clock.pending_timeouts, 0)

    async def runtime(
        self, provider, *, execution=None, tools=(), summary=None, system_prompt=None
    ):
        builder = (
            AgentRuntimeBuilder()
            .with_provider(provider)
            .with_session_store(self.store)
            .with_tools(tools)
            .with_system_prompt(system_prompt)
            .with_graceful_shutdown(0)
            .with_distributed_execution(
                execution or self.execution,
                self.leases,
                settings=DistributedExecutionSettings("worker", "profile:v1", "hash"),
                clock=self.clock,
            )
        )
        if summary:
            builder.with_compaction(policy=None, strategy=summary)
        runtime = await builder.build()
        self.runtimes.append(runtime)
        return runtime

    async def submit_interrupted(self, provider, *, tools=()):
        faulty = CrashAfterAssistant(self.execution)
        runtime = await self.runtime(provider, execution=faulty, tools=tools)
        session = await runtime.open_session("s")
        handle = await session.submit("hello")
        await handle.events().aclose()
        with self.assertRaises(SessionStorageError):
            await handle.result()
        return handle.run_id

    async def test_final_assistant_survives_reopen_without_another_model_request(self):
        rid = await self.submit_interrupted(
            FakeProvider([[TextDelta("final"), Finish("length")]])
        )
        self.assertEqual(
            (await self.execution.load_execution("s", rid)).checkpoint.phase,
            "awaiting_boundary",
        )
        provider = FakeProvider([])
        session = await (await self.runtime(provider)).open_session("s")
        result = await session.resume(rid)
        self.assertEqual(
            (result.message.content, result.finish_reason), ("final", "length")
        )
        self.assertEqual(provider.requests, [])
        self.assertEqual(len([m for m in result.messages if m.role == "user"]), 1)
        record = await self.execution.load_execution("s", rid)
        self.assertEqual(
            (record.state, record.attempt, record.checkpoint.dispatches),
            ("completed", 2, 1),
        )
        await session.resume(rid)
        self.assertEqual((await self.execution.load_execution("s", rid)).attempt, 2)

    async def test_prompt_recovery_and_physical_dispatch_counter_continue(self):
        first = FakeProvider(
            [[TextDelta("partial"), SessionStorageError("process interrupted")]]
        )
        session = await (await self.runtime(first)).open_session("s")
        handle = await session.submit("hello")
        await handle.events().aclose()
        with self.assertRaises(SessionStorageError):
            await handle.result()
        provider = FakeProvider([[TextDelta("done"), Finish()]])
        resumed = await (await self.runtime(provider)).open_session("s")
        result = await resumed.resume(handle.run_id)
        self.assertEqual([m.content for m in result.messages], ["hello", "done"])
        record = await self.execution.load_execution("s", handle.run_id)
        self.assertEqual(
            (record.checkpoint.request_number, record.checkpoint.dispatches), (1, 2)
        )

    async def test_tool_checkpoint_compensates_unknown_and_keeps_request_numbers(self):
        calls = []

        async def execute(arguments, context):
            calls.append(1)
            return ToolOutput("must not run")

        tool = FunctionTool(ToolDefinition("do", "do", {"type": "object"}), execute)
        rid = await self.submit_interrupted(
            FakeProvider([[ToolCall("call", "do", {}), Finish("tool_calls")]]),
            tools=(tool,),
        )
        provider = FakeProvider([[TextDelta("recovered"), Finish()]])
        session = await (await self.runtime(provider, tools=(tool,))).open_session("s")
        result = await session.resume(rid)
        self.assertEqual(calls, [])
        self.assertEqual(result.messages[2].structured_content["outcome"], "unknown")
        self.assertEqual(provider.request_contexts[0].request_number, 2)
        record = await self.execution.load_execution("s", rid)
        self.assertEqual(
            (record.checkpoint.request_number, record.checkpoint.dispatches), (2, 2)
        )

    async def test_follow_up_system_prompt_and_maintenance_compaction(self):
        provider = FakeProvider(
            [[TextDelta("one"), Finish()], [TextDelta("two"), Finish()]]
        )
        summary = FakeCompactionStrategy(["summary"])
        session = await (
            await self.runtime(provider, summary=summary, system_prompt="system")
        ).open_session("s")
        h = await session.follow_up("hello")
        await h.events().aclose()
        await h.result()
        await session.run("second")
        self.assertEqual(
            [m.content for m in (await session.snapshot()).messages],
            ["system", "hello", "one", "second", "two"],
        )
        snapshot = await session.compact(keep_recent_turns=1)
        self.assertEqual(snapshot.messages[0].role, "system")
        self.assertIn("summary", snapshot.messages[1].content)
        self.assertEqual(await self.client.exists(self.leases.key_for_session("s")), 0)

    async def test_cancelled_tool_tail_is_repaired_by_new_run_with_new_journal_tag(
        self,
    ):
        blocked = asyncio.Event()
        calls = []

        async def execute(arguments, context):
            calls.append(1)
            blocked.set()
            await asyncio.Event().wait()

        tool = FunctionTool(ToolDefinition("do", "do", {"type": "object"}), execute)
        provider = FakeProvider(
            [
                [ToolCall("call", "do", {}), Finish("tool_calls")],
                [TextDelta("next"), Finish()],
            ]
        )
        session = await (await self.runtime(provider, tools=(tool,))).open_session("s")
        handle = await session.submit("first")
        await handle.events().aclose()
        await asyncio.wait_for(blocked.wait(), 5)
        await handle.abort()
        with self.assertRaises(asyncio.CancelledError):
            await handle.result()
        result = await session.run("second")
        self.assertEqual(calls, [1])
        self.assertEqual(result.messages[2].structured_content["outcome"], "unknown")
        self.assertEqual(result.messages[3].content, "second")

    async def test_wrong_target_does_not_increment_priority_run_attempt(self):
        from pi_agent_py.api import RunSubmission
        from pi_agent_py.errors import SessionConflictError

        await self.execution.submit_run(
            RunSubmission("s", "earlier", "first", "key", "profile:v1", "hash")
        )
        provider = FakeProvider(
            [[TextDelta("first"), Finish()], [TextDelta("second"), Finish()]]
        )
        session = await (await self.runtime(provider)).open_session("s")
        handle = await session.submit("second")
        await handle.events().aclose()
        with self.assertRaises(SessionConflictError):
            await handle.result()
        self.assertEqual(
            (await self.execution.load_execution("s", "earlier")).attempt, 0
        )
        self.assertEqual(provider.requests, [])
        await session.resume("earlier")
        await session.resume(handle.run_id)
        self.assertEqual(
            (await self.execution.load_execution("s", "earlier")).attempt, 1
        )

    async def test_maintenance_cannot_bypass_pg_when_redis_key_is_lost(self):
        from pi_agent_py.api import (
            RunSubmission,
            ClaimExecutionRequest,
            execution_operation,
        )
        from pi_agent_py.errors import SessionConflictError

        await self.execution.submit_run(
            RunSubmission("s", "held", "held", "key", "profile:v1", "hash")
        )
        lease = await self.leases.acquire("s", "other-worker", "claim", 30)
        receipt = await self.execution.claim_next(
            lease, 30, execution_operation("claim", ClaimExecutionRequest(lease, 30))
        )
        summary = FakeCompactionStrategy(["must not execute"])
        session = await (
            await self.runtime(FakeProvider([]), summary=summary)
        ).open_session("s")
        try:
            await self.client.delete(self.leases.key_for_session("s"))
            with self.assertRaises(SessionConflictError):
                await session.compact()
            self.assertEqual(summary.requests, [])
            self.assertEqual(
                (await self.execution.load_execution("s", "held")).attempt, 1
            )
        finally:
            await self.execution.release_grant(receipt.grant)
