"""Pure domain values shared by providers, the runtime, and applications."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, TypeAlias

Role: TypeAlias = Literal["system", "user", "assistant"]
FinishReason: TypeAlias = Literal["stop", "length"]
EventChannel: TypeAlias = Literal["durable", "live"]
EventKind: TypeAlias = Literal[
    "run_started",
    "message_committed",
    "model_request_started",
    "text_delta",
    "run_completed",
    "run_failed",
    "run_cancelled",
    "run_settled",
]


@dataclass(frozen=True, slots=True)
class Message:
    """A model-visible conversation entry."""

    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class TextDelta:
    """A piece of assistant text emitted by a model provider."""

    text: str


@dataclass(frozen=True, slots=True)
class Finish:
    """The provider's explicit end-of-message marker."""

    reason: FinishReason = "stop"


ModelStreamEvent: TypeAlias = TextDelta | Finish


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
