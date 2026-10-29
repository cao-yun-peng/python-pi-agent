"""TiMi CC adapter for Codex, GPT, and Grok through the Responses API.

TiMi selects model access from the API-key group, so callers must name the
model explicitly. Requests are stateless (``store: false``); completed output
items are therefore preserved as opaque provider state for the next turn.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal, NoReturn, TypeAlias, cast

import httpx

from ..domain import (
    Finish,
    JSONObject,
    Message,
    ModelStreamEvent,
    ProviderRequestContext,
    ProviderState,
    TextDelta,
    ThinkingDelta,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from ..errors import (
    ConfigurationError,
    ProviderError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderTransportError,
)
from ._provider_support import (
    DEFAULT_TIMEOUT_SECONDS,
    TIMICC_DEFAULT_BASE_URL,
    classify_http_error,
    parse_retry_after_seconds,
    validate_openai_compatible_config,
)
from ._sse import iter_sse

TimiccReasoningEffort: TypeAlias = Literal[
    "none", "minimal", "low", "medium", "high", "xhigh", "max"
]
TimiccReasoningSummary: TypeAlias = Literal["auto", "concise", "detailed"]

_PROVIDER_STATE_OWNER = "timicc-responses"
_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
_REASONING_SUMMARIES = {"auto", "concise", "detailed"}
_IGNORED_EVENTS = {
    "response.created",
    "response.in_progress",
    "response.output_item.added",
    "response.content_part.added",
    "response.content_part.done",
    "response.output_text.done",
    "response.reasoning_summary_part.added",
    "response.reasoning_summary_part.done",
    "response.reasoning_summary_text.done",
    "response.reasoning_text.done",
    "response.function_call_arguments.done",
    "response.queued",
}


@dataclass(frozen=True, slots=True)
class TimiccResponsesConfig:
    """Validated TiMi Responses API settings."""

    api_key: str = field(repr=False)
    model: str
    base_url: str = TIMICC_DEFAULT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    reasoning_effort: TimiccReasoningEffort | None = None
    reasoning_summary: TimiccReasoningSummary | None = None
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        validate_openai_compatible_config(
            provider_name="TiMi Responses",
            api_key=self.api_key,
            model=self.model,
            base_url=self.base_url,
            timeout_seconds=self.timeout_seconds,
            max_tokens=self.max_output_tokens,
        )
        if (
            self.reasoning_effort is not None
            and self.reasoning_effort not in _REASONING_EFFORTS
        ):
            raise ConfigurationError("TiMi reasoning_effort is not supported")
        if (
            self.reasoning_summary is not None
            and self.reasoning_summary not in _REASONING_SUMMARIES
        ):
            raise ConfigurationError("TiMi reasoning_summary is not supported")
        if self.max_output_tokens is not None and self.max_output_tokens < 16:
            raise ConfigurationError("TiMi max_output_tokens must be at least 16")


@dataclass(slots=True)
class _FunctionParts:
    call_id: str
    name: str
    chunks: list[str] = field(default_factory=list)


class _FunctionCallAccumulator:
    def __init__(self) -> None:
        self._calls: dict[str, _FunctionParts] = {}

    def add_item(self, item: object) -> None:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return
        item_id = _required_string(item, "id", "function call item")
        call_id = _required_string(item, "call_id", "function call item")
        name = _required_string(item, "name", "function call item")
        arguments = item.get("arguments", "")
        if not isinstance(arguments, str):
            _fail("function call item arguments must be text")
        parts = self._calls.get(item_id)
        if parts is None:
            chunks = [arguments] if arguments else []
            self._calls[item_id] = _FunctionParts(call_id, name, chunks)
        elif parts.call_id != call_id or parts.name != name:
            _fail("function call identity changed while streaming")

    def add_delta(self, event: JSONObject) -> None:
        item_id = _required_string(event, "item_id", "function argument delta")
        delta = _required_string(
            event,
            "delta",
            "function argument delta",
            allow_empty=True,
        )
        parts = self._calls.get(item_id)
        if parts is None:
            _fail("function argument delta arrived before its output item")
        parts.chunks.append(delta)

    def finish_item(self, item: JSONObject) -> ToolCall:
        item_id = _required_string(item, "id", "function call item")
        self.add_item(item)
        parts = self._calls[item_id]
        final_arguments = _required_string(
            item, "arguments", "function call item", allow_empty=True
        )
        streamed_arguments = "".join(parts.chunks)
        if streamed_arguments and streamed_arguments != final_arguments:
            _fail("streamed function arguments do not match the completed item")
        raw_arguments = final_arguments or streamed_arguments or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as error:
            raise ProviderResponseError(
                "TiMi Responses tool arguments are not valid JSON"
            ) from error
        if not isinstance(arguments, dict):
            _fail("tool arguments must decode to an object")
        return ToolCall(parts.call_id, parts.name, cast(JSONObject, arguments))


class TimiccResponsesProvider:
    """Normalize TiMi's OpenAI-compatible streaming Responses endpoint."""

    def __init__(
        self,
        config: TimiccResponsesConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._closed = False

    async def __aenter__(self) -> TimiccResponsesProvider:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.aclose()

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
        *,
        context: ProviderRequestContext,
    ) -> AsyncIterator[ModelStreamEvent]:
        del context
        if self._closed:
            raise ProviderError("TiMi Responses provider is closed")
        if not messages:
            raise ProviderError("TiMi Responses requires at least one message")

        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        url = f"{self._config.base_url.rstrip('/')}/responses"
        try:
            async with self._client.stream(
                "POST",
                url,
                headers=headers,
                json=self._build_payload(messages, tools),
                timeout=self._config.timeout_seconds,
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise await _http_error(response)
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/event-stream" not in content_type:
                    raise ProviderResponseError(
                        "TiMi Responses streaming response is not text/event-stream"
                    )

                calls = _FunctionCallAccumulator()
                emitted_calls = 0
                terminal = False
                async for event_name, data in iter_sse(response):
                    payload = _decode_event(data)
                    event_type = payload.get("type")
                    if not isinstance(event_type, str):
                        _fail("stream event type must be text")
                    if event_name not in {None, event_type}:
                        _fail("SSE event name does not match payload type")

                    if event_type in {
                        "response.output_text.delta",
                        "response.refusal.delta",
                    }:
                        yield TextDelta(
                            _required_string(
                                payload,
                                "delta",
                                event_type,
                                allow_empty=True,
                            )
                        )
                    elif event_type in {
                        "response.reasoning_summary_text.delta",
                        "response.reasoning_text.delta",
                    }:
                        yield ThinkingDelta(
                            _required_string(payload, "delta", event_type, allow_empty=True)
                        )
                    elif event_type == "response.output_item.added":
                        calls.add_item(payload.get("item"))
                    elif event_type == "response.function_call_arguments.delta":
                        calls.add_delta(payload)
                    elif event_type == "response.output_item.done":
                        item = _required_object(payload, "item", event_type)
                        yield ProviderState(_PROVIDER_STATE_OWNER, deepcopy(item))
                        if item.get("type") == "function_call":
                            yield calls.finish_item(item)
                            emitted_calls += 1
                    elif event_type == "response.completed":
                        response_payload = _required_object(
                            payload,
                            "response",
                            event_type,
                        )
                        usage = response_payload.get("usage")
                        if usage is not None:
                            yield _normalize_usage(usage)
                        yield Finish("tool_calls" if emitted_calls else "stop")
                        terminal = True
                        break
                    elif event_type == "response.incomplete":
                        response_payload = _required_object(
                            payload,
                            "response",
                            event_type,
                        )
                        usage = response_payload.get("usage")
                        if usage is not None:
                            yield _normalize_usage(usage)
                        details = response_payload.get("incomplete_details")
                        reason = details.get("reason") if isinstance(details, dict) else None
                        if reason != "max_output_tokens":
                            _fail(
                                "response was incomplete: "
                                f"{reason or 'unknown reason'}"
                            )
                        yield Finish("length")
                        terminal = True
                        break
                    elif event_type in {"response.failed", "error"}:
                        raise ProviderResponseError(
                            f"TiMi Responses stream failed: {_stream_error(payload)}"
                        )
                    elif event_type not in _IGNORED_EVENTS:
                        _fail(f"unsupported stream event: {event_type}")
                if not terminal:
                    _fail("stream ended without a terminal response event")
        except ProviderError:
            raise
        except httpx.TimeoutException as error:
            raise ProviderTimeoutError(
                f"TiMi Responses request timed out after {self._config.timeout_seconds:g}s"
            ) from error
        except httpx.HTTPError as error:
            raise ProviderTransportError(
                f"TiMi Responses transport failed: {type(error).__name__}"
            ) from error

    def _build_payload(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self._config.model,
            "input": _serialize_input(messages),
            "stream": True,
            "store": False,
            # Current OpenAI responses include this state by default; keeping
            # the accepted legacy selector helps older compatible gateways.
            "include": ["reasoning.encrypted_content"],
        }
        reasoning: dict[str, str] = {}
        if self._config.reasoning_effort is not None:
            reasoning["effort"] = self._config.reasoning_effort
        if self._config.reasoning_summary is not None:
            reasoning["summary"] = self._config.reasoning_summary
        if reasoning:
            payload["reasoning"] = reasoning
        if self._config.max_output_tokens is not None:
            payload["max_output_tokens"] = self._config.max_output_tokens
        if tools:
            payload["tools"] = [_serialize_tool(tool) for tool in tools]
        return payload


def _serialize_input(messages: Sequence[Message]) -> list[object]:
    serialized: list[object] = []
    for message in messages:
        if message.role == "tool":
            serialized.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                }
            )
            continue
        if message.role == "assistant":
            owned_state = tuple(
                state
                for state in message.provider_state
                if state.provider == _PROVIDER_STATE_OWNER
            )
            if owned_state:
                serialized.extend(deepcopy(state.item) for state in owned_state)
                continue
            if message.content:
                serialized.append({"role": "assistant", "content": message.content})
            for call in message.tool_calls:
                serialized.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    }
                )
            continue
        serialized.append({"role": message.role, "content": message.content})
    return serialized


