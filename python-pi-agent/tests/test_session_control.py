from __future__ import annotations

import asyncio
import unittest
from collections.abc import AsyncIterator, Sequence

from pi_agent_py import (
    AgentSession,
    FakeCompactionStrategy,
    FakeProvider,
    Finish,
    FunctionTool,
    InMemorySessionStore,
    JSONObject,
    Message,
    SessionControlError,
    TextDelta,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
    WaitUntilCancelled,
)
from pi_agent_py.domain import ModelStreamEvent


class GatedProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[tuple[Message, ...]] = []

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ModelStreamEvent]:
        del tools
        self.requests.append(tuple(messages))
        if len(self.requests) == 1:
            self.started.set()
            await self.release.wait()
            yield TextDelta("first answer")
        else:
            yield TextDelta("follow-up answer")
        yield Finish()


class AgentSessionControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_abort_cancels_active_run_and_waits_for_idle(self) -> None:
        provider = FakeProvider(
            [
                [WaitUntilCancelled()],
                [TextDelta("continued"), Finish()],
            ]
        )
        store = InMemorySessionStore()
        session = AgentSession("demo", store, provider)

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
        session = AgentSession("demo", store, provider)

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
            if event.message is not None
            and event.message.content == "change direction"
        )
        self.assertEqual(steered.provenance.source, "user")
        self.assertEqual(steered.provenance.actor_id, "steer")
        self.assertEqual(steered.run_id, events[0].run_id)
        self.assertTrue((steered.turn_id or "").endswith(":turn:2"))

    async def test_steer_rejects_an_idle_session(self) -> None:
        session = AgentSession(
            "demo",
            InMemorySessionStore(),
            FakeProvider([]),
        )

        with self.assertRaisesRegex(SessionControlError, "active run"):
            await session.steer("too late")

    async def test_abort_drops_queued_follow_ups_by_default(self) -> None:
        provider = FakeProvider([[WaitUntilCancelled()]])
        session = AgentSession("demo", InMemorySessionStore(), provider)
        task = asyncio.create_task(session.run("active"))
        await provider.blocked.wait()
        await session.follow_up("queued")

        await session.abort()

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(session.pending_follow_up_count, 0)
        self.assertTrue(session.is_idle)
        self.assertEqual(len(provider.requests), 1)

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
            FakeProvider([]),
            compaction_strategy=strategy,
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
