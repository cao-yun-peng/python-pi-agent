"""Manually advanced lease clock. No real waits, threads or background timers."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from math import isfinite


class ManualLeaseClock:
    def __init__(self, *, unix_time: float = 1_000.0) -> None:
        self._wall = unix_time
        self._mono = 0.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []
        self._timeouts: list[tuple[float, asyncio.Timeout]] = []

    def unix_time(self) -> float:
        return self._wall

    def monotonic(self) -> float:
        return self._mono

    @property
    def pending_sleeps(self) -> int:
        return sum(not future.done() for _, future in self._sleepers)

    @property
    def pending_timeouts(self) -> int:
        return len(self._timeouts)

    def advance(self, seconds: float) -> None:
        if isinstance(seconds, bool) or not isfinite(seconds) or seconds < 0:
            raise ValueError("clock advance must be finite and non-negative")
        self._mono += seconds
        self._wall += seconds
        for deadline, future in tuple(self._sleepers):
            if deadline <= self._mono and not future.done():
                future.set_result(None)
        for deadline, context in tuple(self._timeouts):
            if deadline <= self._mono and not context.expired():
                context.reschedule(asyncio.get_running_loop().time())

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        entry = (self._mono + seconds, future)
        self._sleepers.append(entry)
        if seconds <= 0:
            future.set_result(None)
        try:
            await future
        finally:
            self._sleepers.remove(entry)

    @asynccontextmanager
    async def timeout(self, seconds: float) -> AsyncIterator[object]:
        async with asyncio.timeout(None) as context:
            entry = (self._mono + seconds, context)
            self._timeouts.append(entry)
            try:
                yield context
            finally:
                self._timeouts.remove(entry)
