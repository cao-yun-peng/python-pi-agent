"""Evaluation budgets and shared, cancellable admission above ModelProvider."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields

from ...domain import Message, ModelStreamEvent, ProviderRequestContext, ToolDefinition
from ...ports import ModelProvider


@dataclass(frozen=True)
class EvaluationPolicy:
    max_requests: int = 40
    timeout_seconds: float = 1200
    min_request_interval_seconds: float = 30
    retry_attempts: int = 3
    retry_base_seconds: float = 30
    retry_max_seconds: float = 60
    retry_elapsed_seconds: float = 180

    def __post_init__(self) -> None:
        for name in ("max_requests", "retry_attempts"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("timeout_seconds", "min_request_interval_seconds", "retry_base_seconds",
                     "retry_max_seconds", "retry_elapsed_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.timeout_seconds == 0 or self.retry_elapsed_seconds == 0:
            raise ValueError("elapsed budgets must be positive")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("retry maximum must cover base delay")

    @classmethod
    def parse(cls, values: Mapping[str, object]) -> EvaluationPolicy:
        allowed = {field.name for field in fields(cls)}
        if set(values) - allowed:
            raise ValueError("unknown evaluation policy fields")
        # Dataclass validation owns the boundary, including bool/int distinctions.
        return cls(**values)  # type: ignore[arg-type]

    def record(self) -> dict[str, object]:
        return dict(asdict(self))


class SharedRequestLimiter:
    """One instance per batch, also used by every physical retry.

    The lock protects admission only. Cancellation during a wait neither spends
    a slot nor holds the lock. Injectable clocks/sleep keep default tests fast.
    """

    def __init__(self, interval: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, initial_wait: bool = False) -> None:
        if isinstance(interval, bool) or not math.isfinite(interval) or interval < 0:
            raise ValueError("interval must be finite and nonnegative")
        self.interval = interval
        self._clock, self._sleep = clock, sleep
        self._lock = asyncio.Lock()
        self._last: float | None = clock() if initial_wait else None
        self.waited_seconds = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            if self._last is not None:
                delay = self.interval - (self._clock() - self._last)
                if delay > 0:
                    waiting = self._clock()
                    try:
                        await self._sleep(delay)
                    finally:
                        self.waited_seconds += max(0, self._clock() - waiting)
            self._last = self._clock()


class LimitedProvider:
    """Borrow a provider and batch limiter; no independent tasks or cleanup."""

    def __init__(self, provider: ModelProvider, limiter: SharedRequestLimiter) -> None:
        self._provider, self._limiter = provider, limiter

    async def stream(self, messages: Sequence[Message], tools: Sequence[ToolDefinition] = (),
                     *, context: ProviderRequestContext) -> AsyncIterator[ModelStreamEvent]:
        await self._limiter.acquire()
        async for event in self._provider.stream(messages, tools, context=context):
            yield event
