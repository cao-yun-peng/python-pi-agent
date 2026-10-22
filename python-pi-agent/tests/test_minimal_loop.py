from __future__ import annotations

import asyncio
import unittest

from pi_agent_py import (
    Agent,
    FakeProvider,
    Finish,
    InMemoryEventSink,
    Message,
    ModelProtocolError,
    ProviderError,
    TextDelta,
    WaitUntilCancelled,
)


class MinimalAgentLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_text_and_commits_complete_assistant_message(self) -> None:
        provider = FakeProvider(
            [[TextDelta("你"), TextDelta("好"), Finish(reason="stop")]]
        )
        events = InMemoryEventSink()
        agent = Agent(provider, event_sink=events, system_prompt="回答要简洁。")

        result = await agent.run("打个招呼")

        self.assertEqual(result.message, Message(role="assistant", content="你好"))
        self.assertEqual(provider.requests[0][-1], Message(role="user", content="打个招呼"))
        request_context = provider.request_contexts[0]
        self.assertEqual(request_context.purpose, "agent_turn")
        self.assertEqual(request_context.request_number, 1)
        self.assertIsNone(request_context.session_id)
        self.assertTrue(request_context.run_id)
        self.assertTrue((request_context.turn_id or "").endswith(":turn:1"))
        self.assertEqual(
            [event.kind for event in events.events],
            [
                "run_started",
                "message_committed",
                "context_prepared",
                "model_request_started",
                "text_delta",
                "text_delta",
                "model_request_finished",
                "message_committed",
                "run_completed",
                "run_settled",
            ],
        )
        self.assertEqual(
            [event.channel for event in events.events if event.kind == "message_committed"],
            ["durable", "durable"],
        )
        started = next(
            event for event in events.events if event.kind == "model_request_started"
        )
        finished = next(
            event for event in events.events if event.kind == "model_request_finished"
        )
        self.assertEqual(started.envelope.schema_version, 1)
        self.assertEqual(started.envelope.source, "agent")
        self.assertEqual(started.envelope.request_id, request_context.request_id)
        self.assertEqual(started.envelope.span_id, finished.envelope.span_id)
        self.assertIsNotNone(finished.envelope.duration_ms)
        self.assertTrue(all(event.envelope.run_id for event in events.events))
        self.assertNotIn("run_id", started.payload)
        self.assertNotIn("request_id", started.payload)

    async def test_provider_failure_does_not_commit_partial_assistant_message(self) -> None:
        provider = FakeProvider(
            [[TextDelta("未完成"), ProviderError("simulated outage")]]
        )
        events = InMemoryEventSink()
        agent = Agent(provider, event_sink=events)

        with self.assertRaisesRegex(ProviderError, "simulated outage"):
            await agent.run("会失败吗？")

        self.assertEqual(agent.messages, (Message(role="user", content="会失败吗？"),))
        failed = next(
            event for event in events.events if event.kind == "model_request_failed"
        )
        self.assertEqual(failed.payload["error"], "ProviderError")
        self.assertEqual(failed.payload["outcome"], "error")
        self.assertIsNotNone(failed.envelope.duration_ms)
        self.assertEqual(
            [event.kind for event in events.events[-2:]],
            ["run_failed", "run_settled"],
        )

    async def test_missing_finish_is_a_protocol_error(self) -> None:
        provider = FakeProvider([[TextDelta("没有结束事件")]])
        events = InMemoryEventSink()
        agent = Agent(provider, event_sink=events)

        with self.assertRaisesRegex(ModelProtocolError, "without finish"):
            await agent.run("检查协议")

        self.assertEqual(
            [event.kind for event in events.events[-2:]],
            ["run_failed", "run_settled"],
        )

    async def test_cancellation_reaches_provider_and_always_settles(self) -> None:
        provider = FakeProvider([[WaitUntilCancelled()]])
        events = InMemoryEventSink()
        agent = Agent(provider, event_sink=events)

        task = asyncio.create_task(agent.run("等待取消"))
        await provider.blocked.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(
            [event.kind for event in events.events[-2:]],
            ["run_cancelled", "run_settled"],
        )
        self.assertEqual(agent.messages, (Message(role="user", content="等待取消"),))


if __name__ == "__main__":
    unittest.main()
