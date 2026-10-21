"""Pure domain values shared by providers, the runtime, and applications."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, TypeAlias

JSONPrimitive: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONPrimitive | list["JSONValue"] | dict[str, "JSONValue"]
JSONObject: TypeAlias = dict[str, JSONValue]
Role: TypeAlias = Literal["system", "user", "assistant", "tool"]
FinishReason: TypeAlias = Literal["stop", "length", "tool_calls"]
ToolSideEffects: TypeAlias = Literal["none", "read", "write", "external"]
EventChannel: TypeAlias = Literal["durable", "live", "capability"]
SessionEventKind: TypeAlias = Literal["message_committed", "context_compacted"]
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
    "thinking_delta",
    "text_delta",
    "model_usage",
    "tool_call_received",
    "tool_resolution_failed",
    "tool_validation_failed",
    "tool_approval_requested",
    "tool_approval_decided",
    "tool_execution_started",
    "tool_sandbox_denied",
    "tool_execution_finished",
    "tool_call_recovered",
    "plugin_loaded",
    "plugin_unloaded",
    "plugin_hook_started",
    "plugin_hook_finished",
    "plugin_hook_failed",
    "plugin_hook_cancelled",
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

    def __post_init__(self) -> None:
        if self.call.name != self.definition.name:
            raise ValueError("tool call and definition names must match")
        if self.request_number <= 0:
            raise ValueError("request_number must be greater than zero")
        if self.call_index <= 0:
            raise ValueError("call_index must be greater than zero")
        if self.session_id is not None and not self.session_id.strip():
            raise ValueError("session_id must not be empty")


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

    def __post_init__(self) -> None:
        if self.thinking and self.role != "assistant":
            raise ValueError("only assistant messages may contain thinking")
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
class DurableSessionEvent:
    """One ordered fact in a replayable session log."""

    session_id: str
    sequence: int
    kind: SessionEventKind
    message: Message | None = None
    compaction: SessionCompaction | None = None
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
            if self.message is None or self.compaction is not None:
                raise ValueError(
                    "message_committed event requires only a message payload"
                )
        elif self.kind == "context_compacted":
            if self.compaction is None or self.message is not None:
                raise ValueError(
                    "context_compacted event requires only a compaction payload"
                )
        else:
            raise ValueError(f"unsupported session event kind: {self.kind}")


def session_event_id(session_id: str, sequence: int) -> str:
    """Return the stable identity assigned to one append-only session fact."""
    return f"{session_id}:{sequence}"


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """An immutable view of one session at a known version."""

    session_id: str
    version: int
    messages: tuple[Message, ...]


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
    TextDelta | ThinkingDelta | ToolCall | TokenUsage | Finish
)


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """An observable runtime event.

    Durable events can be persisted and replayed. Live events describe transient
    progress and must never be the only source of model-visible state.
    """

    kind: EventKind
    channel: EventChannel
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentResult:
    """The committed result of one successful run."""

    message: Message
    messages: tuple[Message, ...]
    finish_reason: FinishReason
    usage: TokenUsage | None = None
