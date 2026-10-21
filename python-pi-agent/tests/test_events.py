from __future__ import annotations

import unittest

from pi_agent_py import (
    Agent,
    AgentEvent,
    AgentSession,
    BufferedEventSink,
    CallbackEventSink,
    EventRedactionPolicy,
    FakeProvider,
    Finish,
    InMemorySessionStore,
    PluginHost,
    ProviderError,
    RedactingEventSink,
    TextDelta,
    redact_event,
)


class _ExplodingEventSink:
    def emit(self, event: AgentEvent) -> None:
        del event
        raise RuntimeError("do not copy exception messages into diagnostics: secret")


class EventIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sink_failure_does_not_change_a_successful_agent_result(self) -> None:
        agent = Agent(
            FakeProvider([[TextDelta("answer"), Finish()]]),
            event_sink=_ExplodingEventSink(),
        )

        result = await agent.run("question")

        self.assertEqual(result.message.content, "answer")
        self.assertGreater(len(agent.event_sink_failures), 0)
        self.assertTrue(
            all(
                failure.error_type == "RuntimeError"
                for failure in agent.event_sink_failures
            )
        )
        self.assertFalse(
            any(
                "secret" in repr(failure)
                for failure in agent.event_sink_failures
            )
        )

    async def test_settled_sink_failure_does_not_mask_provider_error(self) -> None:
        agent = Agent(
            FakeProvider([[ProviderError("provider is unavailable")]]),
            event_sink=_ExplodingEventSink(),
        )

        with self.assertRaisesRegex(ProviderError, "provider is unavailable"):
            await agent.run("question")

        self.assertEqual(
            agent.event_sink_failures[-1].event_kind,
            "run_settled",
        )

    async def test_session_and_plugin_host_share_the_same_observer_boundary(self) -> None:
        sink = _ExplodingEventSink()
        session = AgentSession(
            "session-1",
            InMemorySessionStore(),
            FakeProvider([[TextDelta("answer"), Finish()]]),
            event_sink=sink,
        )
        host = PluginHost(event_sink=sink)

        result = await session.run("question")
        handle = await host.load("empty", lambda api: None)

        self.assertEqual(result.message.content, "answer")
        self.assertTrue(host.has_plugin("empty"))
        self.assertGreater(len(session.event_sink_failures), 0)
        self.assertGreater(len(host.event_sink_failures), 0)
        await handle.close()
        await host.close()


class EventRedactionTests(unittest.TestCase):
    def test_redacts_model_visible_fields_and_secret_keys_without_mutation(self) -> None:
        original = AgentEvent(
            kind="message_committed",
            channel="durable",
            payload={
                "role": "assistant",
                "content": "private answer",
                "thinking": "private chain of thought",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "lookup",
                        "arguments": {
                            "query": "private query",
                            "api_key": "sk-test",
                        },
                    }
                ],
                "structured_content": {
                    "password": "correct horse battery staple",
                },
                "total_tokens": 42,
            },
        )

        redacted = redact_event(original)

        self.assertEqual(redacted.payload["content"], "<redacted>")
        self.assertEqual(redacted.payload["thinking"], "<redacted>")
        calls = redacted.payload["tool_calls"]
        self.assertIsInstance(calls, list)
        self.assertEqual(calls[0]["arguments"], "<redacted>")
        self.assertEqual(redacted.payload["structured_content"], "<redacted>")
        self.assertEqual(redacted.payload["total_tokens"], 42)
        self.assertEqual(original.payload["content"], "private answer")
        self.assertEqual(
            original.payload["tool_calls"][0]["arguments"]["api_key"],
            "sk-test",
        )

    def test_policy_can_keep_content_while_still_redacting_secret_keys(self) -> None:
        event = AgentEvent(
            kind="message_committed",
            channel="durable",
            payload={
                "content": "safe-to-log",
                "structured_content": {"access_token": "token-value"},
            },
        )
        policy = EventRedactionPolicy(
            redact_message_content=False,
            redact_structured_content=False,
        )

        redacted = redact_event(event, policy)

        self.assertEqual(redacted.payload["content"], "safe-to-log")
        self.assertEqual(
            redacted.payload["structured_content"]["access_token"],
            "<redacted>",
        )

    def test_stream_text_and_tool_arguments_are_redacted(self) -> None:
        cases = (
            (
                AgentEvent("text_delta", "live", {"text": "private"}),
                "text",
            ),
            (
                AgentEvent(
                    "tool_call_received",
                    "capability",
                    {"arguments": {"query": "private"}},
                ),
                "arguments",
            ),
        )
        for event, field in cases:
            with self.subTest(kind=event.kind):
                self.assertEqual(redact_event(event).payload[field], "<redacted>")


class BufferedEventSinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivers_in_order_and_reports_consumer_failure(self) -> None:
        received: list[str] = []

        def consume(event: AgentEvent) -> None:
            received.append(event.kind)
            if event.kind == "text_delta":
                raise OSError("logger unavailable")

        buffered = BufferedEventSink(CallbackEventSink(consume), max_queue_size=4)
        buffered.emit(AgentEvent("run_started", "live"))
        buffered.emit(AgentEvent("text_delta", "live", {"text": "secret"}))
        buffered.emit(AgentEvent("run_settled", "live"))

        await buffered.flush()

        self.assertEqual(
            received,
            ["run_started", "text_delta", "run_settled"],
        )
        self.assertEqual(len(buffered.failures), 1)
        self.assertEqual(buffered.failures[0].stage, "deliver")
        self.assertEqual(buffered.failures[0].error_type, "OSError")
        await buffered.aclose()

    async def test_redaction_happens_before_an_event_is_queued_for_logging(self) -> None:
        received: list[AgentEvent] = []
        buffered = BufferedEventSink(CallbackEventSink(received.append))
        logging_sink = RedactingEventSink(buffered)

        logging_sink.emit(
            AgentEvent(
                "message_committed",
                "durable",
                {"content": "secret prompt", "thinking": "secret thought"},
            )
        )
        await buffered.aclose()

        self.assertEqual(received[0].payload["content"], "<redacted>")
        self.assertEqual(received[0].payload["thinking"], "<redacted>")

    async def test_emit_after_close_is_dropped_without_raising(self) -> None:
        buffered = BufferedEventSink(CallbackEventSink(lambda event: None))
        await buffered.aclose()

        buffered.emit(AgentEvent("run_started", "live"))

        self.assertEqual(len(buffered.failures), 1)
        self.assertEqual(buffered.failures[0].stage, "enqueue")
        self.assertTrue(buffered.failures[0].dropped)


class BufferedEventSinkBackpressureTests(unittest.TestCase):
    def test_full_queue_drops_event_and_records_a_bounded_diagnostic(self) -> None:
        buffered = BufferedEventSink(
            CallbackEventSink(lambda event: None),
            max_queue_size=1,
        )

        buffered.emit(AgentEvent("run_started", "live"))
        buffered.emit(AgentEvent("run_settled", "live"))

        self.assertEqual(len(buffered.failures), 1)
        self.assertEqual(buffered.failures[0].event_kind, "run_settled")
        self.assertEqual(buffered.failures[0].error_type, "QueueFull")
        self.assertTrue(buffered.failures[0].dropped)


if __name__ == "__main__":
    unittest.main()
