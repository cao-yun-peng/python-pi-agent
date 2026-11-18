"""Internal codecs shared by OpenAI-compatible Chat Completions providers.

This module intentionally stops at the wire-format boundary. Provider-specific
authentication, endpoint construction, error mapping, and model capability
rules remain in each adapter.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import NoReturn, cast

from ..domain import (
    Finish,
    JSONObject,
    Message,
    ModelStreamEvent,
    TextDelta,
    ThinkingDelta,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from ..errors import ProviderResponseError
from ._sse import iter_sse as iter_sse


def serialize_message(
    message: Message,
    *,
    include_reasoning_content: bool,
    include_empty_reasoning_content: bool = False,
) -> dict[str, object]:
    """Serialize one domain message to the compatible chat message shape."""
    if message.role == "assistant" and message.tool_calls:
        serialized: dict[str, object] = {
            "role": "assistant",
            "content": message.content or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                }
                for call in message.tool_calls
            ],
        }
        if include_reasoning_content and (
            message.thinking or include_empty_reasoning_content
        ):
            serialized["reasoning_content"] = message.thinking
        return serialized
    if message.role == "tool":
        return {
            "role": "tool",
            "content": message.content,
            "tool_call_id": message.tool_call_id,
        }
    serialized = {"role": message.role, "content": message.content}
    if (
        message.role == "assistant"
        and include_reasoning_content
        and (message.thinking or include_empty_reasoning_content)
    ):
        serialized["reasoning_content"] = message.thinking
    return serialized


def serialize_tool(definition: ToolDefinition) -> dict[str, object]:
    """Serialize one provider-neutral tool definition."""
    return {
        "type": "function",
        "function": {
            "name": definition.name,
            "description": definition.description,
            "parameters": dict(definition.input_schema),
        },
    }


@dataclass(slots=True)
class _ToolCallParts:
    id: str | None = None
    name: str | None = None
    argument_chunks: list[str] = field(default_factory=list)


class ToolCallAccumulator:
    """Fold indexed OpenAI-compatible tool-call deltas."""

    def __init__(self, provider_name: str) -> None:
        self._provider_name = provider_name
        self._calls: dict[int, _ToolCallParts] = {}

    @property
    def has_calls(self) -> bool:
        return bool(self._calls)

    def add(self, raw: object) -> None:
        if not isinstance(raw, dict):
            self._fail("tool call delta must be an object")
        index = raw.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            self._fail("tool call index must be an integer")
        parts = self._calls.setdefault(index, _ToolCallParts())

        call_id = raw.get("id")
        if call_id is not None:
            if not isinstance(call_id, str) or not call_id:
                self._fail("tool call id must be a string")
            if parts.id is not None and parts.id != call_id:
                self._fail("changed a streamed tool call id")
            parts.id = call_id

        function = raw.get("function")
        if function is None:
            return
        if not isinstance(function, dict):
            self._fail("tool call function must be an object")
        name = function.get("name")
        if name is not None:
            if not isinstance(name, str) or not name:
                self._fail("tool name must be a string")
            if parts.name is not None and parts.name != name:
                self._fail("changed a streamed tool name")
            parts.name = name
        arguments = function.get("arguments")
        if arguments is not None:
            if not isinstance(arguments, str):
                self._fail("streamed tool arguments must be text")
            parts.argument_chunks.append(arguments)

    def build(self) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index in sorted(self._calls):
            parts = self._calls[index]
            if parts.id is None or parts.name is None:
                self._fail("tool call is missing id or function name")
            raw_arguments = "".join(parts.argument_chunks) or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as error:
                raise ProviderResponseError(
                    f"{self._provider_name} tool arguments are not valid JSON"
                ) from error
            if not isinstance(arguments, dict):
                self._fail("tool arguments must decode to an object")
            calls.append(
                ToolCall(
                    id=parts.id,
                    name=parts.name,
                    arguments=cast(JSONObject, arguments),
                )
            )
        return calls

    def _fail(self, detail: str) -> NoReturn:
        raise ProviderResponseError(f"{self._provider_name} {detail}")


def normalize_chunk(
    data: str,
    tool_calls: ToolCallAccumulator,
    *,
    provider_name: str,
) -> Iterator[ModelStreamEvent]:
    """Normalize one compatible Chat Completions chunk."""
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError as error:
        raise ProviderResponseError(
            f"{provider_name} returned malformed SSE JSON"
        ) from error

    if not isinstance(chunk, dict):
        raise ProviderResponseError(
            f"{provider_name} SSE payload must be a JSON object"
        )

    api_error = chunk.get("error")
    if isinstance(api_error, dict):
        message = api_error.get("message")
        detail = message if isinstance(message, str) else "unknown streaming error"
        raise ProviderResponseError(f"{provider_name} stream failed: {detail}")

    choices = chunk.get("choices")
    if choices is not None and not isinstance(choices, list):
        raise ProviderResponseError(f"{provider_name} choices must be a list")
    choice_usage: object = None
    if choices:
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ProviderResponseError(f"{provider_name} choice must be an object")
        delta = choice.get("delta")
        if delta is not None and not isinstance(delta, dict):
            raise ProviderResponseError(f"{provider_name} delta must be an object")
        if isinstance(delta, dict):
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                yield ThinkingDelta(reasoning)
            content = delta.get("content")
            if isinstance(content, str) and content:
                yield TextDelta(content)
            raw_tool_calls = delta.get("tool_calls")
            if raw_tool_calls is not None:
                if not isinstance(raw_tool_calls, list):
                    raise ProviderResponseError(
                        f"{provider_name} delta.tool_calls must be a list"
                    )
                for raw_tool_call in raw_tool_calls:
                    tool_calls.add(raw_tool_call)

        finish_reason = choice.get("finish_reason")
        choice_usage = choice.get("usage")
        if finish_reason is not None:
            if tool_calls.has_calls:
                yield from tool_calls.build()
            if finish_reason == "stop":
                yield Finish("stop")
            elif finish_reason == "length":
                yield Finish("length")
            elif finish_reason == "tool_calls":
                yield Finish("tool_calls")
            else:
                raise ProviderResponseError(
                    f"{provider_name} finish reason is not supported yet: "
                    f"{finish_reason}"
                )

    usage = chunk.get("usage")
    if not isinstance(usage, dict):
        usage = choice_usage
    if isinstance(usage, dict):
        yield normalize_usage(usage, provider_name=provider_name)


def normalize_usage(
    usage: dict[object, object],
    *,
    provider_name: str,
) -> TokenUsage:
    """Normalize common usage fields and known cache-token locations."""
    input_tokens = _required_int(usage, "prompt_tokens", provider_name)
    output_tokens = _required_int(usage, "completion_tokens", provider_name)
    total_tokens = _required_int(usage, "total_tokens", provider_name)
    cached_input_tokens: int | None = None
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = details.get("cached_tokens")
        if isinstance(cached, int) and not isinstance(cached, bool):
            cached_input_tokens = cached
    if cached_input_tokens is None:
        cached = usage.get("cached_tokens")
        if isinstance(cached, int) and not isinstance(cached, bool):
            cached_input_tokens = cached
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_input_tokens=cached_input_tokens,
    )


def _required_int(
    values: dict[object, object],
    key: str,
    provider_name: str,
) -> int:
    value = values.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProviderResponseError(
            f"{provider_name} usage.{key} must be an integer"
        )
    return value
