from __future__ import annotations

import unittest
from collections.abc import Sequence

from pi_agent_py import (
    Agent,
    CharacterTokenEstimator,
    ContextBudgetExceededError,
    ContextPreparationError,
    FakeProvider,
    Finish,
    InMemoryEventSink,
    Message,
    TextDelta,
    TokenBudgetContextStrategy,
    ToolCall,
    group_context_messages,
)


class _MessageCountEstimator:
    def estimate(self, messages: Sequence[Message]) -> int:
        return len(messages)


class _InvalidEstimator:
    def estimate(self, messages: Sequence[Message]) -> int:
        del messages
        return -1


class TokenBudgetContextTests(unittest.IsolatedAsyncioTestCase):
    def test_rejects_invalid_budget_and_estimator_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "token_budget"):
            TokenBudgetContextStrategy(True)
        with self.assertRaisesRegex(ValueError, "characters_per_token"):
            CharacterTokenEstimator(characters_per_token=0)
        with self.assertRaisesRegex(ValueError, "per_message_overhead"):
            CharacterTokenEstimator(per_message_overhead=-1)

    def test_groups_a_complete_tool_turn_atomically(self) -> None:
        messages = _tool_history()

        groups = group_context_messages(messages)

        self.assertEqual([len(group) for group in groups], [1, 4, 1])
        self.assertEqual(
            [message.role for message in groups[1]],
            ["user", "assistant", "tool", "assistant"],
        )

    async def test_never_keeps_only_part_of_a_tool_turn(self) -> None:
        messages = _tool_history()
        strategy = TokenBudgetContextStrategy(
            2,
            estimator=_MessageCountEstimator(),
        )

        plan = await strategy.prepare(messages)

        self.assertEqual(
            [(message.role, message.content) for message in plan.messages],
            [("system", "stay safe"), ("user", "new question")],
        )
        self.assertEqual(plan.omitted_message_count, 4)
        self.assertEqual(plan.estimated_tokens, 2)

    async def test_keeps_a_contiguous_suffix_of_complete_turns(self) -> None:
        messages = (
            Message(role="system", content="system"),
            Message(role="user", content="old user"),
            Message(role="assistant", content="old assistant"),
            Message(role="user", content="recent user"),
            Message(role="assistant", content="recent assistant"),
            Message(role="user", content="current user"),
        )
        strategy = TokenBudgetContextStrategy(
            4,
            estimator=_MessageCountEstimator(),
        )

        plan = await strategy.prepare(messages)

        self.assertEqual(
            [message.content for message in plan.messages],
            ["system", "recent user", "recent assistant", "current user"],
        )
        self.assertEqual(plan.omitted_message_count, 2)
        self.assertEqual(plan.token_budget, 4)
        self.assertEqual(plan.estimated_tokens, 4)

    async def test_fails_when_system_and_latest_turn_exceed_budget(self) -> None:
        strategy = TokenBudgetContextStrategy(
            1,
            estimator=_MessageCountEstimator(),
        )

        with self.assertRaises(ContextBudgetExceededError) as raised:
            await strategy.prepare(
                (
                    Message(role="system", content="required"),
                    Message(role="user", content="also required"),
                )
            )

        self.assertEqual(raised.exception.required_tokens, 2)
        self.assertEqual(raised.exception.token_budget, 1)

    async def test_rejects_an_invalid_estimator_result(self) -> None:
        strategy = TokenBudgetContextStrategy(10, estimator=_InvalidEstimator())

        with self.assertRaisesRegex(ContextPreparationError, "non-negative integer"):
            await strategy.prepare((Message(role="user", content="hello"),))

    async def test_agent_event_reports_budgeted_projection(self) -> None:
        provider = FakeProvider([[TextDelta("answer"), Finish()]])
        events = InMemoryEventSink()
        agent = Agent(
            provider,
            initial_messages=(
                Message(role="user", content="old question"),
                Message(role="assistant", content="old answer"),
            ),
            context_strategy=TokenBudgetContextStrategy(
                1,
                estimator=_MessageCountEstimator(),
            ),
            event_sink=events,
        )

        result = await agent.run("current question")

        self.assertEqual(
            provider.requests[0],
            (Message(role="user", content="current question"),),
        )
        self.assertEqual(len(result.messages), 4)
        prepared = next(
            event for event in events.events if event.kind == "context_prepared"
        )
        self.assertEqual(prepared.payload["source_message_count"], 3)
        self.assertEqual(prepared.payload["selected_message_count"], 1)
        self.assertEqual(prepared.payload["omitted_message_count"], 2)
        self.assertEqual(prepared.payload["token_budget"], 1)
        self.assertEqual(prepared.payload["estimated_tokens"], 1)

    def test_character_estimator_counts_model_visible_fields(self) -> None:
        estimator = CharacterTokenEstimator(
            characters_per_token=1,
            per_message_overhead=0,
        )

        tokens = estimator.estimate((Message(role="user", content="ab"),))

        self.assertEqual(tokens, len("user") + len("ab"))

    def test_character_estimator_ignores_programmatic_structured_content(self) -> None:
        estimator = CharacterTokenEstimator(
            characters_per_token=1,
            per_message_overhead=0,
        )
        plain = Message(
            role="tool",
            content="visible",
            tool_call_id="call-1",
            tool_name="lookup",
        )
        structured = Message(
            role="tool",
            content="visible",
            tool_call_id="call-1",
            tool_name="lookup",
            structured_content={"large_programmatic_value": "x" * 1000},
        )

        self.assertEqual(
            estimator.estimate((plain,)),
            estimator.estimate((structured,)),
        )


def _tool_history() -> tuple[Message, ...]:
    call = ToolCall(id="call-1", name="lookup", arguments={"city": "北京"})
    return (
        Message(role="system", content="stay safe"),
        Message(role="user", content="old question"),
        Message(role="assistant", content="", tool_calls=(call,)),
        Message(
            role="tool",
            content="sunny",
            tool_call_id=call.id,
            tool_name=call.name,
        ),
        Message(role="assistant", content="old answer"),
        Message(role="user", content="new question"),
    )


if __name__ == "__main__":
    unittest.main()
