"""Consumer-facing identity, event stream, and result for a queued run."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .domain import AgentResult
from .event_hub import EventSubscription


class RunHandle:
    def __init__(
        self,
        run_id: str,
        events: EventSubscription,
        result: asyncio.Future[AgentResult],
        abort: Callable[[], Awaitable[None]],
    ) -> None:
        self._run_id = run_id
        self._events = events
        self._result = result
        self._abort: Callable[[], Awaitable[None]] | None = abort
        result.add_done_callback(self._settled)

    @property
    def run_id(self) -> str:
        return self._run_id

    def events(self) -> EventSubscription:
        """Return the same single-consumer stream; consumed events are not replayed."""
        return self._events

    async def result(self) -> AgentResult:
        """Wait independently of event consumption or another waiter's cancellation."""
        return await asyncio.shield(self._result)

    async def abort(self) -> None:
        """Cancel only this run, whether queued or active."""
        abort = self._abort
        if not self._result.done() and abort is not None:
            await abort()

    def _settled(self, _: asyncio.Future[AgentResult]) -> None:
        self._abort = None
        self._events._finish()
