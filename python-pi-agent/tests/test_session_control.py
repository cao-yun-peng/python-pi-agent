from __future__ import annotations

import asyncio
import unittest
from collections.abc import AsyncIterator, Sequence
from sqlite3 import OperationalError

from pi_agent_py import (
    AgentComponents,
    CompactionComponents,
    AgentSession,
    FakeCompactionStrategy,
    FakeProvider,
    Finish,
    FunctionTool,
    InMemoryEventSink,
    InMemorySessionStore,
    JSONObject,
    Message,
    ProviderRequestContext,
    QueuedControl,
    SessionControlError,
    SessionEventContext,
    SessionSnapshot,
    SessionStorageError,
    TextDelta,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
    WaitUntilCancelled,
)
from pi_agent_py.domain import ControlTransition, ModelStreamEvent, SessionEventKind


class GatedProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[tuple[Message, ...]] = []

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
        *,
        context: ProviderRequestContext,
    ) -> AsyncIterator[ModelStreamEvent]:
        del tools, context
        self.requests.append(tuple(messages))
        if len(self.requests) == 1:
            self.started.set()
            await self.release.wait()
            yield TextDelta("first answer")
        else:
            yield TextDelta("follow-up answer")
        yield Finish()


class FailingControlStore(InMemorySessionStore):
    def __init__(self, *failing_kinds: SessionEventKind) -> None:
        super().__init__()
        self.failing_kinds = set(failing_kinds)

    async def record_control(
        self,
        session_id: str,
        expected_version: int,
        kind: SessionEventKind,
        control: ControlTransition,
        *,
        event_context: SessionEventContext | None = None,
    ) -> SessionSnapshot:
        if kind in self.failing_kinds:
            raise OperationalError("control write failed")
        return await super().record_control(
            session_id,
            expected_version,
            kind,
            control,
            event_context=event_context,
        )


class AgentSessionControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_abort_cancels_active_run_and_waits_for_idle(self) -> None:
        provider = FakeProvider(
            [
                [WaitUntilCancelled()],
                [TextDelta("continued"), Finish()],
            ]
        )
        store = InMemorySessionStore()
        session = AgentSession("demo", store, AgentComponents(provider))

        task = asyncio.create_task(session.run("long request"))
        await provider.blocked.wait()
        self.assertEqual(session.state, "running")
        self.assertFalse(session.is_idle)
        self.assertIsNotNone(session.active_run_id)

        await session.abort()

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(session.state, "idle")
        self.assertTrue(session.is_idle)
        self.assertIsNone(session.active_run_id)
        self.assertEqual(
            (await store.load("demo")).messages,
            (Message(role="user", content="long request"),),
        )

        result = await session.run("continue")
        self.assertEqual(result.message.content, "continued")

    async def test_follow_up_runs_fifo_after_active_run_settles(self) -> None:
        provider = GatedProvider()
        store = InMemorySessionStore()
        session = AgentSession("demo", store, AgentComponents(provider))

        first = asyncio.create_task(session.run("first"))
        await provider.started.wait()
        follow_up_run_id = await session.follow_up("second")

        self.assertTrue(follow_up_run_id)
        self.assertEqual(session.pending_follow_up_count, 1)
        provider.release.set()
        self.assertEqual((await first).message.content, "first answer")
        await session.wait_until_idle()

        self.assertEqual(session.state, "idle")
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(
            [message.content for message in provider.requests[1]],
            ["first", "first answer", "second"],
        )
        events = await store.read_events("demo")
        self.assertEqual(
            [
                event.kind
                for event in events
                if event.control is not None
            ],
            ["control_enqueued", "control_claimed", "control_delivered"],
        )

    async def test_failed_follow_up_claim_settles_and_dispatcher_keeps_running(
        self,
    ) -> None:
        store = FailingControlStore("control_claimed")
        provider = FakeProvider([[TextDelta("continued"), Finish()]])
        sink = InMemoryEventSink()
        session = AgentSession("demo", store, AgentComponents(provider), event_sink=sink)
        run_id = await session.follow_up("resume after reopening")

        async with asyncio.timeout(2):
            await session.wait_until_idle()
            result = await session.run("next request")

        self.assertEqual(result.message.content, "continued")
        failed, settled = [
            event for event in sink.events if event.envelope.run_id == run_id
        ]
        self.assertEqual((failed.kind, settled.kind), ("run_failed", "run_settled"))
        self.assertEqual(
            failed.payload,
            {"error": "SessionStorageError", "error_category": "storage"},
        )
        pending = (await store.load("demo")).pending_controls
        self.assertEqual([item.control.run_id for item in pending], [run_id])
        self.assertIsNone(pending[0].claim)

    async def test_failed_steer_cleanup_preserves_failure_and_dispatcher(self) -> None:
        store = FailingControlStore("control_claimed", "control_cancelled")
        provider = GatedProvider()
        sink = InMemoryEventSink()
        session = AgentSession("demo", store, AgentComponents(provider), event_sink=sink)
        first = asyncio.create_task(session.run("first request"))
        await provider.started.wait()
        await session.steer("steer this run")
        provider.release.set()

        with self.assertRaises(SessionStorageError) as caught:
            await first

        async with asyncio.timeout(2):
            await session.wait_until_idle()
            result = await session.run("next request")

        self.assertIsInstance(caught.exception.__cause__, OperationalError)
        self.assertEqual(result.message.content, "follow-up answer")
        failed, settled, cleanup = [
            event for event in sink.events
            if event.kind in {"run_failed", "run_settled", "session_control_cleanup_failed"}
        ][:3]
        self.assertEqual(
            (failed.kind, settled.kind, cleanup.kind),
            ("run_failed", "run_settled", "session_control_cleanup_failed"),
        )
        self.assertEqual(cleanup.envelope.run_id, failed.envelope.run_id)
        self.assertEqual(cleanup.payload["error_category"], "storage")
        pending = (await store.load("demo")).pending_controls
        self.assertEqual([item.control.delivery for item in pending], ["steer"])

    async def test_steer_is_committed_at_the_next_safe_turn_boundary(self) -> None:
        tool_started = asyncio.Event()
        release_tool = asyncio.Event()

        async def pause(
            arguments: JSONObject,
            context: ToolExecutionContext,
        ) -> ToolOutput:
            del arguments, context
            tool_started.set()
            await release_tool.wait()
            return ToolOutput(content="tool complete")

        provider = FakeProvider(
            [
                [ToolCall("call-1", "pause", {}), Finish("tool_calls")],
                [TextDelta("steered answer"), Finish()],
            ]
        )
        store = InMemorySessionStore()
        session = AgentSession(
            "demo",
            store,
            AgentComponents(
                provider,
                tools=[
                    FunctionTool(
                        ToolDefinition(
                            name="pause",
                            description="Pause until the test releases the tool.",
                            input_schema={
                                "type": "object",
                                "properties": {},
                                "additionalProperties": False,
                            },
                        ),
                        pause,
                    )
                ],
            ),
        )

        task = asyncio.create_task(session.run("start"))
        await tool_started.wait()
        await session.steer("change direction")
        self.assertEqual(session.pending_steer_count, 1)
        release_tool.set()

        result = await task

        self.assertEqual(result.message.content, "steered answer")
        self.assertEqual(
            [message.content for message in provider.requests[1][-2:]],
            ["tool complete", "change direction"],
        )
        events = await store.read_events("demo")
        steered = next(
            event
            for event in events
            if event.kind == "control_delivered"
            and event.control is not None
        )
        self.assertEqual(steered.provenance.source, "user")
        self.assertEqual(steered.provenance.actor_id, "steer")
        self.assertEqual(steered.run_id, events[0].run_id)
        self.assertTrue((steered.turn_id or "").endswith(":turn:2"))

    async def test_steer_rejects_an_idle_session(self) -> None:
        session = AgentSession("demo", InMemorySessionStore(), AgentComponents(FakeProvider([])))

        with self.assertRaisesRegex(SessionControlError, "active run"):
            await session.steer("too late")

    async def test_abort_drops_queued_follow_ups_by_default(self) -> None:
        provider = FakeProvider([[WaitUntilCancelled()]])
        store = InMemorySessionStore()
        session = AgentSession("demo", store, AgentComponents(provider))
        task = asyncio.create_task(session.run("active"))
        await provider.blocked.wait()
        await session.follow_up("queued")

        await session.abort()

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(session.pending_follow_up_count, 0)
        self.assertTrue(session.is_idle)
        self.assertEqual(len(provider.requests), 1)

        events = await store.read_events("demo")
        self.assertIn("control_cancelled", [event.kind for event in events])

    async def test_restores_a_durable_follow_up_after_reopen(self) -> None:
        store = InMemorySessionStore()
        await store.record_control(
            "demo",
            0,
            "control_enqueued",
            QueuedControl(
                control_id="control-1",
                delivery="follow_up",
                prompt="restored",
                run_id="run-restored",
            ),
        )
        provider = FakeProvider([[TextDelta("done"), Finish()]])
        session = AgentSession("demo", store, AgentComponents(provider))

        await session.wait_until_idle()

        self.assertEqual(
            [message.content for message in (await store.load("demo")).messages],
            ["restored", "done"],
        )
        self.assertEqual((await store.load("demo")).pending_controls, ())

    async def test_restore_cancels_steer_for_a_non_active_target(self) -> None:
        store = InMemorySessionStore()
        await store.record_control(
            "demo",
            0,
            "control_enqueued",
            QueuedControl(
                control_id="steer-1",
                delivery="steer",
                prompt="too late",
                run_id="run-old",
                target_run_id="run-old",
                cause_run_id="run-old",
            ),
        )
        session = AgentSession("demo", store, AgentComponents(FakeProvider([])))

        await session.restore()

        snapshot = await store.load("demo")
        self.assertEqual(snapshot.pending_controls, ())
        self.assertEqual(
            (await store.read_events("demo"))[-1].kind,
            "control_cancelled",
        )

    async def test_abort_cancels_manual_compaction_and_restores_idle(self) -> None:
        store = InMemorySessionStore()
        await store.append(
            "demo",
            0,
            [
                Message(role="user", content="old"),
                Message(role="assistant", content="answer"),
                Message(role="user", content="recent"),
            ],
        )
        strategy = FakeCompactionStrategy([WaitUntilCancelled()])
        session = AgentSession(
            "demo",
            store,
            AgentComponents(FakeProvider([]), compaction=CompactionComponents(strategy)),
        )
        task = asyncio.create_task(session.compact())
        await strategy.blocked.wait()
        self.assertEqual(session.state, "running")

        await session.abort()

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(session.is_idle)


if __name__ == "__main__":
    unittest.main()
