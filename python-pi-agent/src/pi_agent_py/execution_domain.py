"""Immutable execution contracts, separate from model-visible session facts."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Literal, TypeAlias

from .domain import (
    ControlCancellation,
    ControlClaim,
    ControlDelivery,
    DurableEventIdentity,
    FinishReason,
    Message,
    QueuedControl,
    SessionCompaction,
    SessionEventContext,
    SessionSnapshot,
)
from .errors import SessionValidationError

RunState: TypeAlias = Literal["queued", "running", "completed", "failed", "cancelled"]
CheckpointPhase: TypeAlias = Literal[
    "before_prompt", "awaiting_model", "awaiting_tools", "awaiting_boundary", "terminal"
]
GrantPurpose: TypeAlias = Literal["run", "maintenance"]


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SessionValidationError(f"{name} must not be empty")


def _integer(value: int, name: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise SessionValidationError(f"{name} must be an integer >= {minimum}")


def _time(value: float, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value < 0
    ):
        raise SessionValidationError(f"{name} must be finite and non-negative")


def _ids(values: tuple[str, ...], name: str) -> None:
    if not isinstance(values, tuple):
        raise SessionValidationError(f"{name} must be an immutable tuple")
    for value in values:
        _text(value, name)
    if len(set(values)) != len(values):
        raise SessionValidationError(f"{name} contains duplicates")


@dataclass(frozen=True, slots=True)
class RunSubmission:
    session_id: str
    run_id: str
    prompt: str
    submission_key: str
    recovery_spec_ref: str
    recovery_spec_hash: str
    delivery: Literal["prompt", "follow_up"] = "prompt"
    control_id: str | None = None
    max_requests: int = 20
    max_dispatches: int = 40
    max_attempts: int = 5

    def __post_init__(self) -> None:
        for name in (
            "session_id",
            "run_id",
            "prompt",
            "submission_key",
            "recovery_spec_ref",
            "recovery_spec_hash",
        ):
            _text(getattr(self, name), name)
        if self.delivery not in ("prompt", "follow_up"):
            raise SessionValidationError("unsupported run delivery")
        if (self.delivery == "follow_up") != (self.control_id is not None):
            raise SessionValidationError("only follow_up requires control_id")
        if self.control_id is not None:
            _text(self.control_id, "control_id")
        for name in ("max_requests", "max_dispatches", "max_attempts"):
            _integer(getattr(self, name), name, 1)


@dataclass(frozen=True, slots=True)
class SessionLease:
    """Redis coordination identity; never an authority to write PG by itself."""

    session_id: str
    owner_id: str
    claim_id: str
    nonce: str
    expires_at: float

    def __post_init__(self) -> None:
        for name in ("session_id", "owner_id", "claim_id", "nonce"):
            _text(getattr(self, name), name)
        _time(self.expires_at, "expires_at")


@dataclass(frozen=True, slots=True)
class ExecutionGrant:
    session_id: str
    owner_id: str
    claim_id: str
    epoch: int
    expires_at: float
    purpose: GrantPurpose = "run"
    run_id: str | None = None
    maintenance: Literal["compact"] | None = None

    def __post_init__(self) -> None:
        for name in ("session_id", "owner_id", "claim_id"):
            _text(getattr(self, name), name)
        _integer(self.epoch, "epoch", 1)
        _time(self.expires_at, "expires_at")
        if self.purpose == "run":
            _text(self.run_id or "", "run_id")
            if self.maintenance is not None:
                raise SessionValidationError("run grant cannot authorize maintenance")
        elif self.purpose == "maintenance":
            if self.run_id is not None or self.maintenance != "compact":
                raise SessionValidationError(
                    "maintenance grant only authorizes compact"
                )
        else:
            raise SessionValidationError("unsupported grant purpose")


@dataclass(frozen=True, slots=True)
class ExecutionAuthority:
    """Session aggregate loaded under a lock; retains epoch and interrupted run."""

    session_id: str
    epoch: int = 0
    grant: ExecutionGrant | None = None
    active_run_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.session_id, "session_id")
        _integer(self.epoch, "epoch")
        if self.active_run_id is not None:
            _text(self.active_run_id, "active_run_id")
        if self.grant is not None:
            if (
                self.grant.session_id != self.session_id
                or self.grant.epoch != self.epoch
            ):
                raise SessionValidationError("grant does not match authority")
            if self.grant.run_id != self.active_run_id:
                raise SessionValidationError("grant does not match active run")


@dataclass(frozen=True, slots=True)
class RunCheckpoint:
    phase: CheckpointPhase = "before_prompt"
    request_number: int = 1
    dispatches: int = 0
    prompt_event: DurableEventIdentity | None = None
    assistant_event: DurableEventIdentity | None = None
    finish_reason: FinishReason | None = None
    unresolved_tool_ids: tuple[str, ...] = ()
    dispatched: bool = False
    requires_continuation: bool = False

    def __post_init__(self) -> None:
        if self.phase not in (
            "before_prompt",
            "awaiting_model",
            "awaiting_tools",
            "awaiting_boundary",
            "terminal",
        ):
            raise SessionValidationError("unknown checkpoint phase")
        _integer(self.request_number, "request_number", 1)
        _integer(self.dispatches, "dispatches")
        _ids(self.unresolved_tool_ids, "unresolved_tool_ids")
        if (
            type(self.dispatched) is not bool
            or type(self.requires_continuation) is not bool
        ):
            raise SessionValidationError("checkpoint flags must be boolean")
        if self.finish_reason not in (None, "stop", "length", "tool_calls"):
            raise SessionValidationError("unknown finish reason")
        if (self.assistant_event is None) != (self.finish_reason is None):
            raise SessionValidationError("assistant reference and Finish must coexist")
        if self.phase == "before_prompt" and (
            self.prompt_event is not None
            or self.assistant_event is not None
            or self.dispatches
            or self.request_number != 1
        ):
            raise SessionValidationError(
                "before_prompt cannot contain committed progress"
            )
        if (
            self.phase not in ("before_prompt", "terminal")
            and self.prompt_event is None
        ):
            raise SessionValidationError("checkpoint requires committed prompt")
        if (
            self.phase in ("awaiting_tools", "awaiting_boundary")
            and self.assistant_event is None
        ):
            raise SessionValidationError("checkpoint requires completed assistant")
        if bool(self.unresolved_tool_ids) != (self.phase == "awaiting_tools"):
            raise SessionValidationError("only awaiting_tools carries unresolved tools")
        if self.dispatched and self.phase != "awaiting_model":
            raise SessionValidationError("dispatch reservation requires awaiting_model")


@dataclass(frozen=True, slots=True)
class OperationIdentity:
    operation_id: str
    request_digest: str

    def __post_init__(self) -> None:
        _text(self.operation_id, "operation_id")
        _text(self.request_digest, "request_digest")
        if len(self.request_digest) != 64 or any(
            c not in "0123456789abcdef" for c in self.request_digest
        ):
            raise SessionValidationError("request_digest must be a SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class ClaimRun:
    grant: ExecutionGrant


@dataclass(frozen=True, slots=True)
class ExhaustRun:
    """Fail a stranded run under a new grant without funding another attempt."""

    grant: ExecutionGrant


@dataclass(frozen=True, slots=True)
class CommitPrompt:
    event: DurableEventIdentity


@dataclass(frozen=True, slots=True)
class DispatchModel:
    """Reserve a physical dispatch before network I/O, including retries."""


@dataclass(frozen=True, slots=True)
class CommitAssistant:
    event: DurableEventIdentity
    finish_reason: FinishReason
    tool_call_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _ids(self.tool_call_ids, "tool_call_ids")
        if self.finish_reason not in ("stop", "length", "tool_calls"):
            raise SessionValidationError("unknown finish reason")


@dataclass(frozen=True, slots=True)
class CommitToolResults:
    tool_call_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _ids(self.tool_call_ids, "tool_call_ids")
        if not self.tool_call_ids:
            raise SessionValidationError("tool results must not be empty")


@dataclass(frozen=True, slots=True)
class ContinueRun:
    steering_events: tuple[DurableEventIdentity, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.steering_events, tuple):
            raise SessionValidationError("steering_events must be a tuple")
        _ids(tuple(event.event_id for event in self.steering_events), "steering_events")


@dataclass(frozen=True, slots=True)
class FinishRun:
    state: Literal["completed", "failed", "cancelled"]
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.state not in ("completed", "failed", "cancelled"):
            raise SessionValidationError("finish requires a terminal state")
        if self.state == "completed" and self.error_code is not None:
            raise SessionValidationError("completed run cannot have an error")
        if self.state != "completed":
            _text(self.error_code or "", "error_code")


RunCommand: TypeAlias = (
    RunSubmission
    | ClaimRun
    | ExhaustRun
    | CommitPrompt
    | DispatchModel
    | CommitAssistant
    | CommitToolResults
    | ContinueRun
    | FinishRun
)


@dataclass(frozen=True, slots=True)
class RunRecord:
    submission: RunSubmission
    revision: int
    queue_order: int
    state: RunState = "queued"
    checkpoint: RunCheckpoint = RunCheckpoint()
    attempt: int = 0
    epoch: int = 0
    error_code: str | None = None

    def __post_init__(self) -> None:
        _integer(self.revision, "revision", 1)
        _integer(self.queue_order, "queue_order", 1)
        _integer(self.attempt, "attempt")
        _integer(self.epoch, "epoch")
        if self.state not in ("queued", "running", "completed", "failed", "cancelled"):
            raise SessionValidationError("unknown run state")
        if (self.state in ("completed", "failed", "cancelled")) != (
            self.checkpoint.phase == "terminal"
        ):
            raise SessionValidationError("terminal run requires terminal checkpoint")
        if (self.attempt == 0) != (self.epoch == 0):
            raise SessionValidationError("attempt and epoch must coexist")
        if self.state == "running" and not self.attempt:
            raise SessionValidationError("running run requires a claim")
        if self.state == "queued" and (
            self.attempt or self.checkpoint != RunCheckpoint()
        ):
            raise SessionValidationError("queued run cannot contain execution progress")
        if (
            self.attempt > self.submission.max_attempts
            or self.checkpoint.dispatches > self.submission.max_dispatches
            or self.checkpoint.request_number > self.submission.max_requests
        ):
            raise SessionValidationError("record exceeds submission budget")
        if self.state in ("failed", "cancelled"):
            _text(self.error_code or "", "error_code")
        elif self.error_code is not None:
            raise SessionValidationError("non-error run cannot carry an error")
        if self.state == "completed" and (
            self.checkpoint.assistant_event is None
            or self.checkpoint.requires_continuation
        ):
            raise SessionValidationError("completed record requires final assistant")
        for event in (self.checkpoint.prompt_event, self.checkpoint.assistant_event):
            if event is not None and event.session_id != self.submission.session_id:
                raise SessionValidationError(
                    "record reference belongs to another session"
                )


@dataclass(frozen=True, slots=True)
class RunEvent:
    session_id: str
    run_id: str
    revision: int
    queue_order: int
    operation: OperationIdentity
    command: RunCommand

    def __post_init__(self) -> None:
        _text(self.session_id, "session_id")
        _text(self.run_id, "run_id")
        _integer(self.revision, "revision", 1)
        _integer(self.queue_order, "queue_order", 1)


@dataclass(frozen=True, slots=True)
class RunTransitionPlan:
    """Persist event before exposing record; event=None is a deduplicated receipt."""

    event: RunEvent | None
    record: RunRecord


@dataclass(frozen=True, slots=True)
class AppendExecutionMessages:
    messages: tuple[Message, ...]
    context: SessionEventContext

    def __post_init__(self) -> None:
        if not isinstance(self.messages, tuple) or not self.messages:
            raise SessionValidationError("execution messages must be a non-empty tuple")


@dataclass(frozen=True, slots=True)
class DeliverExecutionControl:
    delivery: ControlDelivery
    context: SessionEventContext


@dataclass(frozen=True, slots=True)
class RecordExecutionControl:
    """Owner-scoped control bookkeeping without changing the Run checkpoint."""

    kind: Literal["control_enqueued", "control_claimed", "control_cancelled"]
    control: QueuedControl | ControlClaim | ControlCancellation
    context: SessionEventContext

    def __post_init__(self) -> None:
        expected = {
            QueuedControl: "control_enqueued",
            ControlClaim: "control_claimed",
            ControlCancellation: "control_cancelled",
        }.get(type(self.control))
        if self.kind != expected:
            raise SessionValidationError("control mutation kind and payload differ")


@dataclass(frozen=True, slots=True)
class CompactExecutionHistory:
    compaction: SessionCompaction
    context: SessionEventContext


ExecutionMutation: TypeAlias = (
    AppendExecutionMessages
    | DeliverExecutionControl
    | RecordExecutionControl
    | CompactExecutionHistory
)
ProgressCommand: TypeAlias = (
    CommitPrompt | DispatchModel | CommitAssistant | CommitToolResults | ContinueRun
)


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    """Detached atomic result; run is absent for maintenance-only operations."""

    session: SessionSnapshot
    run: RunRecord | None = None
    grant: ExecutionGrant | None = None
