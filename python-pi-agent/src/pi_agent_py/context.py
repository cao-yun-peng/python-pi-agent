"""Context strategy implementations kept separate from durable history."""

from __future__ import annotations

import json
from collections.abc import Sequence

from .domain import ContextPlan, Message
from .errors import ContextBudgetExceededError, ContextPreparationError
from .ports import TokenEstimator


class FullHistoryContextStrategy:
    """Pass every committed message to the provider in its original order."""

    async def prepare(self, messages: Sequence[Message]) -> ContextPlan:
        history = tuple(messages)
        return ContextPlan(
            messages=history,
            source_message_count=len(history),
        )


class CharacterTokenEstimator:
    """A deterministic character heuristic, not a vendor tokenizer."""

    def __init__(
        self,
        *,
        characters_per_token: int = 4,
        per_message_overhead: int = 4,
    ) -> None:
        if (
            isinstance(characters_per_token, bool)
            or not isinstance(characters_per_token, int)
            or characters_per_token <= 0
        ):
            raise ValueError("characters_per_token must be a positive integer")
        if (
            isinstance(per_message_overhead, bool)
            or not isinstance(per_message_overhead, int)
            or per_message_overhead < 0
        ):
            raise ValueError("per_message_overhead must be a non-negative integer")
        self._characters_per_token = characters_per_token
        self._per_message_overhead = per_message_overhead

    def estimate(self, messages: Sequence[Message]) -> int:
        character_count = sum(
            _message_character_count(message) for message in messages
        )
        content_tokens = (
            character_count + self._characters_per_token - 1
        ) // self._characters_per_token
        return content_tokens + self._per_message_overhead * len(messages)


class TokenBudgetContextStrategy:
    """Keep pinned system messages and recent complete turns in a message budget.

    The budget covers only Message values. Tool definitions, provider envelope
    overhead, and reserved output tokens remain application concerns.
    """

    def __init__(
        self,
        token_budget: int,
        *,
        estimator: TokenEstimator | None = None,
    ) -> None:
        if (
            isinstance(token_budget, bool)
            or not isinstance(token_budget, int)
            or token_budget < 0
        ):
            raise ValueError("token_budget must be a non-negative integer")
        self._token_budget = token_budget
        self._estimator = estimator or CharacterTokenEstimator()

    async def prepare(self, messages: Sequence[Message]) -> ContextPlan:
        history = tuple(messages)
        groups = group_context_messages(history)
        if not groups:
            return ContextPlan(
                messages=(),
                source_message_count=0,
                token_budget=self._token_budget,
                estimated_tokens=0,
            )

        pinned = {
            index
            for index, group in enumerate(groups)
            if group[0].role == "system"
        }
        turns = [index for index in range(len(groups)) if index not in pinned]
        selected = set(pinned)
        if turns:
            selected.add(turns[-1])

        selected_messages = _flatten_selected_groups(groups, selected)
        estimated_tokens = self._estimate(selected_messages)
        if estimated_tokens > self._token_budget:
            raise ContextBudgetExceededError(
                required_tokens=estimated_tokens,
                token_budget=self._token_budget,
            )

        for group_index in reversed(turns[:-1]):
            candidate = selected | {group_index}
            candidate_messages = _flatten_selected_groups(groups, candidate)
            candidate_tokens = self._estimate(candidate_messages)
            if candidate_tokens > self._token_budget:
                break
            selected = candidate
            selected_messages = candidate_messages
            estimated_tokens = candidate_tokens

        return ContextPlan(
            messages=selected_messages,
            source_message_count=len(history),
            omitted_message_count=len(history) - len(selected_messages),
            token_budget=self._token_budget,
            estimated_tokens=estimated_tokens,
        )

    def _estimate(self, messages: Sequence[Message]) -> int:
        try:
            estimate = self._estimator.estimate(messages)
        except ContextPreparationError:
            raise
        except Exception as error:
            raise ContextPreparationError("token estimator failed") from error
        if (
            isinstance(estimate, bool)
            or not isinstance(estimate, int)
            or estimate < 0
        ):
            raise ContextPreparationError(
                "token estimator must return a non-negative integer"
            )
        return estimate


def group_context_messages(
    messages: Sequence[Message],
) -> tuple[tuple[Message, ...], ...]:
    """Group system messages and complete user turns without splitting tools."""

    groups: list[tuple[Message, ...]] = []
    current_turn: list[Message] = []

    for message in messages:
        if message.role == "system":
            _append_group(groups, current_turn)
            groups.append((message,))
        elif message.role == "user":
            _append_group(groups, current_turn)
            current_turn.append(message)
        else:
            current_turn.append(message)

    _append_group(groups, current_turn)
    return tuple(groups)


def _append_group(
    groups: list[tuple[Message, ...]],
    current: list[Message],
) -> None:
    if current:
        groups.append(tuple(current))
        current.clear()


def _flatten_selected_groups(
    groups: Sequence[tuple[Message, ...]],
    selected: set[int],
) -> tuple[Message, ...]:
    return tuple(
        message
        for index, group in enumerate(groups)
        if index in selected
        for message in group
    )


def _message_character_count(message: Message) -> int:
    count = len(message.role) + len(message.content) + len(message.thinking)
    for call in message.tool_calls:
        count += len(call.id) + len(call.name)
        count += len(_compact_json(call.arguments))
    if message.tool_call_id is not None:
        count += len(message.tool_call_id)
    if message.tool_name is not None:
        count += len(message.tool_name)
    return count


def _compact_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
