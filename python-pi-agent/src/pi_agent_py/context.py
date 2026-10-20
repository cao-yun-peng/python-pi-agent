"""Context strategy implementations kept separate from durable history."""

from __future__ import annotations

from collections.abc import Sequence

from .domain import ContextPlan, Message


class FullHistoryContextStrategy:
    """Pass every committed message to the provider in its original order."""

    async def prepare(self, messages: Sequence[Message]) -> ContextPlan:
        history = tuple(messages)
        return ContextPlan(
            messages=history,
            source_message_count=len(history),
        )
