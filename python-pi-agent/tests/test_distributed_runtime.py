"""Offline execution/runtime contracts; no service or model fees."""

import asyncio
import unittest

from pi_agent_py.api import (
    AgentRuntimeBuilder,
    DistributedExecutionSettings,
    TextDelta,
    Finish,
    InMemoryEventSink,
    ToolCall,
    FunctionTool,
    ToolDefinition,
    ToolOutput,
)
from pi_agent_py.errors import (
    AgentLoopLimitError,
    SessionStorageError,
    LeaseLostError,
    ConfigurationError,
)
from pi_agent_py.storage.lease import InMemoryLeaseManager
from pi_agent_py.testing import FakeProvider, FakeCompactionStrategy, WaitUntilCancelled
from pi_agent_py.testing_clock import ManualLeaseClock
from execution_runtime_fixture import ExecutionMemory
from lease_fakes import pump_until


class DistributedConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_distributed_mode_rejects_implicit_memory_store(self):
        from pi_agent_py.storage.lease import InMemoryLeaseManager
        from pi_agent_py.errors import RuntimeBuildError

        builder = AgentRuntimeBuilder().with_provider(FakeProvider([]))
        builder.with_distributed_execution(
            object(),
            InMemoryLeaseManager(),
            settings=DistributedExecutionSettings("worker", "profile:v1", "hash"),
        )
        with self.assertRaises(RuntimeBuildError):
            await builder.build()


class DistributedRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = ManualLeaseClock()
        self.store = ExecutionMemory(self.clock)
        self.leases = InMemoryLeaseManager(clock=self.clock)
        self.events = InMemoryEventSink()
        self.runtimes = []

    async def asyncTearDown(self):
        for runtime in self.runtimes:
            await runtime.aclose()
        self.assertEqual(self.clock.pending_sleeps, 0)
        self.assertEqual(self.clock.pending_timeouts, 0)
        self.assertFalse(
            any(
                t.get_name().startswith(("lease-", "agent-run:", "agent-session:"))
                for t in asyncio.all_tasks()
            )
        )

    async def runtime(
        self,
        provider,
        *,
        max_requests=8,
        max_dispatches=40,
        spec_hash="hash",
        tools=(),
        compaction=None,
        resolver_factory=None,
    ):
        builder = (
            AgentRuntimeBuilder()
            .with_provider(provider)
            .with_session_store(self.store)
            .with_event_sink(self.events)
            .with_tools(tools)
            .with_max_model_requests(max_requests)
            .with_graceful_shutdown(0)
            .with_distributed_execution(
                self.store,
                self.leases,
                settings=DistributedExecutionSettings(
                    "worker", "profile:v1", spec_hash, max_dispatches=max_dispatches
                ),
                clock=self.clock,
            )
        )
        if compaction:
            builder.with_compaction(policy=None, strategy=compaction)
        if resolver_factory:
            builder.with_session_spec_resolver_factory(resolver_factory)
        runtime = await builder.build()
        self.runtimes.append(runtime)
        return runtime

    async def test_prompt_is_durable_before_model_and_completed_before_notification(
        self,
    ):
        outer = self

        class Provider(FakeProvider):
            async def stream(self, messages, tools=(), *, context):
                record = await outer.store.load_execution("s", context.run_id)
                outer.assertEqual(record.checkpoint.phase, "awaiting_model")
                outer.assertEqual(record.checkpoint.dispatches, 1)
                async for event in super().stream(messages, tools, context=context):
                    yield event

        provider = Provider([[TextDelta("done"), Finish()]])
        completed_after_commit = []
        store = self.store

        class EvidenceSink(InMemoryEventSink):
            def emit(self, event):
                super().emit(event)
                if event.kind == "run_completed":
                    command = store.journals[
                        event.envelope.session_id, event.envelope.run_id
                    ][-1].command
                    from pi_agent_py.execution_domain import FinishRun

                    completed_after_commit.append(
                        isinstance(command, FinishRun) and command.state == "completed"
                    )

        self.events = EvidenceSink()
        runtime = await self.runtime(provider)
        session = await runtime.open_session("s")
        handle = await session.submit("hello")
        await handle.events().aclose()
        result = await handle.result()
        record = await self.store.load_execution("s", handle.run_id)
        self.assertEqual((record.state, result.message.content), ("completed", "done"))
        self.assertEqual([m.content for m in result.messages], ["hello", "done"])
        self.assertEqual(
            len([e for e in self.events.events if e.kind == "run_completed"]), 1
        )
        self.assertIsNone(self.store.authority("s").grant)
        self.assertEqual(completed_after_commit, [True])

    async def test_resume_after_prompt_reuses_run_and_preserves_dispatch_budget(self):
        first = FakeProvider(
            [[TextDelta("partial"), SessionStorageError("interrupted")]]
        )
        session = await (await self.runtime(first)).open_session("s")
        handle = await session.submit("hello")
        await handle.events().aclose()
        with self.assertRaises(SessionStorageError):
            await handle.result()
        second = FakeProvider([[TextDelta("done"), Finish()]])
        resumed = await (await self.runtime(second)).open_session("s")
        result = await resumed.resume(handle.run_id)
        record = await self.store.load_execution("s", handle.run_id)
        self.assertEqual([m.content for m in result.messages], ["hello", "done"])
        self.assertEqual(
            (
                record.attempt,
                record.checkpoint.dispatches,
                record.checkpoint.request_number,
            ),
            (2, 2, 1),
        )
        self.assertEqual(second.request_contexts[0].run_id, handle.run_id)
        self.assertEqual(second.request_contexts[0].request_number, 1)

    async def test_final_assistant_crash_resumes_boundary_without_inference(self):
        self.store.fail_after_assistant = True
        session = await (
            await self.runtime(FakeProvider([[TextDelta("final"), Finish("length")]]))
        ).open_session("s")
        handle = await session.submit("hello")
        await handle.events().aclose()
        with self.assertRaises(SessionStorageError):
            await handle.result()
        provider = FakeProvider([])
        resumed = await (await self.runtime(provider)).open_session("s")
        result = await resumed.resume(handle.run_id)
        self.assertEqual(
            (result.message.content, result.finish_reason), ("final", "length")
        )
        self.assertEqual(provider.requests, [])
        self.assertEqual(
            (await self.store.load_execution("s", handle.run_id)).checkpoint.dispatches,
            1,
        )
        await resumed.resume(
            handle.run_id
        )  # Terminal query performs no claim or inference.
        self.assertEqual(self.store.claims, 2)
        self.assertEqual(provider.requests, [])

    async def test_resumed_tool_tail_is_unknown_without_reexecuting_tools(self):
        calls = []

        async def tool(arguments, context):
            calls.append(arguments)
            return ToolOutput("executed")

        tool = FunctionTool(ToolDefinition("do", "do", {"type": "object"}), tool)
        self.store.fail_after_assistant = True
        provider = FakeProvider([[ToolCall("call", "do", {}), Finish("tool_calls")]])
        session = await (await self.runtime(provider, tools=(tool,))).open_session("s")
        handle = await session.submit("hello")
        await handle.events().aclose()
        with self.assertRaises(SessionStorageError):
            await handle.result()
        second = FakeProvider([[TextDelta("recovered"), Finish()]])
        resumed = await (await self.runtime(second, tools=(tool,))).open_session("s")
        result = await resumed.resume(handle.run_id)
        self.assertEqual(calls, [])
        repaired = [m for m in result.messages if m.role == "tool"]
        self.assertEqual(len(repaired), 1)
        self.assertEqual(repaired[0].structured_content["outcome"], "unknown")
        self.assertEqual(second.request_contexts[0].request_number, 2)
        record = await self.store.load_execution("s", handle.run_id)
        self.assertEqual(
            (record.checkpoint.request_number, record.checkpoint.dispatches), (2, 2)
        )

    async def test_logical_budget_is_durable(self):
        provider = FakeProvider(
            [[ToolCall("call", "missing", {}), Finish("tool_calls")]]
        )
        session = await (await self.runtime(provider, max_requests=1)).open_session("s")
        handle = await session.submit("hello")
        await handle.events().aclose()
        with self.assertRaises(AgentLoopLimitError):
            await handle.result()
        record = await self.store.load_execution("s", handle.run_id)
        self.assertEqual(
            (record.state, record.checkpoint.request_number), ("failed", 1)
        )
        self.assertEqual(len(provider.requests), 1)

    async def test_physical_retries_reserve_separately_and_cannot_reset_budget(self):
        from pi_agent_py.providers.runtime import ResilientModelRuntime
        from pi_agent_py.ports import ProviderCandidate
        from pi_agent_py.errors import ProviderTransportError

        class Backoff:
            def delay_seconds(self, failure):
                return 0

        raw = FakeProvider(
            [[ProviderTransportError("retry")], [TextDelta("not allowed"), Finish()]]
        )
        provider = ResilientModelRuntime(
            (ProviderCandidate("raw", raw),), backoff=Backoff()
        )
        session = await (await self.runtime(provider, max_dispatches=1)).open_session(
            "s"
        )
        handle = await session.submit("hello")
        await handle.events().aclose()
        with self.assertRaises(AgentLoopLimitError):
            await handle.result()
        self.assertEqual(len(raw.requests), 1)
        record = await self.store.load_execution("s", handle.run_id)
        self.assertEqual((record.state, record.checkpoint.dispatches), ("failed", 1))

    async def test_accepted_follow_up_and_steer_are_not_double_delivered(self):
        blocked, proceed = asyncio.Event(), asyncio.Event()

        class Provider(FakeProvider):
            async def stream(self, messages, tools=(), *, context):
                if not self.requests:
                    blocked.set()
                    await proceed.wait()
                async for event in super().stream(messages, tools, context=context):
                    yield event

        provider = Provider(
            [
                [TextDelta("first"), Finish()],
                [TextDelta("steered"), Finish()],
                [TextDelta("followed"), Finish()],
            ]
        )
        session = await (await self.runtime(provider)).open_session("s")
        handle = await session.submit("first")
        await handle.events().aclose()
        await blocked.wait()
        follow = await session.follow_up("next")
        await follow.events().aclose()
        self.assertEqual(
            (await self.store.load_execution("s", follow.run_id)).state, "queued"
        )
        await session.steer("steer")
        proceed.set()
        self.assertEqual((await handle.result()).message.content, "steered")
        result = await follow.result()
        self.assertEqual(
            [m.content for m in result.messages if m.role == "user"],
            ["first", "steer", "next"],
        )
        self.assertEqual(result.message.content, "followed")
        self.assertEqual(
            [c.request_number for c in provider.request_contexts], [1, 2, 1]
        )
        self.assertEqual(
            (await self.store.load_execution("s", handle.run_id)).checkpoint.dispatches,
            2,
        )

    async def test_cancelled_tool_tail_is_repaired_before_the_next_prompt(self):
        blocked = asyncio.Event()
        calls = []

        async def tool(arguments, context):
            calls.append("side effect may have happened")
            blocked.set()
            await asyncio.Event().wait()

        tool = FunctionTool(ToolDefinition("do", "do", {"type": "object"}), tool)
        provider = FakeProvider(
            [
                [ToolCall("call", "do", {}), Finish("tool_calls")],
                [TextDelta("next"), Finish()],
            ]
        )
        session = await (await self.runtime(provider, tools=(tool,))).open_session("s")
        handle = await session.submit("first")
        await handle.events().aclose()
        await blocked.wait()
        await handle.abort()
        with self.assertRaises(asyncio.CancelledError):
            await handle.result()
        self.assertEqual(
            (await self.store.load_execution("s", handle.run_id)).state, "cancelled"
        )
        result = await session.run("second")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            [m.role for m in result.messages],
            ["user", "assistant", "tool", "user", "assistant"],
        )
        self.assertEqual(result.messages[2].structured_content["outcome"], "unknown")

    async def test_lost_lease_leaves_running_run_and_no_background_tasks(self):
        provider = FakeProvider([[WaitUntilCancelled()]])
        session = await (await self.runtime(provider)).open_session("s")
        handle = await session.submit("hello")
        await handle.events().aclose()
        await provider.blocked.wait()
        await pump_until(lambda: self.clock.pending_sleeps == 1)
        self.leases.clear()
        self.clock.advance(10)
        with self.assertRaises(LeaseLostError):
            await handle.result()
        self.assertEqual(
            (await self.store.load_execution("s", handle.run_id)).state, "running"
        )

    async def test_recovery_configuration_mismatch_never_dispatches(self):
        self.store.fail_after_assistant = True
        session = await (
            await self.runtime(FakeProvider([[TextDelta("done"), Finish()]]))
        ).open_session("s")
        handle = await session.submit("hello")
        await handle.events().aclose()
        with self.assertRaises(SessionStorageError):
            await handle.result()
        provider = FakeProvider([])
        other = await (await self.runtime(provider, spec_hash="changed")).open_session(
            "s"
        )
        with self.assertRaises(ConfigurationError):
            await other.resume(handle.run_id)
        self.assertEqual(self.store.claims, 1)
        self.assertEqual(provider.requests, [])

    async def test_maintenance_compaction_uses_grant_and_cleans_up(self):
        provider = FakeProvider(
            [[TextDelta("one"), Finish()], [TextDelta("two"), Finish()]]
        )
        summary = FakeCompactionStrategy(["summary"])
        session = await (await self.runtime(provider, compaction=summary)).open_session(
            "s"
        )
        await session.run("first")
        await session.run("second")
        snapshot = await session.compact(keep_recent_turns=1)
        self.assertEqual(self.store.maintenance_claims, 1)
        self.assertIn("summary", snapshot.messages[0].content)
        self.assertIsNone(self.store.authority("s").grant)

    async def test_unknown_terminal_commit_resolves_original_result(self):
        self.store.unknown_finish = True
        session = await (
            await self.runtime(FakeProvider([[TextDelta("done"), Finish()]]))
        ).open_session("s")
        result = await session.run("hello")
        self.assertEqual(result.message.content, "done")
        self.assertEqual(self.store.claims, 1)

    async def test_out_of_order_submission_does_not_spend_an_earlier_run_attempt(self):
        from pi_agent_py.api import RunSubmission
        from pi_agent_py.errors import SessionConflictError

        await self.store.submit_run(
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
        self.assertEqual((await self.store.load_execution("s", "earlier")).attempt, 0)
        self.assertEqual(provider.requests, [])
        await session.resume("earlier")
        await session.resume(handle.run_id)
        self.assertEqual((await self.store.load_execution("s", "earlier")).attempt, 1)

    async def test_maintenance_lease_loss_cancels_summary_without_committing(self):
        summary = FakeCompactionStrategy([WaitUntilCancelled()])
        provider = FakeProvider(
            [[TextDelta("one"), Finish()], [TextDelta("two"), Finish()]]
        )
        session = await (await self.runtime(provider, compaction=summary)).open_session(
            "s"
        )
        await session.run("first")
        await session.run("second")
        before = await session.snapshot()
        task = asyncio.create_task(session.compact(keep_recent_turns=1))
        await summary.blocked.wait()
        await pump_until(lambda: self.clock.pending_sleeps == 1)
        self.leases.clear()
        self.clock.advance(10)
        with self.assertRaises(LeaseLostError):
            await task
        self.assertEqual(await session.snapshot(), before)

    async def test_idle_abort_does_not_cancel_a_later_submission(self):
        session = await (
            await self.runtime(FakeProvider([[TextDelta("done"), Finish()]]))
        ).open_session("s")
        await session.abort()
        self.assertEqual((await session.run("hello")).message.content, "done")

    async def test_crash_between_continuation_and_dispatch_keeps_request_number(self):
        self.store.fail_after_continue = True
        first = FakeProvider([[ToolCall("c", "missing", {}), Finish("tool_calls")]])
        session = await (await self.runtime(first, max_requests=2)).open_session("s")
        handle = await session.submit("hello")
        await handle.events().aclose()
        with self.assertRaises(SessionStorageError):
            await handle.result()
        record = await self.store.load_execution("s", handle.run_id)
        self.assertEqual(
            (record.checkpoint.request_number, record.checkpoint.dispatches), (2, 1)
        )
        second = FakeProvider([[TextDelta("final"), Finish()]])
        resumed = await (await self.runtime(second)).open_session("s")
        await resumed.resume(handle.run_id)
        self.assertEqual(second.request_contexts[0].request_number, 2)
        self.assertEqual(second.request_contexts[0].turn_id, handle.run_id + ":turn:2")
        self.assertEqual(
            (await self.store.load_execution("s", handle.run_id)).checkpoint.dispatches,
            2,
        )

    async def test_committed_tool_result_is_preserved_while_only_missing_calls_are_repaired(
        self,
    ):
        calls = []

        async def execute(arguments, context):
            calls.append(context.call.id)
            return ToolOutput("already committed")

        tool = FunctionTool(ToolDefinition("do", "do", {"type": "object"}), execute)
        self.store.fail_after_tool = True
        first = FakeProvider(
            [[ToolCall("a", "do", {}), ToolCall("b", "do", {}), Finish("tool_calls")]]
        )
        session = await (await self.runtime(first, tools=(tool,))).open_session("s")
        handle = await session.submit("hello")
        await handle.events().aclose()
        with self.assertRaises(SessionStorageError):
            await handle.result()
        second = FakeProvider([[TextDelta("done"), Finish()]])
        resumed = await (await self.runtime(second, tools=(tool,))).open_session("s")
        result = await resumed.resume(handle.run_id)
        results = [m for m in result.messages if m.role == "tool"]
        self.assertEqual(calls, ["a"])
        self.assertEqual(
            [(m.tool_call_id, m.is_error) for m in results], [("a", False), ("b", True)]
        )
        self.assertEqual(results[0].content, "already committed")

    async def test_owned_resources_close_after_work_heartbeats_and_grants(self):
        closed = []

        async def close_execution():
            self.assertIsNone(self.store.authority("s").grant)
            self.assertEqual(self.clock.pending_sleeps, 0)
            closed.append("execution")

        async def close_leases():
            self.assertEqual(self.clock.pending_timeouts, 0)
            closed.append("leases")

        provider = FakeProvider([[WaitUntilCancelled()]])
        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(provider)
            .with_session_store(self.store)
            .with_graceful_shutdown(0)
            .with_distributed_execution(
                self.store,
                self.leases,
                settings=DistributedExecutionSettings("worker", "profile:v1", "hash"),
                clock=self.clock,
                store_ownership="owned",
                store_cleanup=close_execution,
                lease_ownership="owned",
                lease_cleanup=close_leases,
            )
            .build()
        )
        self.runtimes.append(runtime)
        session = await runtime.open_session("s")
        handle = await session.submit("hello")
        await handle.events().aclose()
        await provider.blocked.wait()
        await runtime.aclose()
        with self.assertRaises(asyncio.CancelledError):
            await handle.result()
        self.assertEqual(closed, ["leases", "execution"])

    async def test_terminal_resume_returns_original_prefix_after_later_runs(self):
        provider = FakeProvider(
            [[TextDelta("one"), Finish()], [TextDelta("two"), Finish()]]
        )
        session = await (await self.runtime(provider)).open_session("s")
        first = await session.submit("first")
        await first.events().aclose()
        original = await first.result()
        await session.run("second")
        restored = await session.resume(first.run_id)
        self.assertEqual(restored.messages, original.messages)
        self.assertEqual(restored.message.content, "one")
        self.assertEqual(len(provider.requests), 2)

    async def test_summary_retries_check_lease_before_each_physical_dispatch(self):
        from pi_agent_py.context.compaction import ModelCompactionStrategy
        from pi_agent_py.providers.runtime import ResilientModelRuntime
        from pi_agent_py.ports import ProviderCandidate
        from pi_agent_py.errors import ProviderTransportError

        clock = self.clock

        class Remote(FakeProvider):
            async def stream(self, messages, tools=(), *, context):
                clock.advance(
                    40
                )  # First physical invocation crosses the lease deadline.
                async for event in super().stream(messages, tools, context=context):
                    yield event

        class Backoff:
            def delay_seconds(self, failure):
                return 0

        raw = Remote(
            [
                [ProviderTransportError("retry")],
                [TextDelta("must not dispatch"), Finish()],
            ]
        )
        summary = ModelCompactionStrategy(
            ResilientModelRuntime(
                (ProviderCandidate("summary", raw),), backoff=Backoff()
            )
        )
        provider = FakeProvider(
            [[TextDelta("one"), Finish()], [TextDelta("two"), Finish()]]
        )
        session = await (await self.runtime(provider, compaction=summary)).open_session(
            "s"
        )
        await session.run("first")
        await session.run("second")
        with self.assertRaises(LeaseLostError):
            await session.compact(keep_recent_turns=1)
        self.assertEqual(len(raw.requests), 1)

    async def test_nested_retry_provider_cannot_hide_physical_dispatches(self):
        from pi_agent_py.providers.runtime import ResilientModelRuntime
        from pi_agent_py.ports import ProviderCandidate, RetryDecision
        from pi_agent_py.errors import ProviderTransportError

        class Backoff:
            def delay_seconds(self, failure):
                return 0

        raw = FakeProvider(
            [[ProviderTransportError("retry")], [TextDelta("over budget"), Finish()]]
        )
        inner = ResilientModelRuntime(
            (ProviderCandidate("raw", raw),), backoff=Backoff()
        )
        decisions = []

        class RetryEverything:
            def decide(self, failure):
                decisions.append(failure)
                return RetryDecision("retry_same", "custom retry")

        outer = ResilientModelRuntime(
            (ProviderCandidate("nested", inner),),
            backoff=Backoff(),
            retry_policy=RetryEverything(),
        )
        session = await (await self.runtime(outer, max_dispatches=1)).open_session("s")
        with self.assertRaises(AgentLoopLimitError):
            await session.run("hello")
        self.assertEqual(len(raw.requests), 1)
        self.assertEqual(decisions, [])

    async def test_selected_role_recovery_identity_cannot_fall_back_to_default(self):
        from pi_agent_py.api import SessionSpec

        def resolver_factory(base):
            class Resolver:
                async def resolve(self, sid, request):
                    return SessionSpec(
                        base.replace(system_prompt="role-A"),
                        recovery_spec_ref="role-A:v1",
                        recovery_spec_hash="role-A-hash",
                    )

            return Resolver()

        self.store.fail_after_assistant = True
        first = await self.runtime(
            FakeProvider([[TextDelta("final"), Finish()]]),
            resolver_factory=resolver_factory,
        )
        session = await first.open_session("s")
        handle = await session.submit("hello")
        await handle.events().aclose()
        with self.assertRaises(SessionStorageError):
            await handle.result()
        record = await self.store.load_execution("s", handle.run_id)
        self.assertEqual(record.submission.recovery_spec_ref, "role-A:v1")
        default_provider = FakeProvider([])
        default = await (await self.runtime(default_provider)).open_session("s")
        with self.assertRaises(ConfigurationError):
            await default.resume(handle.run_id)
        restored = await (
            await self.runtime(FakeProvider([]), resolver_factory=resolver_factory)
        ).open_session("s")
        result = await restored.resume(handle.run_id)
        self.assertEqual(result.messages[0].content, "role-A")
        self.assertEqual(self.store.claims, 2)
        self.assertEqual(default_provider.requests, [])

    async def test_selected_components_need_explicit_recovery_identity(self):
        from pi_agent_py.api import SessionSpec

        def resolver_factory(base):
            class Resolver:
                async def resolve(self, sid, request):
                    return SessionSpec(base.replace(system_prompt="selected"))

            return Resolver()

        runtime = await self.runtime(
            FakeProvider([]), resolver_factory=resolver_factory
        )
        with self.assertRaises(ConfigurationError):
            await runtime.open_session("s")
        self.assertEqual(self.store.journals, {})

    async def test_direct_session_injection_observes_one_run_lifecycle(self):
        from pi_agent_py.api import AgentSession, AgentComponents, DistributedExecution

        provider = FakeProvider([[TextDelta("done"), Finish()]])
        session = AgentSession(
            "s",
            self.store,
            AgentComponents(provider),
            event_sink=self.events,
            distributed_execution=DistributedExecution(
                self.store,
                self.leases,
                DistributedExecutionSettings("worker", "profile:v1", "hash"),
                self.clock,
            ),
        )
        async with session:
            self.assertEqual((await session.run("hello")).message.content, "done")
        self.assertEqual(sum(e.kind == "run_started" for e in self.events.events), 1)
        self.assertEqual(sum(e.kind == "run_completed" for e in self.events.events), 1)

    async def test_aborting_a_completed_handle_does_not_cancel_a_later_run(self):
        session = await (
            await self.runtime(
                FakeProvider(
                    [[TextDelta("one"), Finish()], [TextDelta("two"), Finish()]]
                )
            )
        ).open_session("s")
        handle = await session.submit("first")
        await handle.events().aclose()
        await handle.result()
        await handle.abort()
        self.assertEqual((await session.run("second")).message.content, "two")

    async def test_queue_cancellation_storage_failure_still_joins_active_work_on_close(
        self,
    ):
        from pi_agent_py.errors import RuntimeCloseError

        provider = FakeProvider([[WaitUntilCancelled()]])
        runtime = await self.runtime(provider)
        session = await runtime.open_session("s")
        active = await session.submit("first")
        await active.events().aclose()
        await provider.blocked.wait()
        queued = await session.follow_up("next")
        await queued.events().aclose()
        original = self.store.cancel_queued

        async def unavailable(*args, **kwargs):
            raise SessionStorageError("queue cancellation unavailable")

        self.store.cancel_queued = unavailable
        try:
            with self.assertRaises(RuntimeCloseError):
                await runtime.aclose()
            self.assertEqual(self.clock.pending_sleeps, 0)
            with self.assertRaises(asyncio.CancelledError):
                await active.result()
            with self.assertRaises(SessionStorageError):
                await queued.result()
        finally:
            self.store.cancel_queued = original
            await session.abort()
