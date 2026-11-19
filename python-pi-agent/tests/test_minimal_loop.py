from __future__ import annotations

import asyncio
import unittest
from pi_agent_py.runtime.loop import _AgentWiring

from pi_agent_py import (
    AgentComponents,
    Agent,
    DurableEventIdentity,
    FakeProvider,
    Finish,
    InMemoryEventSink,
    InternalError,
    Message,
    ModelProtocolError,
    ProviderError,
    ProviderState,
    SessionEventContext,
    TextDelta,
    WaitUntilCancelled,
)


class MinimalAgentLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_text_and_commits_complete_assistant_message(self) -> None:
        provider = FakeProvider(
            [[TextDelta("你"), TextDelta("好"), Finish(reason="stop")]]
        )
        events = InMemoryEventSink()
        agent = Agent(AgentComponents(provider, system_prompt="回答要简洁。"), event_sink=events)

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

    def test_rejects_system_prompt_with_initial_messages(self) -> None:
        components = AgentComponents(FakeProvider([]), system_prompt="be brief")

        with self.assertRaisesRegex(ValueError, "cannot be supplied together"):
            Agent(
                components,
                initial_messages=(Message(role="user", content="earlier"),),
            )

    async def test_provider_failure_does_not_commit_partial_assistant_message(self) -> None:
        provider = FakeProvider(
            [[TextDelta("未完成"), ProviderError("simulated outage")]]
        )
        events = InMemoryEventSink()
        agent = Agent(AgentComponents(provider), event_sink=events)

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
        self.assertEqual(events.events[-2].payload["error_category"], "provider")

    async def test_native_stream_error_is_classified_as_provider_failure(self) -> None:
        original = OSError("provider diagnostics")
        events = InMemoryEventSink()
        agent = Agent(AgentComponents(FakeProvider([[original]])), event_sink=events)

        with self.assertRaises(ProviderError) as caught:
            await agent.run("question")

        self.assertIs(caught.exception.__cause__, original)
        self.assertEqual(events.events[-3].kind, "model_request_failed")
        self.assertEqual(events.events[-3].payload["error"], "ProviderError")
        self.assertEqual(
            events.events[-2].payload,
            {"error": "ProviderError", "error_category": "provider"},
        )

    async def test_unexpected_runtime_error_has_its_own_category(self) -> None:
        original = RuntimeError("runtime diagnostics")

        async def fail_commit(
            message: Message,
            context: SessionEventContext,
        ) -> DurableEventIdentity:
            raise original

        events = InMemoryEventSink()
        agent = Agent._for_session(
            AgentComponents(FakeProvider([])),
            wiring=_AgentWiring(message_committer=fail_commit),
            event_sink=events,
            initial_messages=(),
            session_id="demo",
            next_turn_hook=None,
        )

        with self.assertRaises(InternalError) as caught:
            await agent.run("question")

        self.assertIs(caught.exception.__cause__, original)
        self.assertEqual(
            events.events[-2].payload,
            {"error": "InternalError", "error_category": "internal"},
        )
        self.assertEqual(events.events[-1].kind, "run_settled")

    async def test_commits_provider_state_without_exposing_it_to_observers(self) -> None:
        state = ProviderState(
            provider="timicc-responses",
            item={
                "type": "reasoning",
                "id": "rs_1",
                "encrypted_content": "opaque-secret-state",
            },
        )
        events = InMemoryEventSink()
        agent = Agent(
            AgentComponents(FakeProvider([[state, TextDelta("完成"), Finish()]])),
            event_sink=events,
        )

        result = await agent.run("继续")

        self.assertEqual(result.message.provider_state, (state,))
        committed = [
            event for event in events.events if event.kind == "message_committed"
        ][-1]
        self.assertNotIn("provider_state", committed.payload)
        self.assertNotIn("opaque-secret-state", repr(events.events))

    async def test_missing_finish_is_a_protocol_error(self) -> None:
        provider = FakeProvider([[TextDelta("没有结束事件")]])
        events = InMemoryEventSink()
        agent = Agent(AgentComponents(provider), event_sink=events)

        with self.assertRaisesRegex(ModelProtocolError, "without finish"):
            await agent.run("检查协议")

        self.assertEqual(
            [event.kind for event in events.events[-2:]],
            ["run_failed", "run_settled"],
        )
        self.assertEqual(events.events[-2].payload["error_category"], "protocol")

    async def test_cancellation_reaches_provider_and_always_settles(self) -> None:
        provider = FakeProvider([[WaitUntilCancelled()]])
        events = InMemoryEventSink()
        agent = Agent(AgentComponents(provider), event_sink=events)

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
