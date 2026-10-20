from __future__ import annotations

import asyncio
import unittest

from pi_agent_py import (
    AgentSession,
    CompactionError,
    FakeCompactionStrategy,
    FakeProvider,
    Finish,
    InMemoryEventSink,
    InMemorySessionStore,
    Message,
    ProviderError,
    TextDelta,
    WaitUntilCancelled,
)


class AgentSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_loads_history_and_persists_each_run(self) -> None:
        provider = FakeProvider(
            [
                [TextDelta("first answer"), Finish(reason="stop")],
                [TextDelta("second answer"), Finish(reason="stop")],
            ]
        )
        store = InMemorySessionStore()
        session = AgentSession(
            "demo",
            store,
            provider,
            system_prompt="You are helpful.",
        )

        first = await session.run("first question")
        second = await session.run("second question")

        self.assertEqual(
            [message.role for message in first.messages],
            ["system", "user", "assistant"],
        )
        self.assertEqual(
            [message.role for message in second.messages],
            ["system", "user", "assistant", "user", "assistant"],
        )
        self.assertEqual(provider.requests[1], second.messages[:-1])

    async def test_provider_failure_keeps_committed_user_message(self) -> None:
        store = InMemorySessionStore()
        session = AgentSession(
            "demo",
            store,
            FakeProvider([[ProviderError("provider failed")]]),
        )

        with self.assertRaises(ProviderError):
            await session.run("remember this")

        snapshot = await store.load("demo")
        self.assertEqual(
            [(message.role, message.content) for message in snapshot.messages],
            [("user", "remember this")],
        )

    async def test_cancellation_keeps_committed_user_message(self) -> None:
        provider = FakeProvider([[WaitUntilCancelled()]])
        store = InMemorySessionStore()
        session = AgentSession("demo", store, provider)

        task = asyncio.create_task(session.run("remember this"))
        await provider.blocked.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        snapshot = await store.load("demo")
        self.assertEqual(
            [(message.role, message.content) for message in snapshot.messages],
            [("user", "remember this")],
        )

    async def test_compacts_history_before_the_next_run(self) -> None:
        store = InMemorySessionStore()
        await store.append(
            "demo",
            0,
            [
                Message(role="system", content="Be helpful."),
                Message(role="user", content="old question"),
                Message(role="assistant", content="old answer"),
                Message(role="user", content="recent question"),
                Message(role="assistant", content="recent answer"),
            ],
        )
        provider = FakeProvider([[TextDelta("new answer"), Finish()]])
        strategy = FakeCompactionStrategy(["Old topic was resolved."])
        events = InMemoryEventSink()
        session = AgentSession(
            "demo",
            store,
            provider,
            compaction_strategy=strategy,
            event_sink=events,
        )

        compacted = await session.compact(keep_recent_turns=1)
        result = await session.run("new question")

        self.assertEqual(compacted.version, 6)
        self.assertEqual(
            [message.content for message in compacted.messages],
            [
                "Be helpful.",
                "<conversation-summary>\nOld topic was resolved.\n"
                "</conversation-summary>",
                "recent question",
                "recent answer",
            ],
        )
        self.assertEqual(strategy.requests[0][0].content, "old question")
        self.assertEqual(provider.requests[0], result.messages[:-1])
        self.assertIn(
            "compaction_completed",
            [event.kind for event in events.events],
        )

    async def test_compaction_failure_does_not_append_an_event(self) -> None:
        store = InMemorySessionStore()
        original = await store.append(
            "demo",
            0,
            [
                Message(role="user", content="old"),
                Message(role="assistant", content="answer"),
                Message(role="user", content="recent"),
            ],
        )
        events = InMemoryEventSink()
        session = AgentSession(
            "demo",
            store,
            FakeProvider([]),
            compaction_strategy=FakeCompactionStrategy(
                [CompactionError("cannot summarize")]
            ),
            event_sink=events,
        )

        with self.assertRaises(CompactionError):
            await session.compact()

        self.assertEqual(await store.load("demo"), original)
        self.assertEqual(
            [event.kind for event in events.events],
            ["compaction_started", "compaction_failed"],
        )

    async def test_compaction_cancellation_does_not_append_an_event(self) -> None:
        store = InMemorySessionStore()
        original = await store.append(
            "demo",
            0,
            [
                Message(role="user", content="old"),
                Message(role="assistant", content="answer"),
                Message(role="user", content="recent"),
            ],
        )
        strategy = FakeCompactionStrategy([WaitUntilCancelled()])
        events = InMemoryEventSink()
        session = AgentSession(
            "demo",
            store,
            FakeProvider([]),
            compaction_strategy=strategy,
            event_sink=events,
        )

        task = asyncio.create_task(session.compact())
        await strategy.blocked.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(await store.load("demo"), original)
        self.assertEqual(
            [event.kind for event in events.events],
            ["compaction_started", "compaction_cancelled"],
        )


if __name__ == "__main__":
    unittest.main()
