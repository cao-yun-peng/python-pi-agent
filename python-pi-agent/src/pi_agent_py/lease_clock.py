"""Default lease clock; construction performs no I/O and starts no tasks."""

import asyncio
from contextlib import AbstractAsyncContextManager
from time import monotonic, time


class SystemLeaseClock:
    def unix_time(self) -> float:
        return time()

    def monotonic(self) -> float:
        return monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def timeout(self, seconds: float) -> AbstractAsyncContextManager[object]:
        return asyncio.timeout(seconds)
