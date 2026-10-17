"""Ports consumed by the core runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from .domain import AgentEvent, Message, ModelStreamEvent


class ModelProvider(Protocol):
    """Produces a normalized stream independently of any model vendor SDK."""

    def stream(self, messages: Sequence[Message]) -> AsyncIterator[ModelStreamEvent]:
        """Stream one assistant response for the supplied model-visible history."""
        ...


class EventSink(Protocol):
    """Accepts ordered runtime events without performing blocking I/O."""

    def emit(self, event: AgentEvent) -> None:
        """Record or enqueue an event and return immediately."""
        ...
