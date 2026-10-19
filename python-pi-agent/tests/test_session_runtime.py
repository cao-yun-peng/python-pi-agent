from __future__ import annotations

import asyncio
import unittest

from pi_agent_py import (
    AgentSession,
    FakeProvider,
    Finish,
    InMemorySessionStore,
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


if __name__ == "__main__":
    unittest.main()
