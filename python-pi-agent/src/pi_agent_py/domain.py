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
EventKind: TypeAlias = Literal[
    "run_started",
    "message_committed",
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
    "tool_execution_finished",
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

    def __post_init__(self) -> None:
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
class TextDelta:
    """A piece of assistant text emitted by a model provider."""

    text: str


@dataclass(frozen=True, slots=True)
class ThinkingDelta:
    """A piece of model reasoning that is observable but not committed."""

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
