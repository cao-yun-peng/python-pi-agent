"""Pure domain values shared by providers, the runtime, and applications."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from pathlib import PurePosixPath
from time import time
from typing import Literal, Mapping, TypeAlias
from uuid import uuid4

from ._metadata import _freeze_metadata

JSONPrimitive: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONPrimitive | list["JSONValue"] | dict[str, "JSONValue"]
JSONObject: TypeAlias = dict[str, JSONValue]
Role: TypeAlias = Literal["system", "user", "assistant", "tool"]
FinishReason: TypeAlias = Literal["stop", "length", "tool_calls"]
ProviderRequestPurpose: TypeAlias = Literal["agent_turn", "compaction"]
ToolSideEffects: TypeAlias = Literal["none", "read", "write", "external"]
WorkspaceAccessMode: TypeAlias = Literal["read-only", "workspace-write"]
SandboxEnforcement: TypeAlias = Literal["full", "partial"]
EventChannel: TypeAlias = Literal["durable", "live", "capability"]
AGENT_EVENT_SCHEMA_VERSION = 1
ControlDeliveryKind: TypeAlias = Literal["steer", "follow_up"]
SessionEventKind: TypeAlias = Literal[
    "message_committed",
    "context_compacted",
    "control_enqueued",
    "control_claimed",
    "control_delivered",
    "control_cancelled",
]
EventSource: TypeAlias = Literal[
    "user",
    "model",
    "tool",
    "runtime",
    "plugin",
    "recovery",
    "compaction",
    "legacy",
]
SessionState: TypeAlias = Literal["idle", "running", "aborting", "settled"]
EventKind: TypeAlias = Literal[
    "middleware_started",
    "middleware_finished",
    "middleware_failed",
    "middleware_cancelled",
    "tool_result_transformed",
    "runtime_started",
    "runtime_closing",
    "runtime_closed",
    "runtime_session_opened",
    "runtime_session_evicted",
    "session_control_cleanup_failed",
    "run_started",
    "message_committed",
    "context_prepared",
    "compaction_evaluated",
    "compaction_skipped",
    "compaction_started",
    "compaction_completed",
    "compaction_failed",
    "compaction_cancelled",
    "model_request_started",
    "model_request_finished",
    "model_request_failed",
    "provider_route_selected",
    "provider_attempt_started",
    "provider_attempt_succeeded",
    "provider_attempt_failed",
    "provider_retry_scheduled",
    "provider_fallback_selected",
    "thinking_delta",
    "text_delta",
    "model_usage",
    "tool_call_received",
    "tool_resolution_failed",
    "tool_validation_failed",
    "tool_approval_requested",
    "tool_approval_decided",
    "tool_execution_started",
    "tool_execution_denied",
    "tool_execution_finished",
    "tool_call_recovered",
    "plugin_loaded",
    "plugin_unloaded",
    "plugin_hook_started",
    "plugin_hook_finished",
    "plugin_hook_failed",
    "plugin_hook_cancelled",
    "plugin_session_opened",
    "plugin_session_closed",
    "run_completed",
    "run_failed",
    "run_cancelled",
    "run_settled",
]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A complete provider-neutral request to invoke one named tool."""

    id: str
    name: str
    arguments: JSONObject

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("tool call id must not be empty")
        if not self.name.strip():
            raise ValueError("tool call name must not be empty")


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Model-facing schema plus runtime safety metadata for a tool."""

    name: str
    description: str
    input_schema: Mapping[str, JSONValue]
    side_effects: ToolSideEffects = "none"
    requires_approval: bool = False
    retry_safe: bool = False
    replay_safe: bool = False
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool name must not be empty")
        if not self.description.strip():
            raise ValueError("tool description must not be empty")
        if self.input_schema.get("type") != "object":
            raise ValueError("tool input schema must have type='object'")
        if self.timeout_seconds is not None and self.timeout_seconds < 0:
            raise ValueError("tool timeout_seconds must not be negative")


@dataclass(frozen=True, slots=True)
class ToolOutput:
    """A finalized tool result with separate model and programmatic content."""

    content: str
    structured_content: JSONValue = None
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class ShellCollectedOutput:
    """One bounded shell stream plus optional recovery evidence."""

    text: str
    truncated: bool = False
    spill_path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("shell output text must be a string")
        if type(self.truncated) is not bool:
            raise TypeError("shell output truncated must be a boolean")
        if self.spill_path is not None:
            if not isinstance(self.spill_path, str):
                raise TypeError("shell output spill_path must be a string or None")
            if not self.spill_path.strip():
                raise ValueError("shell output spill_path must not be empty")


@dataclass(frozen=True, slots=True)
class ShellExecRequest:
    """Caller-facing Bash request before implementation defaults are resolved."""

    command: str
    workdir: str | None = None
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.command, str) or not self.command.strip():
            raise ValueError("shell command must not be empty")
        if "\x00" in self.command:
            raise ValueError("shell command must not contain NUL")
        if self.workdir is not None:
            if not isinstance(self.workdir, str) or not self.workdir.strip():
                raise ValueError("shell workdir must not be empty")
            if "\x00" in self.workdir:
                raise ValueError("shell workdir must not contain NUL")
        if self.timeout_seconds is not None:
            _validate_shell_timeout(self.timeout_seconds, "shell timeout_seconds")


@dataclass(frozen=True, slots=True)
class ShellExecSpec:
    """Fully resolved execution settings accepted by a ShellExecutor."""

    command: str
    workdir: str
    timeout_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.command, str) or not self.command.strip():
            raise ValueError("resolved shell command must not be empty")
        if "\x00" in self.command:
            raise ValueError("resolved shell command must not contain NUL")
        if not isinstance(self.workdir, str) or not self.workdir.strip():
            raise ValueError("resolved shell workdir must not be empty")
        if "\x00" in self.workdir:
            raise ValueError("resolved shell workdir must not contain NUL")
        _validate_shell_timeout(
            self.timeout_seconds,
            "resolved shell timeout_seconds",
        )


@dataclass(frozen=True, slots=True)
class ShellSandboxInfo:
    """Independent confinement facts for one shell execution."""

    mode: WorkspaceAccessMode
    enforcement: SandboxEnforcement | None = None
    denied: bool = False
    runner_failed: bool = False

    def __post_init__(self) -> None:
        if self.mode not in ("read-only", "workspace-write"):
            raise ValueError("unknown shell sandbox mode")
        if self.enforcement is not None and self.enforcement not in (
            "full",
            "partial",
        ):
            raise ValueError("unknown shell sandbox enforcement level")
        if type(self.denied) is not bool:
            raise TypeError("shell sandbox denied must be a boolean")
        if type(self.runner_failed) is not bool:
            raise TypeError("shell sandbox runner_failed must be a boolean")


@dataclass(frozen=True, slots=True)
class ShellRunResult:
    """Settled shell outcome with command, cancellation, and sandbox facts."""

    exit_code: int | None
    signal: str | None
    timed_out: bool
    cancelled: bool
    timeout_seconds: float
    stdout: ShellCollectedOutput
    stderr: ShellCollectedOutput
    sandbox: ShellSandboxInfo | None = None

    def __post_init__(self) -> None:
        if self.exit_code is not None and (
            not isinstance(self.exit_code, int) or isinstance(self.exit_code, bool)
        ):
            raise TypeError("shell exit_code must be an integer or None")
        if self.signal is not None and (
            not isinstance(self.signal, str) or not self.signal.strip()
        ):
            raise ValueError("shell signal must be a non-empty string or None")
        if type(self.timed_out) is not bool or type(self.cancelled) is not bool:
            raise TypeError("shell interruption facts must be booleans")
        if self.timed_out and self.cancelled:
            raise ValueError("shell timeout and cancellation are mutually exclusive")
        _validate_shell_timeout(
            self.timeout_seconds,
            "shell result timeout_seconds",
        )
        if not isinstance(self.stdout, ShellCollectedOutput):
            raise TypeError("shell stdout must be ShellCollectedOutput")
        if not isinstance(self.stderr, ShellCollectedOutput):
            raise TypeError("shell stderr must be ShellCollectedOutput")
        if self.sandbox is not None and not isinstance(
            self.sandbox,
            ShellSandboxInfo,
        ):
            raise TypeError("shell sandbox must be ShellSandboxInfo or None")


def _validate_shell_timeout(value: object, label: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{label} must be positive and finite")


@dataclass(frozen=True, slots=True)
class SandboxProcessRequest:
    """One exact argv requested inside a workspace process sandbox."""

    argv: tuple[str, ...]
    workdir: str = "/workspace"
    mode: WorkspaceAccessMode = "workspace-write"
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.argv, tuple):
            raise TypeError("sandbox argv must be a tuple")
        if (
            not self.argv
            or not isinstance(self.argv[0], str)
            or not self.argv[0].strip()
        ):
            raise ValueError("sandbox argv must contain a non-empty program")
        if any(not isinstance(item, str) for item in self.argv):
            raise TypeError("sandbox argv entries must be strings")
        if any("\x00" in item for item in self.argv):
            raise ValueError("sandbox argv entries must not contain NUL")
        if not isinstance(self.workdir, str) or not self.workdir.strip():
            raise ValueError("sandbox workdir must not be empty")
        if "\x00" in self.workdir:
            raise ValueError("sandbox workdir must not contain NUL")
        workdir = PurePosixPath(self.workdir)
        if (
            not workdir.is_absolute()
            or workdir.parts[:2] != ("/", "workspace")
            or ".." in workdir.parts
        ):
            raise ValueError("sandbox workdir must stay under /workspace")
        if self.mode not in ("read-only", "workspace-write"):
            raise ValueError("unknown workspace access mode")
        if self.timeout_seconds is not None:
            if (
                not isinstance(self.timeout_seconds, (int, float))
                or isinstance(self.timeout_seconds, bool)
                or not isfinite(self.timeout_seconds)
                or self.timeout_seconds < 0
            ):
                raise ValueError(
                    "sandbox timeout_seconds must be non-negative and finite"
                )


@dataclass(frozen=True, slots=True)
class SandboxProcessResult:
    """Completed sandbox command plus evidence about its enforcement."""

    exit_code: int
    stdout: str
    stderr: str
    enforcement: SandboxEnforcement
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.exit_code, int) or isinstance(
            self.exit_code,
            bool,
        ):
            raise TypeError("sandbox exit_code must be an integer")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise TypeError("sandbox stdout and stderr must be strings")
        if self.enforcement not in ("full", "partial"):
            raise ValueError("unknown sandbox enforcement level")
        if type(self.stdout_truncated) is not bool:
            raise TypeError("stdout_truncated must be a boolean")
        if type(self.stderr_truncated) is not bool:
            raise TypeError("stderr_truncated must be a boolean")

    @property
    def output_truncated(self) -> bool:
        """Whether either captured stream dropped bytes from its head."""
        return self.stdout_truncated or self.stderr_truncated


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """The result returned by an ApprovalPolicy."""

    approved: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.approved) is not bool:
            raise TypeError("approved must be a boolean")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError("approval reason must be a string or None")


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Frozen snapshot of identity and policy state for one tool invocation."""

    call: ToolCall
    definition: ToolDefinition
    request_number: int
    call_index: int
    session_id: str | None = None
    approval: ApprovalDecision | None = None
    run_id: str | None = None
    turn_id: str | None = None

    def __post_init__(self) -> None:
        if self.call.name != self.definition.name:
            raise ValueError("tool call and definition names must match")
        if self.request_number <= 0:
            raise ValueError("request_number must be greater than zero")
        if self.call_index <= 0:
            raise ValueError("call_index must be greater than zero")
        if self.session_id is not None and not self.session_id.strip():
            raise ValueError("session_id must not be empty")
        for name, value in (("run_id", self.run_id), ("turn_id", self.turn_id)):
            if value is not None and not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.turn_id is not None and self.run_id is None:
            raise ValueError("turn_id requires run_id")


