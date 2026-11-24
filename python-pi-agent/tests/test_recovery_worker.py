"""Offline recovery orchestration contracts; no model or service requests."""

import asyncio
import json
import unittest

from pi_agent_py.api import (
    AgentRuntimeBuilder,
    Finish,
    TextDelta,
    RunSubmission,
    InMemoryEventSink,
    ToolCall,
    FunctionTool,
    ToolDefinition,
    ToolOutput,
)
from pi_agent_py.errors import (
    SessionStorageError,
    RecoveryConfigurationError,
    SessionConflictError,
)
from pi_agent_py.application.recovery import (
    RegisteredRecoveryDispatcher,
    RecoveryDispatchSettings,
)
from pi_agent_py.runtime.execution_config import (
    DistributedExecution,
    DistributedExecutionSettings,
)
from pi_agent_py.runtime.recovery import SessionRecoveryWorker
from pi_agent_py.storage.lease import InMemoryLeaseManager
from pi_agent_py.testing import FakeProvider, WaitUntilCancelled
from pi_agent_py.testing_clock import ManualLeaseClock
from execution_runtime_fixture import ExecutionMemory
from lease_fakes import pump_until

from pi_agent_py.runtime.recovery import RecoveryWorkerSettings


class RecoverySettingsTests(unittest.TestCase):
    def test_positive_bounds_and_retry_budget(self):
        for value in (0, -1, True, float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RecoveryWorkerSettings(poll_interval=value)
        with self.assertRaises(ValueError):
            RecoveryWorkerSettings(retry_delay=2, max_retry_delay=1)


class RecoveryWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = ManualLeaseClock()
        self.store = ExecutionMemory(self.clock)
        self.leases = InMemoryLeaseManager(clock=self.clock)
        self.events = InMemoryEventSink()
        self.providers = {}
        self.runtimes, self.workers = [], []
        self.tools = ()
        self.factory_calls = []

    async def asyncTearDown(self):
        for worker in self.workers:
            await worker.aclose()
        for runtime in self.runtimes:
            await runtime.aclose()
        self.assertEqual(self.clock.pending_sleeps, 0)
        self.assertEqual(self.clock.pending_timeouts, 0)
        self.assertFalse(
            any(
                task.get_name().startswith(
                    ("recovery-", "lease-", "agent-run:", "agent-session:")
                )
                for task in asyncio.all_tasks()
            )
        )

    def config(self, owner):
        return DistributedExecution(
            self.store,
            self.leases,
            DistributedExecutionSettings(owner, "profile:v1", "hash"),
            self.clock,
        )

    async def accept(self, sid="s", rid="r", prompt="hello", **kwargs):
        return await self.store.submit_run(
            RunSubmission(sid, rid, prompt, rid, "profile:v1", "hash", **kwargs)
        )

    async def build_runtime(self, provider, owner="direct", *, cleanup=None):
        builder = (
            AgentRuntimeBuilder()
            .with_provider(
                provider, ownership="owned" if cleanup else "borrowed", cleanup=cleanup
            )
            .with_session_store(self.store)
            .with_tools(self.tools)
            .with_event_sink(self.events)
            .with_graceful_shutdown(0)
            .with_distributed_execution(
                self.store,
                self.leases,
                settings=self.config(owner).settings,
                clock=self.clock,
            )
        )
        runtime = await builder.build()
        self.runtimes.append(runtime)
        return runtime

    async def factory(self, receipt):
        key = (receipt.run.submission.session_id, receipt.run.submission.run_id)
        self.factory_calls.append(key)
        return await self.build_runtime(
            self.providers.get(key, FakeProvider([[TextDelta("done"), Finish()]])),
            receipt.grant.owner_id,
        )

    def worker(
        self,
        owner="A",
        *,
        factory=None,
        catalog=None,
        settings=None,
        dispatch_settings=None,
    ):
        config = self.config(owner)
        dispatcher = RegisteredRecoveryDispatcher(
            self.store,
            config,
            catalog
            if catalog is not None
            else {("profile:v1", "hash"): factory or self.factory},
            settings=dispatch_settings or RecoveryDispatchSettings(),
            event_sink=self.events,
        )
        worker = SessionRecoveryWorker(
            config,
            dispatcher,
            settings=settings or RecoveryWorkerSettings(),
            event_sink=self.events,
        )
        self.workers.append(worker)
        return worker

    async def interrupted(self, provider, *, follow_up=False):
        runtime = await self.build_runtime(provider)
        session = await runtime.open_session("s")
        handle = await (
            session.follow_up("hello") if follow_up else session.submit("hello")
        )
        await handle.events().aclose()
        with self.assertRaises(SessionStorageError):
            await handle.result()
        return handle.run_id

    async def test_accepted_prompt_recovers_once_and_does_not_claim_twice(self):
        await self.accept()
        report = await self.worker().run_once()
        self.assertEqual(report.attempts[0].status, "completed")
        record = await self.store.load_execution("s", "r")
        self.assertEqual((record.attempt, record.checkpoint.dispatches), (1, 1))
        self.assertEqual(
            [m.content for m in (await self.store.load("s")).messages],
            ["hello", "done"],
        )
        self.assertTrue(all(runtime.state == "closed" for runtime in self.runtimes))
        self.assertEqual((await self.worker("B").run_once()).attempts, ())

    async def test_delivered_follow_up_is_discovered_via_run_not_pending_control(self):
        rid = await self.interrupted(
            FakeProvider([[TextDelta("partial"), SessionStorageError("crash")]]),
            follow_up=True,
        )
        self.assertEqual((await self.store.load("s")).pending_controls, ())
        await self.worker().run_once()
        record = await self.store.load_execution("s", rid)
        self.assertEqual(
            (record.state, record.attempt, record.checkpoint.dispatches),
            ("completed", 2, 2),
        )
        self.assertEqual(
            sum(m.role == "user" for m in (await self.store.load("s")).messages), 1
        )

    async def test_final_assistant_recovers_without_inference(self):
        self.store.fail_after_assistant = True
        rid = await self.interrupted(
            FakeProvider([[TextDelta("final"), Finish("length")]])
        )
        provider = FakeProvider([])
        self.providers["s", rid] = provider
        await self.worker().run_once()
        record = await self.store.load_execution("s", rid)
        self.assertEqual(
            (
                record.state,
                record.checkpoint.finish_reason,
                record.checkpoint.dispatches,
            ),
            ("completed", "length", 1),
        )
        self.assertEqual(provider.requests, [])

    async def test_missing_and_mismatched_catalogs_fail_without_default_provider(self):
        for catalog, code in (
            ({}, "recovery_spec_missing"),
            ({("profile:v1", "wrong"): self.factory}, "recovery_spec_mismatch"),
        ):
            rid = code
            await self.accept(rid=rid)
            report = await self.worker(catalog=catalog).run_once()
            self.assertEqual(report.attempts[0].status, "failed")
            self.assertEqual(
                (await self.store.load_execution("s", rid)).error_code, code
            )
        self.assertEqual(self.factory_calls, [])

    async def test_workspace_and_permissions_rejection_is_durable_and_redacted(self):
        for code in ("recovery_workspace_mismatch", "recovery_permissions_mismatch"):
            await self.accept(rid=code)

            async def rejected(receipt):
                raise RecoveryConfigurationError(code) from OSError(
                    "private workspace and token"
                )

            await self.worker(factory=rejected).run_once()
            self.assertEqual(
                (await self.store.load_execution("s", code)).error_code, code
            )
        payloads = json.dumps(
            [e.payload for e in self.events.events if e.kind.startswith("recovery_")]
        )
        self.assertNotIn("private", payloads)
        self.assertNotIn("hello", payloads)

    async def test_busy_head_backoff_allows_another_session(self):
        await self.accept("first")
        await self.accept("second")
        held = await self.leases.acquire("first", "other", "claim", 30)
        worker = self.worker(settings=RecoveryWorkerSettings(scan_limit=1))
        try:
            self.assertEqual((await worker.run_once()).attempts[0].status, "busy")
            self.assertEqual((await worker.run_once()).attempts[0].session_id, "second")
            self.assertEqual((await self.store.load_execution("first", "r")).attempt, 0)
        finally:
            await self.leases.release(held)
        self.clock.advance(0.5)
        await worker.run_once()
        self.assertEqual(
            (await self.store.load_execution("first", "r")).state, "completed"
        )

    async def test_background_scans_other_sessions_while_one_is_blocked(self):
        await self.accept("first")
        await self.accept("second")
        first = FakeProvider([[WaitUntilCancelled()]])
        second = FakeProvider([[TextDelta("second"), Finish()]])
        self.providers.update({("first", "r"): first, ("second", "r"): second})
        worker = self.worker(
            settings=RecoveryWorkerSettings(scan_limit=1, max_concurrent_sessions=2)
        )
        await worker.start()
        await first.blocked.wait()
        await pump_until(lambda: self.clock.pending_sleeps >= 2)
        self.clock.advance(1)
        await pump_until(lambda: len(second.requests) == 1)
        await pump_until(
            lambda: (
                ("second", "r") in self.factory_calls
                and "second" not in worker.active_sessions
            )
        )
        self.assertEqual(
            (await self.store.load_execution("second", "r")).state, "completed"
        )
        self.assertIn("first", worker.active_sessions)
        await worker.aclose()
        self.assertEqual(
            (await self.store.load_execution("first", "r")).state, "running"
        )

    async def test_two_workers_do_not_execute_same_session_in_parallel(self):
        await self.accept()
        blocked = FakeProvider([[WaitUntilCancelled()]])
        self.providers["s", "r"] = blocked
        first = self.worker("A")
        task = asyncio.create_task(first.run_once())
        await blocked.blocked.wait()
        second = self.worker("B")
        self.assertEqual((await second.run_once()).attempts, ())
        self.assertEqual(self.store.claims, 1)
        await first.aclose()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.providers["s", "r"] = FakeProvider([[TextDelta("taken over"), Finish()]])
        await second.run_once()
        record = await self.store.load_execution("s", "r")
        self.assertEqual(
            (record.state, record.attempt, record.epoch), ("completed", 2, 2)
        )

    async def test_partial_model_output_is_not_committed_and_dispatch_budget_continues(
        self,
    ):
        rid = await self.interrupted(
            FakeProvider([[TextDelta("half"), SessionStorageError("process gone")]])
        )
        self.assertEqual(
            [m.content for m in (await self.store.load("s")).messages], ["hello"]
        )
        provider = FakeProvider([[TextDelta("whole"), Finish()]])
        self.providers["s", rid] = provider
        await self.worker().run_once()
        self.assertEqual(provider.request_contexts[0].request_number, 1)
        self.assertEqual(
            (await self.store.load_execution("s", rid)).checkpoint.dispatches, 2
        )

    async def test_tool_calls_and_partial_results_share_unknown_compensation(self):
        calls = []

        async def execute(arguments, context):
            calls.append(context.call.id)
            return ToolOutput("already committed")

        tool = FunctionTool(ToolDefinition("do", "do", {"type": "object"}), execute)
        self.tools = (tool,)
        self.store.fail_after_tool = True
        rid = await self.interrupted(
            FakeProvider(
                [
                    [
                        ToolCall("a", "do", {}),
                        ToolCall("b", "do", {}),
                        Finish("tool_calls"),
                    ]
                ]
            )
        )
        provider = FakeProvider([[TextDelta("done"), Finish()]])
        self.providers["s", rid] = provider
        await self.worker().run_once()
        history = await self.store.read_events("s")
        recovered = [
            e for e in history if e.message is not None and e.message.role == "tool"
        ]
        self.assertEqual(calls, ["a"])
        self.assertEqual(
            [
                (e.message.tool_call_id, e.message.tool_name, e.message.is_error)
                for e in recovered
            ],
            [("a", "do", False), ("b", "do", True)],
        )
        self.assertEqual(recovered[-1].provenance.source, "recovery")
        self.assertEqual(recovered[-1].message.structured_content["outcome"], "unknown")
        self.assertEqual(provider.request_contexts[0].request_number, 2)

    async def test_assistant_tool_checkpoint_does_not_reexecute_any_tool(self):
        self.store.fail_after_assistant = True
        rid = await self.interrupted(
            FakeProvider([[ToolCall("a", "missing", {}), Finish("tool_calls")]])
        )
        self.providers["s", rid] = FakeProvider([[TextDelta("done"), Finish()]])
        await self.worker().run_once()
        snapshot = await self.store.load("s")
        self.assertEqual(snapshot.messages[2].tool_call_id, "a")
        self.assertTrue(snapshot.messages[2].is_error)
        self.assertEqual(snapshot.messages[2].structured_content["outcome"], "unknown")

    async def test_redis_clear_and_pg_renewal_failure_stop_attempt_without_terminal(
        self,
    ):
        for failure in ("redis", "pg"):
            sid = failure
            await self.accept(sid)
            provider = FakeProvider([[WaitUntilCancelled()]])
            self.providers[sid, "r"] = provider
            worker = self.worker()
            task = asyncio.create_task(worker.run_once())
            await provider.blocked.wait()
            await pump_until(lambda: self.clock.pending_sleeps == 1)
            original = self.store.renew_grant

            async def unavailable(*args, **kwargs):
                raise SessionStorageError("private PG failure")

            try:
                if failure == "redis":
                    self.leases.clear()
                else:
                    self.store.renew_grant = unavailable
                self.clock.advance(10)
                report = await task
                self.assertEqual(report.attempts[0].status, "retry")
                self.assertEqual(
                    (await self.store.load_execution(sid, "r")).state, "running"
                )
            finally:
                self.store.renew_grant = original
            self.providers[sid, "r"] = FakeProvider(
                [[TextDelta("recovered"), Finish()]]
            )
            await self.worker("B").run_once()
            self.assertEqual(
                (await self.store.load_execution(sid, "r")).state, "completed"
            )

    async def test_reconstruction_is_supervised_and_renews_while_factory_waits(self):
        await self.accept()
        entered, proceed = asyncio.Event(), asyncio.Event()

        async def slow_factory(receipt):
            entered.set()
            await proceed.wait()
            return await self.factory(receipt)

        worker = self.worker(
            factory=slow_factory,
            dispatch_settings=RecoveryDispatchSettings(rebuild_timeout=45),
        )
        task = asyncio.create_task(worker.run_once())
        await entered.wait()
        await pump_until(lambda: self.clock.pending_sleeps == 1)
        before = self.store.authority("s").grant.expires_at
        self.clock.advance(10)
        await pump_until(lambda: self.store.authority("s").grant.expires_at > before)
        proceed.set()
        self.assertEqual((await task).attempts[0].status, "completed")
        self.assertEqual(self.store.claims, 1)

    async def test_rebuild_timeout_is_bounded_and_attempt_limit_stops_retry(self):
        await self.accept(max_attempts=1)
        entered = asyncio.Event()

        async def blocked(receipt):
            entered.set()
            await asyncio.Event().wait()

        worker = self.worker(
            factory=blocked,
            dispatch_settings=RecoveryDispatchSettings(rebuild_timeout=0.25),
        )
        task = asyncio.create_task(worker.run_once())
        await entered.wait()
        self.clock.advance(0.25)
        self.assertEqual((await task).attempts[0].status, "retry")
        self.clock.advance(0.5)
        report = await worker.run_once()
        self.assertEqual(report.attempts[0].status, "failed")
        self.assertEqual(
            (await self.store.load_execution("s", "r")).error_code,
            "attempt_budget_exhausted",
        )
        self.assertEqual((await worker.run_once()).attempts, ())

    async def test_steering_and_later_follow_up_survive_recovery_runtime_close(self):
        await self.accept()
        entered, proceed = asyncio.Event(), asyncio.Event()

        class GatedProvider(FakeProvider):
            async def stream(self, messages, tools=(), *, context):
                if not self.requests:
                    entered.set()
                    await proceed.wait()
                async for event in super().stream(messages, tools, context=context):
                    yield event

        provider = GatedProvider(
            [[TextDelta("first"), Finish()], [TextDelta("steered"), Finish()]]
        )
        self.providers["s", "r"] = provider
        worker = self.worker()
        task = asyncio.create_task(worker.run_once())
        await entered.wait()
        runtime = self.runtimes[-1]
        session = await runtime.open_session("s")
        await session.steer("steer")
        follow = await session.follow_up("next")
        await follow.events().aclose()
        proceed.set()
        await task
        self.assertEqual((await self.store.load_execution("s", "r")).state, "completed")
        self.assertEqual(
            (await self.store.load_execution("s", follow.run_id)).state, "queued"
        )
        with self.assertRaises(Exception):
            await follow.result()  # Local handle ends, durable admission remains.
        self.providers["s", follow.run_id] = FakeProvider(
            [[TextDelta("followed"), Finish()]]
        )
        await worker.run_once()
        self.assertEqual(
            [
                m.content
                for m in (await self.store.load("s")).messages
                if m.role == "user"
            ],
            ["hello", "steer", "next"],
        )

    async def test_corrupt_checkpoint_reference_never_dispatches(self):
        from dataclasses import replace

        self.store.fail_after_assistant = True
        rid = await self.interrupted(FakeProvider([[TextDelta("final"), Finish()]]))
        events = self.store._events_by_session["s"]
        events[-1] = replace(events[-1], run_id="foreign")
        provider = FakeProvider([])
        self.providers["s", rid] = provider
        report = await self.worker().run_once()
        self.assertEqual(report.attempts[0].status, "failed")
        self.assertEqual(
            (await self.store.load_execution("s", rid)).error_code, "SessionReplayError"
        )
        self.assertEqual(provider.requests, [])

    async def test_runtime_cleanup_failure_does_not_erase_committed_success(self):
        await self.accept()

        async def cleanup():
            raise RuntimeError("private cleanup diagnostics")

        async def factory(receipt):
            return await self.build_runtime(
                FakeProvider([[TextDelta("done"), Finish()]]),
                receipt.grant.owner_id,
                cleanup=cleanup,
            )

        report = await self.worker(factory=factory).run_once()
        self.assertEqual(report.attempts[0].status, "completed")
        self.assertTrue(
            any(e.kind == "recovery_cleanup_failed" for e in self.events.events)
        )

    async def test_unknown_claim_and_tool_commit_query_original_without_reexecution(
        self,
    ):
        from pi_agent_py.errors import SessionCommitUnknownError

        await self.accept()
        original = self.store.claim_next
        lost = True

        async def unknown(*args, **kwargs):
            nonlocal lost
            receipt = await original(*args, **kwargs)
            if lost:
                lost = False
                raise SessionCommitUnknownError(
                    receipt.session.session_id, args[2].operation_id
                )
            return receipt

        self.store.claim_next = unknown
        calls = []

        async def execute(arguments, context):
            calls.append(context.call.id)
            return ToolOutput("committed")

        self.tools = (
            FunctionTool(ToolDefinition("do", "do", {"type": "object"}), execute),
        )
        self.providers["s", "r"] = FakeProvider(
            [
                [ToolCall("a", "do", {}), Finish("tool_calls")],
                [TextDelta("done"), Finish()],
            ]
        )
        self.store.unknown_after_tool = True
        try:
            report = await self.worker().run_once()
        finally:
            self.store.claim_next = original
        self.assertEqual(report.attempts[0].status, "completed")
        self.assertEqual(calls, ["a"])
        self.assertEqual((await self.store.load_execution("s", "r")).attempt, 1)

    async def test_request_limit_is_not_reset_by_worker(self):
        await self.accept(max_requests=1)
        provider = FakeProvider([[ToolCall("a", "missing", {}), Finish("tool_calls")]])
        self.providers["s", "r"] = provider
        report = await self.worker().run_once()
        self.assertEqual(report.attempts[0].status, "failed")
        self.assertEqual(
            (await self.store.load_execution("s", "r")).error_code,
            "AgentLoopLimitError",
        )
        self.assertEqual(len(provider.requests), 1)

    async def test_late_old_epoch_owner_and_claim_writes_are_rejected(self):
        from dataclasses import replace
        from pi_agent_py.api import (
            ExecutionStepRequest,
            DispatchModel,
            execution_operation,
        )

        await self.accept()
        provider = FakeProvider([[WaitUntilCancelled()]])
        self.providers["s", "r"] = provider
        first = self.worker()
        task = asyncio.create_task(first.run_once())
        await provider.blocked.wait()
        old = self.store.authority("s").grant
        await first.aclose()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.providers["s", "r"] = FakeProvider([[TextDelta("done"), Finish()]])
        await self.worker("B").run_once()
        for grant in (
            old,
            replace(old, owner_id="foreign"),
            replace(old, claim_id="foreign"),
        ):
            request = ExecutionStepRequest(grant, DispatchModel(), (), 1, 1)
            with self.assertRaises(SessionConflictError):
                await self.store.commit_step(
                    grant,
                    execution_operation("zombie", request),
                    DispatchModel(),
                    (),
                    expected_session_version=1,
                    expected_revision=1,
                )

    async def test_heartbeat_exception_is_observed_and_joined(self):
        class BrokenSleep(ManualLeaseClock):
            async def sleep(self, seconds):
                raise RuntimeError("private clock failure")

        self.clock = BrokenSleep()
        await self.accept()
        self.providers["s", "r"] = FakeProvider([[WaitUntilCancelled()]])
        report = await self.worker().run_once()
        self.assertEqual(report.attempts[0].status, "retry")
        self.assertEqual((await self.store.load_execution("s", "r")).state, "running")
        self.assertTrue(any(e.kind == "lease_lost" for e in self.events.events))

    async def test_repeated_close_cancellation_waits_for_owned_runtime_cleanup(self):
        await self.accept()
        closing, proceed = asyncio.Event(), asyncio.Event()
        provider = FakeProvider([[WaitUntilCancelled()]])

        async def cleanup():
            closing.set()
            await proceed.wait()

        async def factory(receipt):
            return await self.build_runtime(
                provider, receipt.grant.owner_id, cleanup=cleanup
            )

        worker = self.worker(factory=factory)
        await worker.start()
        await provider.blocked.wait()
        close = asyncio.create_task(worker.aclose())
        await closing.wait()
        close.cancel()
        await asyncio.sleep(0)
        close.cancel()
        proceed.set()
        with self.assertRaises(asyncio.CancelledError):
            await close
        self.assertEqual(worker.state, "closed")
        self.assertEqual((await self.store.load_execution("s", "r")).state, "running")

    async def test_builder_starts_and_closes_worker_before_owned_resources(self):
        await self.accept()
        provider = FakeProvider([[WaitUntilCancelled()]])
        self.providers["s", "r"] = provider
        config = self.config("host")
        dispatcher = RegisteredRecoveryDispatcher(
            self.store, config, {("profile:v1", "hash"): self.factory}
        )
        released = []

        async def cleanup():
            self.assertEqual(self.clock.pending_sleeps, 0)
            released.append("dispatcher")

        root = await (
            AgentRuntimeBuilder()
            .with_provider(FakeProvider([]))
            .with_session_store(self.store)
            .with_distributed_execution(
                self.store, self.leases, settings=config.settings, clock=self.clock
            )
            .with_recovery(dispatcher, ownership="owned", cleanup=cleanup)
            .with_graceful_shutdown(0)
            .build()
        )
        self.runtimes.append(root)
        await provider.blocked.wait()
        self.assertEqual(root.recovery_worker.state, "running")
        await root.aclose()
        self.assertEqual(root.recovery_worker.state, "closed")
        self.assertEqual(released, ["dispatcher"])

    async def test_fatal_startup_scan_rolls_back_builder_resources(self):
        from pi_agent_py.errors import RuntimeBuildError

        original = self.store.list_candidates

        async def fatal(*args, **kwargs):
            raise RuntimeError("invalid scanner implementation")

        self.store.list_candidates = fatal
        released = []

        async def cleanup():
            released.append("provider")

        config = self.config("host")
        dispatcher = RegisteredRecoveryDispatcher(self.store, config, {})
        try:
            with self.assertRaises(RuntimeBuildError):
                await (
                    AgentRuntimeBuilder()
                    .with_provider(FakeProvider([]), ownership="owned", cleanup=cleanup)
                    .with_session_store(self.store)
                    .with_distributed_execution(
                        self.store,
                        self.leases,
                        settings=config.settings,
                        clock=self.clock,
                    )
                    .with_recovery(dispatcher)
                    .build()
                )
        finally:
            self.store.list_candidates = original
        self.assertEqual(released, ["provider"])

    async def test_close_interrupts_a_blocked_candidate_scan_without_waiting_for_ttl(
        self,
    ):
        entered = asyncio.Event()
        original = self.store.list_candidates

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.store.list_candidates = blocked
        worker = self.worker()
        await worker.start()
        await entered.wait()
        closing = asyncio.create_task(worker.aclose())
        try:
            await pump_until(closing.done)
            await closing
            self.assertEqual(worker.state, "closed")
        finally:
            self.store.list_candidates = original
            if not closing.done():
                self.clock.advance(worker.settings.scan_timeout)
                await closing

    async def test_graceful_shutdown_drains_or_cancels_at_fake_deadline(self):
        for complete in (True, False):
            with self.subTest(complete=complete):
                sid = "grace-" + str(complete)
                await self.accept(sid)
                entered, release = asyncio.Event(), asyncio.Event()

                class Provider(FakeProvider):
                    async def stream(self, messages, tools=(), *, context):
                        entered.set()
                        await release.wait()
                        async for event in super().stream(
                            messages, tools, context=context
                        ):
                            yield event

                self.providers[sid, "r"] = Provider([[TextDelta("done"), Finish()]])
                worker = self.worker(settings=RecoveryWorkerSettings(shutdown_grace=2))
                await worker.start()
                await entered.wait()
                closing = asyncio.create_task(worker.aclose())
                try:
                    await pump_until(lambda: self.clock.pending_timeouts == 1)
                    if complete:
                        release.set()
                    else:
                        self.clock.advance(2)
                    await pump_until(closing.done)
                    await closing
                    record = await self.store.load_execution(sid, "r")
                    self.assertEqual(
                        record.state, "completed" if complete else "running"
                    )
                    self.assertEqual(worker.state, "closed")
                finally:
                    release.set()
                    await closing

    async def test_expected_scan_failure_is_observed_then_polling_recovers(self):
        await self.accept()
        original = self.store.list_candidates
        scans = 0

        async def scanner(*args, **kwargs):
            nonlocal scans
            scans += 1
            if scans == 1:
                raise SessionStorageError("private endpoint detail")
            return await original(*args, **kwargs)

        self.store.list_candidates = scanner
        worker = self.worker()
        try:
            await worker.start()
            await pump_until(lambda: self.clock.pending_sleeps == 1)
            self.clock.advance(1)
            await pump_until(
                lambda: any(e.kind == "recovery_completed" for e in self.events.events)
            )
            self.assertEqual(
                (await self.store.load_execution("s", "r")).state, "completed"
            )
            self.assertIsNone(worker.failure)
            self.assertTrue(
                any(e.kind == "recovery_scan_failed" for e in self.events.events)
            )
            self.assertNotIn("private endpoint", repr(self.events.events))
        finally:
            self.store.list_candidates = original
            await worker.aclose()

    async def test_close_racing_start_does_not_leave_a_pending_start_task(self):
        from pi_agent_py.errors import RuntimeStateError

        worker = self.worker()
        starting = asyncio.create_task(worker.start())
        closing = asyncio.create_task(worker.aclose())
        try:
            await pump_until(lambda: starting.done() and closing.done())
            with self.assertRaises(RuntimeStateError):
                await starting
            await closing
            self.assertEqual(worker.state, "closed")
            self.assertEqual(self.store.claims, 0)
        finally:
            for task in (starting, closing):
                if not task.done():
                    task.cancel()
            await asyncio.gather(starting, closing, return_exceptions=True)
            if worker.state != "closed":
                self.workers.remove(
                    worker
                )  # Failure baseline already cancelled its sole task.
