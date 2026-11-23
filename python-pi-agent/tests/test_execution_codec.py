"""Deterministic execution serialization and command/mutation agreement."""

from dataclasses import replace
import json
import unittest

from pi_agent_py.domain import (
    DurableEventIdentity,
    DurableSessionEvent,
    Message,
    SessionEventContext,
    ToolCall,
)
from pi_agent_py.errors import SessionReplayError, SessionValidationError
from pi_agent_py.execution_domain import (
    AppendExecutionMessages,
    ClaimRun,
    CommitAssistant,
    CommitPrompt,
    CommitToolResults,
    ContinueRun,
    DispatchModel,
    ExecutionGrant,
    ExecutionStepRequest,
    ExhaustRun,
    FinishRun,
    RunEvent,
    RunSubmission,
    RunRecord,
    RunCheckpoint,
)
from pi_agent_py.storage.execution_codec import (
    execution_operation,
    encode_run_event,
    decode_run_event,
    record_data,
    load_record,
    dump,
)
from pi_agent_py.storage.execution_transitions import operation_identity
from pi_agent_py.storage.execution_mutations import plan_execution_mutations


class ExecutionCodecTests(unittest.TestCase):
    def test_mutations_reject_checkpoint_and_actual_message_disagreement(self):
        grant = ExecutionGrant("s", "owner", "claim", 1, 100, run_id="r")
        ref = DurableEventIdentity("s", 1, "s:1")
        record = RunRecord(
            RunSubmission("s", "r", "hello", "key", "p", "h"),
            3,
            1,
            "running",
            RunCheckpoint(
                "awaiting_model", prompt_event=ref, dispatches=1, dispatched=True
            ),
            1,
            1,
        )
        history = (
            DurableSessionEvent("s", 1, "message_committed", Message("user", "hello")),
        )
        assistant = Message(
            "assistant", "", tool_calls=(ToolCall("actual", "read", {}),)
        )
        mutation = AppendExecutionMessages(
            (assistant,), SessionEventContext(run_id="r")
        )
        command = CommitAssistant(
            DurableEventIdentity("s", 2, "s:2"), "tool_calls", ("wrong",)
        )
        with self.assertRaisesRegex(SessionValidationError, "tool calls differ"):
            plan_execution_mutations(history, grant, record, command, (mutation,))
        command = replace(command, tool_call_ids=("actual",))
        with self.assertRaises(SessionValidationError):
            plan_execution_mutations(
                history,
                grant,
                record,
                command,
                (replace(mutation, context=SessionEventContext(run_id="foreign")),),
            )
        with self.assertRaises(SessionValidationError):
            plan_execution_mutations(history, grant, record, None, (mutation,))
        new, snapshot = plan_execution_mutations(
            history, grant, record, command, (mutation,)
        )
        self.assertEqual(snapshot.version, 2)
        self.assertEqual(len(new), 1)
        self.assertEqual(len(history), 1)

    def test_each_command_round_trips_with_strict_schema(self):
        grant = ExecutionGrant("s", "owner", "claim", 1, 100, run_id="r")
        ref = DurableEventIdentity("s", 1, "s:1")
        commands = (
            RunSubmission("s", "r", "hello", "key", "profile", "hash"),
            ClaimRun(grant),
            ExhaustRun(grant),
            CommitPrompt(ref),
            DispatchModel(),
            CommitAssistant(ref, "tool_calls", ("c",)),
            CommitToolResults(("c",)),
            ContinueRun((ref,)),
            FinishRun("completed"),
            FinishRun("failed", "reason"),
        )
        for command in commands:
            with self.subTest(command=type(command).__name__):
                event = RunEvent(
                    "s", "r", 1, 1, operation_identity("op", command), command
                )
                self.assertEqual(decode_run_event(encode_run_event(event)), event)
                raw = json.loads(encode_run_event(event))
                raw["schema_version"] = True
                with self.assertRaises(SessionReplayError):
                    decode_run_event(json.dumps(raw))
                raw["schema_version"] = 1
                raw["command"]["data"]["unexpected"] = "bad"
                with self.assertRaises(SessionReplayError):
                    decode_run_event(json.dumps(raw))

    def test_record_round_trip_and_corruption_rejected(self):
        record = RunRecord(
            RunSubmission("s", "r", "hello", "key", "profile", "hash"), 1, 1
        )
        self.assertEqual(load_record(dump(record_data(record))), record)
        data = record_data(record)
        data["state"] = "invalid"
        with self.assertRaises(SessionReplayError):
            load_record(dump(data))

    def test_fingerprint_covers_mutable_message_content_and_versions(self):
        grant = ExecutionGrant("s", "owner", "claim", 1, 100, run_id="r")
        message = Message("assistant", "text")
        mutation = AppendExecutionMessages((message,), SessionEventContext(run_id="r"))
        command = CommitAssistant(DurableEventIdentity("s", 1, "s:1"), "stop")
        request = ExecutionStepRequest(grant, command, (mutation,), 0, 2)
        original = execution_operation("op", request)
        self.assertNotEqual(
            original, execution_operation("op", replace(request, expected_revision=3))
        )
        altered = replace(mutation, messages=(replace(message, content="other"),))
        self.assertNotEqual(
            original, execution_operation("op", replace(request, mutations=(altered,)))
        )
        self.assertNotEqual(
            original,
            execution_operation("op", replace(request, grant=replace(grant, epoch=2))),
        )
        with self.assertRaises(SessionValidationError):
            ExecutionStepRequest(grant, command, [], 0, 2)
