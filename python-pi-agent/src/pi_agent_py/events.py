"""Small event sink implementations for apps, examples, and tests."""

from __future__ import annotations

from collections.abc import Callable

from .domain import AgentEvent


class NullEventSink:
    """Drops events when an application does not need observation."""

    def emit(self, event: AgentEvent) -> None:
        del event


class InMemoryEventSink:
    """Captures events in emission order for deterministic assertions."""

    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)


class CallbackEventSink:
    """Adapts a fast synchronous callback to the EventSink port."""

    def __init__(self, callback: Callable[[AgentEvent], None]) -> None:
        self._callback = callback

    def emit(self, event: AgentEvent) -> None:
        self._callback(event)
