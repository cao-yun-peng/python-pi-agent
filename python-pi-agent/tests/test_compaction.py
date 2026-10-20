from __future__ import annotations

import asyncio
import unittest
from collections.abc import Sequence

from pi_agent_py import (
    CompactionError,
    CompactionNotNeededError,
    FakeProvider,
    Finish,
    Message,
    ModelCompactionStrategy,
    TextDelta,
    TokenThresholdCompactionPolicy,
    ToolCall,
    WaitUntilCancelled,
    build_summary_message,
    prepare_compaction,
)


class MessageCountEstimator:
    def estimate(self, messages: Sequence[Message]) -> int:
        return len(messages)


class InvalidEstimator:
    def estimate(self, messages: Sequence[Message]) -> int:
        return -1


class CompactionPreparationTests(unittest.TestCase):
    def test_selects_complete_old_turns_and_keeps_recent_turn(self) -> None:
        history = (
            Message(role="system", content="Be precise."),
            Message(role="user", content="weather?"),
            Message(
                role="assistant",
                content="",
                tool_calls=(ToolCall("call-1", "weather", {"city": "成都"}),),
            ),
            Message(
                role="tool",
                content="sunny",
                tool_call_id="call-1",
                tool_name="weather",
            ),
            Message(role="assistant", content="It is sunny."),
            Message(role="user", content="what should I wear?"),
            Message(role="assistant", content="A light jacket."),
        )

        preparation = prepare_compaction(history, keep_recent_turns=1)

        self.assertEqual(preparation.replace_start, 1)
        self.assertEqual(preparation.source_message_count, 7)
        self.assertEqual(preparation.messages_to_summarize, history[1:5])
        self.assertEqual(preparation.kept_messages, history[5:])

    def test_rejects_history_without_an_older_turn(self) -> None:
        with self.assertRaises(CompactionNotNeededError):
            prepare_compaction(
                [Message(role="user", content="only turn")],
                keep_recent_turns=1,
            )


class TokenThresholdCompactionPolicyTests(unittest.TestCase):
    def test_triggers_when_estimate_reaches_threshold(self) -> None:
        policy = TokenThresholdCompactionPolicy(
            2,
            keep_recent_turns=1,
            estimator=MessageCountEstimator(),
        )

        below = policy.evaluate([Message(role="user", content="one")])
        reached = policy.evaluate(
            [
                Message(role="user", content="one"),
                Message(role="assistant", content="two"),
            ]
        )

        self.assertFalse(below.should_compact)
        self.assertTrue(reached.should_compact)
        self.assertEqual(reached.estimated_tokens, 2)
        self.assertEqual(reached.token_threshold, 2)
        self.assertEqual(reached.keep_recent_turns, 1)

    def test_rejects_an_invalid_estimator_result(self) -> None:
        policy = TokenThresholdCompactionPolicy(
            10,
            estimator=InvalidEstimator(),
        )

        with self.assertRaisesRegex(CompactionError, "non-negative integer"):
            policy.evaluate([Message(role="user", content="hello")])


class ModelCompactionStrategyTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_a_summary_without_exposing_tools(self) -> None:
        provider = FakeProvider(
            [[TextDelta("Goal: learn durable replay."), Finish()]]
        )
        strategy = ModelCompactionStrategy(provider)

        summary = await strategy.summarize(
            [Message(role="user", content="Teach me replay.")]
        )

        self.assertEqual(summary, "Goal: learn durable replay.")
        self.assertEqual(provider.requested_tools, [()])
        self.assertIn("[User]\nTeach me replay.", provider.requests[0][1].content)

    async def test_rejects_tool_calls_from_summary_provider(self) -> None:
        strategy = ModelCompactionStrategy(
            FakeProvider(
                [[ToolCall("call-1", "write_file", {}), Finish("tool_calls")]]
            )
        )

        with self.assertRaisesRegex(CompactionError, "must not call tools"):
            await strategy.summarize([Message(role="user", content="old")])

    async def test_propagates_cancellation(self) -> None:
        provider = FakeProvider([[WaitUntilCancelled()]])
        task = asyncio.create_task(
            ModelCompactionStrategy(provider).summarize(
                [Message(role="user", content="old")]
            )
        )
        await provider.blocked.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

    def test_builds_a_distinguishable_model_visible_summary(self) -> None:
        message = build_summary_message("The user chose JSONL.")

        self.assertEqual(message.role, "user")
        self.assertEqual(
            message.content,
            "<conversation-summary>\nThe user chose JSONL.\n"
            "</conversation-summary>",
        )


if __name__ == "__main__":
    unittest.main()
