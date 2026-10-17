"""Deterministic test doubles for learning and contract tests."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from typing import TypeAlias

from .domain import Message, ModelStreamEvent
from .errors import ProviderError


@dataclass(frozen=True, slots=True)
class WaitUntilCancelled:
    """A fake script step that blocks until its owning task is cancelled."""


FakeStep: TypeAlias = ModelStreamEvent | Exception | WaitUntilCancelled


class FakeProvider:
    """Replays one deterministic script for each model request."""

    def __init__(self, responses: Iterable[Iterable[FakeStep]]) -> None:
        self._responses = deque(tuple(response) for response in responses)
        self.requests: list[tuple[Message, ...]] = []
        self.blocked = asyncio.Event()

    async def stream(
        self,
        messages: Sequence[Message],
    ) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(tuple(messages))
        if not self._responses:
            raise ProviderError("fake provider has no scripted response left")

        for step in self._responses.popleft():
            if isinstance(step, WaitUntilCancelled):
                self.blocked.set()
                await asyncio.Event().wait()
                continue
            if isinstance(step, Exception):
                raise step
            yield step