def _serialize_tool(definition: ToolDefinition) -> dict[str, object]:
    return {
        "type": "function",
        "name": definition.name,
        "description": definition.description,
        "parameters": dict(definition.input_schema),
        "strict": False,
    }


def _decode_event(data: str) -> JSONObject:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as error:
        raise ProviderResponseError(
            "TiMi Responses returned malformed SSE JSON"
        ) from error
    if not isinstance(payload, dict):
        _fail("SSE payload must be a JSON object")
    return cast(JSONObject, payload)


def _normalize_usage(value: object) -> TokenUsage:
    if not isinstance(value, dict):
        _fail("response usage must be an object")
    input_tokens = _required_int(value, "input_tokens", "response usage")
    output_tokens = _required_int(value, "output_tokens", "response usage")
    total_tokens = _required_int(value, "total_tokens", "response usage")
    cached: int | None = None
    details = value.get("input_tokens_details")
    if isinstance(details, dict):
        raw_cached = details.get("cached_tokens")
        if isinstance(raw_cached, int) and not isinstance(raw_cached, bool):
            cached = raw_cached
    return TokenUsage(input_tokens, output_tokens, total_tokens, cached)


async def _http_error(response: httpx.Response) -> ProviderError:
    body = await response.aread()
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        detail = body.decode("utf-8", errors="replace").strip()[:500]
    else:
        detail = "unrecognized error response"
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                detail = error["message"][:500]
            elif isinstance(error, str):
                detail = error[:500]
    return classify_http_error(
        provider_name="TiMi Responses",
        status_code=response.status_code,
        detail=detail or "empty response body",
        retry_after_seconds=parse_retry_after_seconds(
            response.headers.get("Retry-After")
        ),
    )


def _stream_error(payload: JSONObject) -> str:
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"][:500]
    if isinstance(payload.get("message"), str):
        return cast(str, payload["message"])[:500]
    response = payload.get("response")
    if isinstance(response, dict):
        nested = response.get("error")
        if isinstance(nested, dict) and isinstance(nested.get("message"), str):
            return nested["message"][:500]
    return "unrecognized stream error"


def _required_object(values: JSONObject, key: str, context: str) -> JSONObject:
    value = values.get(key)
    if not isinstance(value, dict):
        _fail(f"{context}.{key} must be an object")
    return cast(JSONObject, value)


def _required_string(
    values: dict[object, object],
    key: str,
    context: str,
    *,
    allow_empty: bool = False,
) -> str:
    value = values.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        _fail(f"{context}.{key} must be text")
    return value


def _required_int(values: dict[object, object], key: str, context: str) -> int:
    value = values.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _fail(f"{context}.{key} must be a non-negative integer")
    return value


def _fail(detail: str) -> NoReturn:
    raise ProviderResponseError(f"TiMi Responses {detail}")