@dataclass(frozen=True, slots=True)
class ToolResultContext:
    """Execution identity and outcome before a tool result becomes durable."""

    execution: ToolExecutionContext
    outcome: str
    run_id: str | None = None
    turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderRequestContext:
    """Immutable identity envelope for one logical model-provider request."""

    request_id: str
    attempt_id: str
    purpose: ProviderRequestPurpose
    request_number: int
    attempt: int = 1
    session_id: str | None = None
    run_id: str | None = None
    turn_id: str | None = None
    parent_request_id: str | None = None
    cause_event_id: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("request_id", self.request_id),
            ("attempt_id", self.attempt_id),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.request_number <= 0:
            raise ValueError("request_number must be greater than zero")
        if self.attempt <= 0:
            raise ValueError("attempt must be greater than zero")
        if self.purpose not in {"agent_turn", "compaction"}:
            raise ValueError(f"unsupported provider request purpose: {self.purpose}")
        for name, optional_value in (
            ("session_id", self.session_id),
            ("run_id", self.run_id),
            ("turn_id", self.turn_id),
            ("parent_request_id", self.parent_request_id),
            ("cause_event_id", self.cause_event_id),
        ):
            if optional_value is not None and not optional_value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.turn_id is not None and self.run_id is None:
            raise ValueError("turn_id requires run_id")
        if self.purpose == "agent_turn" and (
            self.run_id is None or self.turn_id is None
        ):
            raise ValueError("agent_turn requests require run_id and turn_id")


