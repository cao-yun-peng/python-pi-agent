"""Schema v6 wire contract, independent of filesystem and database adapters."""

from dataclasses import replace
import json
import unittest

from pi_agent_py.domain import (
    ControlCancellation,
    ControlClaim,
    ControlDelivery,
    DurableSessionEvent,
    EventProvenance,
    Message,
    ProviderState,
    QueuedControl,
    SessionCompaction,
    ToolCall,
)
from pi_agent_py.errors import SessionReplayError, SessionValidationError
from pi_agent_py.storage._codec import _decode_event, _encode_event


class SessionCodecTests(unittest.TestCase):
    def test_schema_v6_complete_message_has_stable_wire_bytes(self) -> None:
        event = DurableSessionEvent(
            "demo",
            1,
            "message_committed",
            Message(
                "assistant",
                "你好",
                thinking="计划",
                tool_calls=(ToolCall("c", "read", {"path": "a"}),),
                provider_state=(ProviderState("p", {"encrypted_content": "opaque"}),),
            ),
            run_id="r",
            turn_id="r:turn:1",
            provenance=EventProvenance("model", "fake"),
        )
        expected = {
            "schema_version": 6,
            "session_id": "demo",
            "sequence": 1,
            "kind": "message_committed",
            "event_id": "demo:1",
            "run_id": "r",
            "turn_id": "r:turn:1",
            "provenance": {
                "source": "model",
                "actor_id": "fake",
                "cause_event_id": None,
                "copied_from_event_id": None,
            },
            "message": {
                "role": "assistant",
                "content": "你好",
                "thinking": "计划",
                "tool_calls": [{"id": "c", "name": "read", "arguments": {"path": "a"}}],
                "tool_call_id": None,
                "tool_name": None,
                "is_error": False,
                "structured_content": None,
                "provider_state": [
                    {"provider": "p", "item": {"encrypted_content": "opaque"}}
                ],
            },
            "compaction": None,
            "control": None,
        }
        encoded = _encode_event(event)
        self.assertEqual(
            encoded,
            json.dumps(
                expected, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ),
        )
        self.assertEqual(_decode_event(encoded, 9), event)

    def test_all_payload_kinds_round_trip_and_do_not_alias_inputs(self) -> None:
        tool = Message(
            "tool",
            "result",
            tool_call_id="c",
            tool_name="read",
            is_error=True,
            structured_content={"nested": [1]},
        )
        events = (
            DurableSessionEvent("s", 1, "message_committed", tool),
            DurableSessionEvent(
                "s",
                2,
                "context_compacted",
                compaction=SessionCompaction(0, 1, Message("user", "summary")),
            ),
            DurableSessionEvent(
                "s",
                3,
                "control_enqueued",
                control=QueuedControl(
                    "q", "follow_up", "next", "r", cause_run_id="cause"
                ),
            ),
            DurableSessionEvent(
                "s", 4, "control_claimed", control=ControlClaim("q", "c", "w", 123.5)
            ),
            DurableSessionEvent(
                "s",
                5,
                "control_delivered",
                control=ControlDelivery("q", "c", Message("user", "next")),
            ),
            DurableSessionEvent(
                "s", 6, "control_cancelled", control=ControlCancellation("q", "abort")
            ),
            DurableSessionEvent(
                "s",
                7,
                "control_enqueued",
                control=QueuedControl("steer", "steer", "redirect", "r", "r", "r"),
            ),
        )
        for event in events:
            with self.subTest(kind=event.kind):
                restored = _decode_event(_encode_event(event), 1)
                self.assertEqual(restored, event)
                self.assertIsNot(restored, event)
        restored = _decode_event(_encode_event(events[0]), 1)
        self.assertIsNot(restored.message.structured_content, tool.structured_content)

    def test_error_contract_keeps_line_context_and_strict_fields(self) -> None:
        base = json.loads(
            _encode_event(
                DurableSessionEvent("s", 1, "message_committed", Message("user", "hi"))
            )
        )
        for mutate, text in (
            (lambda r: r.update(schema_version=5), "unsupported schema_version: 5"),
            (lambda r: r.update(sequence=True), "sequence must be an integer"),
            (lambda r: r.update(extra=1), "event keys mismatch"),
            (lambda r: r["message"].pop("provider_state"), "message keys mismatch"),
            (
                lambda r: r["message"].update(is_error=1),
                "message.is_error must be a boolean",
            ),
            (lambda r: r.update(kind="unknown"), "unsupported event kind"),
        ):
            record = json.loads(json.dumps(base))
            mutate(record)
            with (
                self.subTest(error=text),
                self.assertRaisesRegex(SessionReplayError, f"line 7: {text}"),
            ):
                _decode_event(json.dumps(record), 7)
        for line, text in (
            ("", "empty JSONL record at line 7"),
            ("{", "invalid JSONL record at line 7"),
        ):
            with self.assertRaisesRegex(SessionReplayError, text):
                _decode_event(line, 7)

    def test_unserializable_message_is_validation_error(self) -> None:
        event = DurableSessionEvent(
            "s",
            1,
            "message_committed",
            Message("tool", "hi", tool_call_id="c", tool_name="read"),
        )
        bad_message = replace(event.message, structured_content={"bad": object()})
        with self.assertRaisesRegex(SessionValidationError, "not JSON serializable"):
            _encode_event(replace(event, message=bad_message))


if __name__ == "__main__":
    unittest.main()
