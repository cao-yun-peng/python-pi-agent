"""Compaction preparation, model summarization, and summary serialization."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass

from .context import CharacterTokenEstimator, group_context_messages
from .domain import (
    CompactionDecision,
    Finish,
    Message,
    TextDelta,
    ThinkingDelta,
    TokenUsage,
    ToolCall,
)
from .errors import (
    CompactionError,
    CompactionNotNeededError,
    CompactionPolicyError,
)
from .ports import ModelProvider, TokenEstimator

_TOOL_RESULT_MAX_CHARS = 2_000
_SUMMARY_PREFIX = "<conversation-summary>"
_SUMMARY_SUFFIX = "</conversation-summary>"
_DEFAULT_SUMMARY_PROMPT = (
    "Summarize the earlier conversation for another agent.\n"
    "Preserve goals, constraints, decisions, progress, failures, tool findings, "
    "and next steps.\n"
    "Do not invent facts. Return only the summary."
)


@dataclass(frozen=True, slots=True)
class CompactionPreparation:
    """A safe prefix selected for summary plus the tail kept verbatim."""

    messages_to_summarize: tuple[Message, ...]
    kept_messages: tuple[Message, ...]
    replace_start: int
    source_message_count: int


class TokenThresholdCompactionPolicy:
    """Trigger compaction when estimated message tokens reach a threshold."""

    def __init__(
        self,
        token_threshold: int,
        *,
        keep_recent_turns: int = 1,
        estimator: TokenEstimator | None = None,
    ) -> None:
        if type(token_threshold) is not int or token_threshold <= 0:
            raise ValueError("token_threshold must be a positive integer")
        if type(keep_recent_turns) is not int or keep_recent_turns <= 0:
            raise ValueError("keep_recent_turns must be a positive integer")
        self._token_threshold = token_threshold
        self._keep_recent_turns = keep_recent_turns
        self._estimator = estimator or CharacterTokenEstimator()

    def evaluate(self, messages: Sequence[Message]) -> CompactionDecision:
        history = tuple(messages)
        try:
            estimated_tokens = self._estimator.estimate(history)
        except Exception as error:
            raise CompactionPolicyError("token estimator failed") from error
        if type(estimated_tokens) is not int or estimated_tokens < 0:
            raise CompactionPolicyError(
                "token estimator must return a non-negative integer"
            )
        return CompactionDecision(
            should_compact=estimated_tokens >= self._token_threshold,
            estimated_tokens=estimated_tokens,
            token_threshold=self._token_threshold,
            keep_recent_turns=self._keep_recent_turns,
        )


def prepare_compaction(
    messages: Sequence[Message],
    *,
    keep_recent_turns: int = 1,
) -> CompactionPreparation:
    """Select complete old turns while preserving leading system messages."""
    if (
        isinstance(keep_recent_turns, bool)
        or not isinstance(keep_recent_turns, int)
        or keep_recent_turns <= 0
    ):
        raise ValueError("keep_recent_turns must be a positive integer")

    history = tuple(messages)
    replace_start = 0
    while (
        replace_start < len(history)
        and history[replace_start].role == "system"
    ):
        replace_start += 1

    if any(message.role == "system" for message in history[replace_start:]):
        raise CompactionError(
            "compaction requires all system messages to precede conversation turns"
        )

    turns = group_context_messages(history[replace_start:])
    if len(turns) <= keep_recent_turns:
        raise CompactionNotNeededError(
            "session does not contain an older complete turn to compact"
        )

    summarized_turns = turns[:-keep_recent_turns]
    kept_turns = turns[-keep_recent_turns:]
    messages_to_summarize = _flatten_groups(summarized_turns)
    kept_messages = _flatten_groups(kept_turns)
    return CompactionPreparation(
        messages_to_summarize=messages_to_summarize,
        kept_messages=kept_messages,
        replace_start=replace_start,
        source_message_count=len(history),
    )


class ModelCompactionStrategy:
    """Generate a semantic summary through a dedicated ModelProvider."""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        system_prompt: str = _DEFAULT_SUMMARY_PROMPT,
    ) -> None:
        if not system_prompt.strip():
            raise ValueError("compaction system_prompt must not be empty")
        self._provider = provider
        self._system_prompt = system_prompt

    async def summarize(self, messages: Sequence[Message]) -> str:
        normalized = tuple(messages)
        if not normalized:
            raise CompactionError("messages_to_summarize must not be empty")

        request = (
            Message(role="system", content=self._system_prompt),
            Message(role="user", content=serialize_conversation(normalized)),
        )
        chunks: list[str] = []
        finish: Finish | None = None
        try:
            async for event in self._provider.stream(request, ()):
                if isinstance(event, TextDelta):
                    if finish is not None:
                        raise CompactionError("summary text arrived after finish")
                    chunks.append(event.text)
                elif isinstance(event, (ThinkingDelta, TokenUsage)):
                    if finish is not None:
                        raise CompactionError(
                            "summary metadata arrived after finish"
                        )
                elif isinstance(event, ToolCall):
                    raise CompactionError("summary provider must not call tools")
                elif isinstance(event, Finish):
                    if finish is not None:
                        raise CompactionError(
                            "summary provider emitted more than one finish"
                        )
                    finish = event
                else:
                    raise CompactionError(
                        f"unsupported summary event: {type(event).__name__}"
                    )
        except asyncio.CancelledError:
            raise
        except CompactionError:
            raise
        except Exception as error:
            raise CompactionError("summary provider failed") from error

        if finish is None:
            raise CompactionError("summary provider stream ended without finish")
        if finish.reason != "stop":
            raise CompactionError(
                f"summary provider stopped with {finish.reason!r}"
            )
        summary = "".join(chunks).strip()
        if not summary:
            raise CompactionError("summary provider returned empty text")
        return summary


def build_summary_message(summary: str) -> Message:
    """Create the portable user-role message persisted by compaction."""
    normalized = summary.strip()
    if not normalized:
        raise CompactionError("compaction summary must not be empty")
    return Message(
        role="user",
        content=f"{_SUMMARY_PREFIX}\n{normalized}\n{_SUMMARY_SUFFIX}",
    )


def serialize_conversation(messages: Sequence[Message]) -> str:
    """Serialize model-visible history into deterministic summary input."""
    sections: list[str] = []
    for message in messages:
        if message.role == "user":
            sections.append(f"[User]\n{message.content}")
        elif message.role == "assistant":
            if message.thinking:
                sections.append(f"[Assistant thinking]\n{message.thinking}")
            if message.content:
                sections.append(f"[Assistant]\n{message.content}")
            if message.tool_calls:
                calls = "\n".join(
                    f"{call.name}("
                    f"{json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)}"
                    ")"
                    for call in message.tool_calls
                )
                sections.append(f"[Assistant tool calls]\n{calls}")
        elif message.role == "tool":
            content = _truncate_tool_result(message.content)
            sections.append(f"[Tool result: {message.tool_name}]\n{content}")
        else:
            sections.append(f"[System]\n{message.content}")
    return "\n\n".join(sections)


def _truncate_tool_result(content: str) -> str:
    if len(content) <= _TOOL_RESULT_MAX_CHARS:
        return content
    omitted = len(content) - _TOOL_RESULT_MAX_CHARS
    return (
        f"{content[:_TOOL_RESULT_MAX_CHARS]}\n\n"
        f"[... {omitted} more characters truncated]"
    )


def _flatten_groups(
    groups: Sequence[tuple[Message, ...]],
) -> tuple[Message, ...]:
    return tuple(message for group in groups for message in group)
