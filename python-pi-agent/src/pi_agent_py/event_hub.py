"""Bounded, loop-local observation queues with explicit lifetimes."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .domain import AgentEvent, EventChannel, EventKind

OverflowPolicy = Literal["drop_oldest", "drop_newest", "close"]


@dataclass(frozen=True, slots=True)
class EventFilter:
    session_id: str | None = None
    run_id: str | None = None
    kinds: frozenset[EventKind] | None = None
    channels: frozenset[EventChannel] | None = None

    def matches(self, event: AgentEvent) -> bool:
        return (
            (self.session_id is None or self.session_id == event.envelope.session_id)
            and (self.run_id is None or self.run_id == event.envelope.run_id)
            and (self.kinds is None or event.kind in self.kinds)
            and (self.channels is None or event.channel in self.channels)
        )


class EventSubscription:
    """Single stream; close explicitly when abandoning consumption early."""

    def __init__(
        self,
        filter: EventFilter,
        max_buffer: int,
        overflow: OverflowPolicy,
        detach: Callable[[EventSubscription], None],
        *,
        until_settled: bool = False,
    ) -> None:
        self._filter = filter
        self._max_buffer = max_buffer
        self._overflow = overflow
        self._detach = detach
        self._until_settled = until_settled
        self._queue: deque[AgentEvent] = deque()
        self._ready = asyncio.Event()
        self._closed = False
        self._dropped = 0

    @property
    def dropped_count(self) -> int:
        return self._dropped

    def __aiter__(self) -> EventSubscription:
        return self

    async def __anext__(self) -> AgentEvent:
        while not self._queue:
            if self._closed:
                raise StopAsyncIteration
            self._ready.clear()
            await self._ready.wait()
        return self._queue.popleft()

    async def __aenter__(self) -> EventSubscription:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._finish()
        self._queue.clear()

    def _finish(self) -> None:
        if not self._closed:
            self._closed = True
            self._detach(self)
            self._ready.set()

    def _publish(self, event: AgentEvent) -> None:
        if not self._filter.matches(event):
            return
        if self._until_settled and event.kind == "run_settled":
            # One reserved slot: even an unattended run can signal termination.
            self._queue.append(event)
            self._finish()
            return
        if len(self._queue) == self._max_buffer:
            self._dropped += 1
            if self._overflow == "close":
                self._finish()
                return
            if self._overflow == "drop_newest":
                return
            self._queue.popleft()
        self._queue.append(event)
        self._ready.set()


class EventHub:
    """EventSink for asynchronous subscribers on the Runtime's event loop."""

    def __init__(self) -> None:
        self._subscriptions: set[EventSubscription] = set()
        self._closed = False

    def subscribe(
        self,
        filter: EventFilter = EventFilter(),
        *,
        max_buffer: int = 256,
        overflow: OverflowPolicy = "drop_oldest",
    ) -> EventSubscription:
        return self._subscribe(filter, max_buffer, overflow)

    def _subscribe(
        self,
        filter: EventFilter,
        max_buffer: int,
        overflow: OverflowPolicy,
        *,
        until_settled: bool = False,
    ) -> EventSubscription:
        if self._closed:
            raise RuntimeError("event hub is closed")
        if type(max_buffer) is not int or max_buffer <= 0:
            raise ValueError("max_buffer must be a positive integer")
        if overflow not in ("drop_oldest", "drop_newest", "close"):
            raise ValueError("unknown overflow policy")
        subscription = EventSubscription(
            filter,
            max_buffer,
            overflow,
            self._subscriptions.discard,
            until_settled=until_settled,
        )
        self._subscriptions.add(subscription)
        return subscription

    def emit(self, event: AgentEvent) -> None:
        for subscription in tuple(self._subscriptions):
            subscription._publish(event)

    async def aclose(self) -> None:
        """Detach and end iterators after they drain their buffered events."""
        self._closed = True
        for subscription in tuple(self._subscriptions):
            subscription._finish()
