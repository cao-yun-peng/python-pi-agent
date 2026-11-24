"""Versioned execution journal codec and full-request operation fingerprints."""

from dataclasses import asdict
from hashlib import sha256
import json
from typing import Literal, cast

from ..domain import DurableEventIdentity
from ..errors import SessionReplayError, SessionValidationError
from ..execution_domain import (
    ClaimRun,
    CommitAssistant,
    CommitPrompt,
    CommitToolResults,
    RepairToolTail,
    ContinueRun,
    DispatchModel,
    ExecutionGrant,
    ExecutionRequest,
    ExhaustRun,
    FinishRun,
    OperationIdentity,
    RunCheckpoint,
    RunCommand,
    RunEvent,
    RunRecord,
    RunSubmission,
    CheckpointPhase,
    GrantPurpose,
    RunState,
)
from ._codec import (
    _require_object,
    _require_exact_keys,
    _require_int,
    _require_string,
    _require_bool,
    _require_array,
)


def dump(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (ValueError, TypeError) as error:
        raise SessionValidationError("execution value is not finite JSON") from error


def execution_operation(
    operation_id: str, request: ExecutionRequest
) -> OperationIdentity:
    """Full request, including mutation type tags; never trust caller's digest alone."""
    data = asdict(request)
    # asdict erases type tags for nested mutations/commands; keep them explicitly.
    from ..execution_domain import ExecutionStepRequest

    if isinstance(request, ExecutionStepRequest):
        data["command"] = command_record(request.command) if request.command else None
        data["mutations"] = [
            {"kind": type(m).__name__, "data": asdict(m)} for m in request.mutations
        ]
    return OperationIdentity(
        operation_id,
        sha256(
            dump({"kind": type(request).__name__, "data": data}).encode("utf-8")
        ).hexdigest(),
    )


def command_record(command: RunCommand) -> dict[str, object]:
    return {"kind": type(command).__name__, "data": asdict(command)}


def _object(value: object, keys: str) -> dict[str, object]:
    data = _require_object(value, "execution")
    _require_exact_keys(data, set(keys.split()), "execution")
    return data


def _s(value: object) -> str:
    return _require_string(value, "execution string")


def _i(value: object) -> int:
    return _require_int(value, "execution integer")


def _optional_s(value: object) -> str | None:
    return None if value is None else _s(value)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("execution time must be a number")
    return float(value)


def decode_identity(value: object) -> DurableEventIdentity:
    data = _object(value, "session_id sequence event_id")
    return DurableEventIdentity(
        _s(data["session_id"]), _i(data["sequence"]), _s(data["event_id"])
    )


def decode_grant(value: object) -> ExecutionGrant:
    data = _object(
        value,
        "session_id owner_id claim_id epoch expires_at purpose run_id maintenance",
    )
    return ExecutionGrant(
        _s(data["session_id"]),
        _s(data["owner_id"]),
        _s(data["claim_id"]),
        _i(data["epoch"]),
        _number(data["expires_at"]),
        cast(GrantPurpose, _s(data["purpose"])),
        _optional_s(data["run_id"]),
        cast(Literal["compact"] | None, _optional_s(data["maintenance"])),
    )


def _submission(value: object) -> RunSubmission:
    data = _object(
        value,
        "session_id run_id prompt submission_key recovery_spec_ref recovery_spec_hash delivery control_id max_requests max_dispatches max_attempts",
    )
    return RunSubmission(
        _s(data["session_id"]),
        _s(data["run_id"]),
        _s(data["prompt"]),
        _s(data["submission_key"]),
        _s(data["recovery_spec_ref"]),
        _s(data["recovery_spec_hash"]),
        cast(Literal["prompt", "follow_up"], _s(data["delivery"])),
        _optional_s(data["control_id"]),
        _i(data["max_requests"]),
        _i(data["max_dispatches"]),
        _i(data["max_attempts"]),
    )


def decode_command(value: object) -> RunCommand:
    tagged = _object(value, "kind data")
    kind, value = _s(tagged["kind"]), tagged["data"]
    if kind == "RunSubmission":
        return _submission(value)
    if kind in ("ClaimRun", "ExhaustRun"):
        data = _object(value, "grant")
        return (
            ClaimRun(decode_grant(data["grant"]))
            if kind == "ClaimRun"
            else ExhaustRun(decode_grant(data["grant"]))
        )
    if kind == "CommitPrompt":
        return CommitPrompt(decode_identity(_object(value, "event")["event"]))
    if kind == "DispatchModel":
        _object(value, "")
        return DispatchModel()
    if kind == "CommitAssistant":
        data = _object(value, "event finish_reason tool_call_ids")
        from ..domain import FinishReason

        return CommitAssistant(
            decode_identity(data["event"]),
            cast(FinishReason, _s(data["finish_reason"])),
            tuple(_s(v) for v in _require_array(data["tool_call_ids"], "calls")),
        )
    if kind in ("CommitToolResults", "RepairToolTail"):
        data = _object(value, "tool_call_ids")
        cls = CommitToolResults if kind == "CommitToolResults" else RepairToolTail
        return cls(
            tuple(_s(v) for v in _require_array(data["tool_call_ids"], "calls"))
        )
    if kind == "ContinueRun":
        data = _object(value, "steering_events")
        return ContinueRun(
            tuple(
                decode_identity(v)
                for v in _require_array(data["steering_events"], "steering")
            )
        )
    if kind == "FinishRun":
        data = _object(value, "state error_code")
        return FinishRun(
            cast(Literal["completed", "failed", "cancelled"], _s(data["state"])),
            _optional_s(data["error_code"]),
        )
    raise ValueError("unknown execution command")


def record_data(record: RunRecord) -> dict[str, object]:
    return asdict(record)


def decode_record(value: object) -> RunRecord:
    data = _object(
        value,
        "submission revision queue_order state checkpoint attempt epoch error_code",
    )
    cp = _object(
        data["checkpoint"],
        "phase request_number dispatches prompt_event assistant_event finish_reason unresolved_tool_ids dispatched requires_continuation",
    )
    from ..domain import FinishReason

    checkpoint = RunCheckpoint(
        cast(CheckpointPhase, _s(cp["phase"])),
        _i(cp["request_number"]),
        _i(cp["dispatches"]),
        decode_identity(cp["prompt_event"]) if cp["prompt_event"] is not None else None,
        decode_identity(cp["assistant_event"])
        if cp["assistant_event"] is not None
        else None,
        cast(FinishReason | None, _optional_s(cp["finish_reason"])),
        tuple(_s(v) for v in _require_array(cp["unresolved_tool_ids"], "calls")),
        _require_bool(cp["dispatched"], "dispatched"),
        _require_bool(cp["requires_continuation"], "continuation"),
    )
    return RunRecord(
        _submission(data["submission"]),
        _i(data["revision"]),
        _i(data["queue_order"]),
        cast(RunState, _s(data["state"])),
        checkpoint,
        _i(data["attempt"]),
        _i(data["epoch"]),
        _optional_s(data["error_code"]),
    )


def encode_run_event(event: RunEvent) -> str:
    return dump(
        {
            "schema_version": 1,
            "session_id": event.session_id,
            "run_id": event.run_id,
            "revision": event.revision,
            "queue_order": event.queue_order,
            "operation": asdict(event.operation),
            "command": command_record(event.command),
        }
    )


def decode_run_event(line: str) -> RunEvent:
    try:
        data = _object(
            json.loads(line),
            "schema_version session_id run_id revision queue_order operation command",
        )
        if _i(data["schema_version"]) != 1:
            raise ValueError("unsupported run schema version")
        op = _object(data["operation"], "operation_id request_digest")
        return RunEvent(
            _s(data["session_id"]),
            _s(data["run_id"]),
            _i(data["revision"]),
            _i(data["queue_order"]),
            OperationIdentity(_s(op["operation_id"]), _s(op["request_digest"])),
            decode_command(data["command"]),
        )
    except (ValueError, TypeError, KeyError, SessionValidationError) as error:
        raise SessionReplayError("invalid execution journal event") from error


def load_record(line: str) -> RunRecord:
    try:
        return decode_record(json.loads(line))
    except (ValueError, TypeError, KeyError, SessionValidationError) as error:
        raise SessionReplayError("invalid execution projection") from error
