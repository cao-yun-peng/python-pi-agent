from __future__ import annotations

import asyncio
import unittest
from collections.abc import Sequence

from pi_agent_py import (
    Agent,
    AgentSession,
    ContextPlan,
    ContextPreparationError,
    FakeProvider,
    Finish,
    FullHistoryContextStrategy,
    InMemoryEventSink,
    InMemorySessionStore,
    Message,
    TextDelta,
)


class _LatestMessageStrategy:
    async def prepare(self, messages: Sequence[Message]) -> ContextPlan:
        history = tuple(messages)
        return ContextPlan(
            messages=history[-1:],
            source_message_count=len(history),
            omitted_message_count=max(0, len(history) - 1),
        )


class _FailingStrategy:
    async def prepare(self, messages: Sequence[Message]) -> ContextPlan:
        del messages
        raise ValueError("broken context")


class _BlockingStrategy:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def prepare(self, messages: Sequence[Message]) -> ContextPlan:
        del messages
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _IncorrectCountStrategy:
    async def prepare(self, messages: Sequence[Message]) -> ContextPlan:
        return ContextPlan(
            messages=tuple(messages),
            source_message_count=0,
        )


class ContextStrategyTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_history_strategy_preserves_order(self) -> None:
        messages = (
            Message(role="system", content="be concise"),
            Message(role="user", content="hello"),
        )

        plan = await FullHistoryContextStrategy().prepare(messages)

        self.assertEqual(plan.messages, messages)
        self.assertEqual(plan.source_message_count, 2)
        self.assertEqual(plan.omitted_message_count, 0)

    async def test_projects_provider_context_without_changing_history(self) -> None:
        provider = FakeProvider([[TextDelta("new answer"), Finish()]])
        events = InMemoryEventSink()
        old_messages = (
            Message(role="user", content="old question"),
            Message(role="assistant", content="old answer"),
        )
        agent = Agent(
            provider,
            initial_messages=old_messages,
            context_strategy=_LatestMessageStrategy(),
            event_sink=events,
        )

        result = await agent.run("new question")

        self.assertEqual(
            provider.requests[0],
            (Message(role="user", content="new question"),),
        )
        self.assertEqual(
            result.messages,
            old_messages
            + (
                Message(role="user", content="new question"),
                Message(role="assistant", content="new answer"),
            ),
        )
        prepared = next(
            event for event in events.events if event.kind == "context_prepared"
        )
        self.assertEqual(prepared.channel, "capability")
        self.assertTrue(prepared.payload["run_id"])
        self.assertTrue(str(prepared.payload["turn_id"]).endswith(":turn:1"))
        self.assertEqual(
            {
                key: value
                for key, value in prepared.payload.items()
                if key not in {"run_id", "turn_id"}
            },
            {
                "request_number": 1,
                "source_message_count": 3,
                "selected_message_count": 1,
                "omitted_message_count": 2,
                "token_budget": None,
                "estimated_tokens": None,
            },
        )

    async def test_strategy_failure_is_not_reported_as_provider_failure(self) -> None:
        provider = FakeProvider([[Finish()]])
        events = InMemoryEventSink()
        agent = Agent(
            provider,
            context_strategy=_FailingStrategy(),
            event_sink=events,
        )

        with self.assertRaisesRegex(ContextPreparationError, "strategy failed"):
            await agent.run("remember this")

        self.assertEqual(provider.requests, [])
        self.assertEqual(
            agent.messages,
            (Message(role="user", content="remember this"),),
        )
        self.assertEqual(
            [event.kind for event in events.events[-2:]],
            ["run_failed", "run_settled"],
        )

    async def test_cancellation_reaches_context_strategy(self) -> None:
        strategy = _BlockingStrategy()
        provider = FakeProvider([[Finish()]])
        events = InMemoryEventSink()
        agent = Agent(
            provider,
            context_strategy=strategy,
            event_sink=events,
        )

        task = asyncio.create_task(agent.run("cancel context"))
        await strategy.started.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(provider.requests, [])
        self.assertEqual(
            [event.kind for event in events.events[-2:]],
            ["run_cancelled", "run_settled"],
        )

    async def test_rejects_context_plan_with_incorrect_source_count(self) -> None:
        provider = FakeProvider([[Finish()]])
        agent = Agent(provider, context_strategy=_IncorrectCountStrategy())

        with self.assertRaisesRegex(ContextPreparationError, "does not match"):
            await agent.run("one source message")

        self.assertEqual(provider.requests, [])

    async def test_agent_session_passes_strategy_without_trimming_storage(self) -> None:
        store = InMemorySessionStore()
        await store.append(
            "demo",
            0,
            (
                Message(role="user", content="old question"),
                Message(role="assistant", content="old answer"),
            ),
        )
        provider = FakeProvider([[TextDelta("new answer"), Finish()]])
        session = AgentSession(
            "demo",
            store,
            provider,
            context_strategy=_LatestMessageStrategy(),
        )

        result = await session.run("new question")

        self.assertEqual(
            provider.requests[0],
            (Message(role="user", content="new question"),),
        )
        self.assertEqual(
            [message.content for message in result.messages],
            ["old question", "old answer", "new question", "new answer"],
        )


if __name__ == "__main__":
    unittest.main()