@dataclass(frozen=True, slots=True)
class NextTurnContext:
    """Identity for the safe boundary before a run's next model request."""

    run_id: str
    turn_id: str
    next_request_number: int
    session_id: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("run_id", self.run_id), ("turn_id", self.turn_id)):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.next_request_number <= 0:
            raise ValueError("next_request_number must be greater than zero")
        if self.session_id is not None and not self.session_id.strip():
            raise ValueError("session_id must not be empty")


@dataclass(frozen=True, slots=True)
class ProviderState:
    """Opaque provider-owned response item required for stateless continuation.

    The runtime persists this value but never publishes it through observer
    events. Providers must ignore state owned by another adapter.
    """

    provider: str
    item: JSONObject = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider state owner must not be empty")
        if not isinstance(self.item, dict):
            raise TypeError("provider state item must be a JSON object")


@dataclass(frozen=True, slots=True)
class Message:
    """A model-visible conversation entry."""

    role: Role
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False
    structured_content: JSONValue = None
    thinking: str = ""
    provider_state: tuple[ProviderState, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if self.thinking and self.role != "assistant":
            raise ValueError("only assistant messages may contain thinking")
        if self.provider_state and self.role != "assistant":
            raise ValueError("only assistant messages may contain provider state")
        if not isinstance(self.provider_state, tuple) or not all(
            isinstance(state, ProviderState) for state in self.provider_state
        ):
            raise TypeError("provider_state must be a tuple of ProviderState values")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages may contain tool calls")
        if self.role == "tool":
            if not self.tool_call_id or not self.tool_name:
                raise ValueError("tool messages require tool_call_id and tool_name")
        elif self.tool_call_id is not None or self.tool_name is not None:
            raise ValueError("only tool messages may reference a tool call")
        if self.is_error and self.role != "tool":
            raise ValueError("only tool messages may be marked as errors")
        if self.structured_content is not None and self.role != "tool":
            raise ValueError("only tool messages may contain structured content")


@dataclass(frozen=True, slots=True)
class SessionCompaction:
    """A durable replacement of one contiguous historical message prefix."""

    replace_start: int
    replaced_message_count: int
    summary_message: Message

    def __post_init__(self) -> None:
        if self.replace_start < 0:
            raise ValueError("compaction replace_start must not be negative")
        if self.replaced_message_count <= 0:
            raise ValueError(
                "compaction replaced_message_count must be greater than zero"
            )
        if self.summary_message.role != "user":
            raise ValueError("compaction summary message must use the user role")
        if not self.summary_message.content.strip():
            raise ValueError("compaction summary message must not be empty")
        if self.summary_message.tool_calls:
            raise ValueError("compaction summary message cannot contain tool calls")


@dataclass(frozen=True, slots=True)
class CompactionDecision:
    """One observable policy decision made before an AgentSession run."""

    should_compact: bool
    estimated_tokens: int
    token_threshold: int
    keep_recent_turns: int

    def __post_init__(self) -> None:
        if type(self.should_compact) is not bool:
            raise TypeError("should_compact must be a boolean")
        if type(self.estimated_tokens) is not int or self.estimated_tokens < 0:
            raise ValueError("estimated_tokens must be a non-negative integer")
        if type(self.token_threshold) is not int or self.token_threshold <= 0:
            raise ValueError("token_threshold must be a positive integer")
        if type(self.keep_recent_turns) is not int or self.keep_recent_turns <= 0:
            raise ValueError("keep_recent_turns must be a positive integer")


@dataclass(frozen=True, slots=True)
class EventProvenance:
    """Origin and causal linkage for one durable session fact."""

    source: EventSource
    actor_id: str | None = None
    cause_event_id: str | None = None
    copied_from_event_id: str | None = None

    def __post_init__(self) -> None:
        if self.source not in {
            "user",
            "model",
            "tool",
            "runtime",
            "plugin",
            "recovery",
            "compaction",
            "legacy",
        }:
            raise ValueError(f"unsupported event provenance source: {self.source}")
        for name, value in (
            ("actor_id", self.actor_id),
            ("cause_event_id", self.cause_event_id),
            ("copied_from_event_id", self.copied_from_event_id),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True, slots=True)
class SessionEventContext:
    """Metadata shared by durable facts created by one runtime action."""

    run_id: str | None = None
    turn_id: str | None = None
    provenance: EventProvenance = field(
        default_factory=lambda: EventProvenance(source="runtime")
    )

    def __post_init__(self) -> None:
        if self.run_id is not None and not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        if self.turn_id is not None and not self.turn_id.strip():
            raise ValueError("turn_id must not be empty")
        if self.turn_id is not None and self.run_id is None:
            raise ValueError("turn_id requires run_id")
        if not isinstance(self.provenance, EventProvenance):
            raise TypeError("provenance must be EventProvenance")


@dataclass(frozen=True, slots=True)
class QueuedControl:
    """One durable steer or follow-up intent accepted by a session."""

    control_id: str
    delivery: ControlDeliveryKind
    prompt: str
    run_id: str
    target_run_id: str | None = None
    cause_run_id: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("control_id", self.control_id),
            ("prompt", self.prompt),
            ("run_id", self.run_id),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.delivery == "steer":
            if self.target_run_id != self.run_id:
                raise ValueError("steer must target its active run_id")
        elif self.target_run_id is not None:
            raise ValueError("follow_up cannot have target_run_id")
        if self.cause_run_id is not None and not self.cause_run_id.strip():
            raise ValueError("cause_run_id must not be empty")


@dataclass(frozen=True, slots=True)
class ControlClaim:
    """A leased claim that prevents two dispatchers consuming one control."""

    control_id: str
    claim_id: str
    owner_id: str
    lease_expires_at: float

    def __post_init__(self) -> None:
        for name, value in (
            ("control_id", self.control_id),
            ("claim_id", self.claim_id),
            ("owner_id", self.owner_id),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.lease_expires_at <= 0:
            raise ValueError("lease_expires_at must be greater than zero")


@dataclass(frozen=True, slots=True)
class ControlDelivery:
    """Atomically consumes a claimed control and commits its user message."""

    control_id: str
    claim_id: str
    message: Message

    def __post_init__(self) -> None:
        if not self.control_id.strip() or not self.claim_id.strip():
            raise ValueError("control_id and claim_id must not be empty")
        if self.message.role != "user":
            raise ValueError("control delivery message must use the user role")


@dataclass(frozen=True, slots=True)
class ControlCancellation:
    """Terminal cancellation of a queued or claimed control."""

    control_id: str
    reason: str

    def __post_init__(self) -> None:
        if not self.control_id.strip() or not self.reason.strip():
            raise ValueError("control_id and reason must not be empty")


ControlTransition: TypeAlias = (
    QueuedControl | ControlClaim | ControlDelivery | ControlCancellation
)


@dataclass(frozen=True, slots=True)
class PendingControl:
    """Replay projection for a control that has not reached a terminal event."""

    control: QueuedControl
    claim: ControlClaim | None = None


@dataclass(frozen=True, slots=True)
class DurableSessionEvent:
    """One ordered fact in a replayable session log."""

    session_id: str
    sequence: int
    kind: SessionEventKind
    message: Message | None = None
    compaction: SessionCompaction | None = None
    control: ControlTransition | None = None
    event_id: str | None = None
    run_id: str | None = None
    turn_id: str | None = None
    provenance: EventProvenance = field(
        default_factory=lambda: EventProvenance(source="legacy")
    )

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("session event id must not be empty")
        if self.sequence <= 0:
            raise ValueError("session event sequence must be greater than zero")
        if self.event_id is None:
            object.__setattr__(
                self,
                "event_id",
                session_event_id(self.session_id, self.sequence),
            )
        elif not self.event_id.strip():
            raise ValueError("event_id must not be empty")
        SessionEventContext(
            run_id=self.run_id,
            turn_id=self.turn_id,
            provenance=self.provenance,
        )
        if self.kind == "message_committed":
            if (
                self.message is None
                or self.compaction is not None
                or self.control is not None
            ):
                raise ValueError(
                    "message_committed event requires only a message payload"
                )
        elif self.kind == "context_compacted":
            if (
                self.compaction is None
                or self.message is not None
                or self.control is not None
            ):
                raise ValueError(
                    "context_compacted event requires only a compaction payload"
                )
        elif self.kind == "control_enqueued":
            if not isinstance(self.control, QueuedControl):
                raise ValueError("control_enqueued requires QueuedControl")
        elif self.kind == "control_claimed":
            if not isinstance(self.control, ControlClaim):
                raise ValueError("control_claimed requires ControlClaim")
        elif self.kind == "control_delivered":
            if not isinstance(self.control, ControlDelivery):
                raise ValueError("control_delivered requires ControlDelivery")
        elif self.kind == "control_cancelled":
            if not isinstance(self.control, ControlCancellation):
                raise ValueError("control_cancelled requires ControlCancellation")
        else:
            raise ValueError(f"unsupported session event kind: {self.kind}")
        if self.control is not None and (
            self.message is not None or self.compaction is not None
        ):
            raise ValueError("control events cannot use message or compaction payloads")


def session_event_id(session_id: str, sequence: int) -> str:
    """Return the stable identity assigned to one append-only session fact."""
    return f"{session_id}:{sequence}"


@dataclass(frozen=True, slots=True)
class DurableEventIdentity:
    """Stable identity returned after one durable session fact is committed."""

    session_id: str
    sequence: int
    event_id: str

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("durable event session_id must not be empty")
        if self.sequence <= 0:
            raise ValueError("durable event sequence must be greater than zero")
        if not self.event_id.strip():
            raise ValueError("durable event event_id must not be empty")


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """An immutable view of one session at a known version."""

    session_id: str
    version: int
    messages: tuple[Message, ...]
    pending_controls: tuple[PendingControl, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextPlan:
    """The model-visible projection prepared for one provider request."""

    messages: tuple[Message, ...]
    source_message_count: int
    omitted_message_count: int = 0
    token_budget: int | None = None
    estimated_tokens: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.messages, tuple):
            raise TypeError("context plan messages must be a tuple")
        if self.source_message_count < 0:
            raise ValueError("source_message_count must not be negative")
        if self.omitted_message_count < 0:
            raise ValueError("omitted_message_count must not be negative")
        if self.omitted_message_count > self.source_message_count:
            raise ValueError(
                "omitted_message_count must not exceed source_message_count"
            )
        if self.token_budget is not None and self.token_budget < 0:
            raise ValueError("token_budget must not be negative")
        if self.estimated_tokens is not None and self.estimated_tokens < 0:
            raise ValueError("estimated_tokens must not be negative")


@dataclass(frozen=True, slots=True)
class TextDelta:
    """A piece of assistant text emitted by a model provider."""

    text: str


@dataclass(frozen=True, slots=True)
class ThinkingDelta:
    """A streamed piece of model reasoning used to build assistant state."""

    text: str


@dataclass(frozen=True, slots=True)
class Finish:
    """The provider's explicit end-of-message marker."""

    reason: FinishReason = "stop"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Provider-neutral token accounting for one model request."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int | None = None


ModelStreamEvent: TypeAlias = (
    TextDelta | ThinkingDelta | ToolCall | ProviderState | TokenUsage | Finish
)


@dataclass(frozen=True, slots=True)
class SessionOpenRequest:
    """Untrusted, non-secret caller hints for a SessionSpecResolver."""

    user_id: str | None = None
    tenant_id: str | None = None
    profile: str | None = None
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("user_id", "tenant_id", "profile"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string")
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class AgentEventEnvelope:
    """Stable metadata shared by every best-effort observer event."""

    schema_version: int = AGENT_EVENT_SCHEMA_VERSION
    event_id: str = field(default_factory=lambda: uuid4().hex)
    occurred_at: float = field(default_factory=time)
    source: str = "application"
    session_id: str | None = None
    run_id: str | None = None
    turn_id: str | None = None
    request_id: str | None = None
    attempt_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    duration_ms: float | None = None
    durable_event_id: str | None = None
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))
        if self.schema_version <= 0:
            raise ValueError("event schema_version must be greater than zero")
        if not self.event_id.strip():
            raise ValueError("event_id must not be empty")
        if self.occurred_at < 0:
            raise ValueError("occurred_at must not be negative")
        if not self.source.strip():
            raise ValueError("event source must not be empty")
        for name, value in (
            ("session_id", self.session_id),
            ("run_id", self.run_id),
            ("turn_id", self.turn_id),
            ("request_id", self.request_id),
            ("attempt_id", self.attempt_id),
            ("span_id", self.span_id),
            ("parent_span_id", self.parent_span_id),
            ("durable_event_id", self.durable_event_id),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.turn_id is not None and self.run_id is None:
            raise ValueError("event turn_id requires run_id")
        if self.attempt_id is not None and self.request_id is None:
            raise ValueError("event attempt_id requires request_id")
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError("event duration_ms must not be negative")


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """An observable runtime event.

    Durable events can be persisted and replayed. Live events describe transient
    progress and must never be the only source of model-visible state.
    """

    kind: EventKind
    channel: EventChannel
    payload: Mapping[str, object] = field(default_factory=dict)
    envelope: AgentEventEnvelope = field(default_factory=AgentEventEnvelope)


@dataclass(frozen=True, slots=True)
class AgentResult:
    """The committed result of one successful run."""

    message: Message
    messages: tuple[Message, ...]
    finish_reason: FinishReason
    usage: TokenUsage | None = None


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    """Allowlisted observation fields; no event payload or content."""

    kind: EventKind
    channel: EventChannel
    envelope: AgentEventEnvelope
    outcome: str | None = None
    provider_name: str | None = None
    tool_name: str | None = None
    usage: TokenUsage | None = None
