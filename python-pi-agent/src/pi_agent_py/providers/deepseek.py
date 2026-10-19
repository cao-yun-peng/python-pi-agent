"""DeepSeek Chat Completions adapter.

This module owns DeepSeek-specific HTTP, authentication, and SSE parsing. The
runtime sees only provider-neutral model events.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal, TypeAlias
from urllib.parse import urlparse

import httpx

from ..domain import (
    Finish,
    Message,
    ModelStreamEvent,
    TextDelta,
    ThinkingDelta,
    TokenUsage,
)
from ..errors import (
    ConfigurationError,
    ProviderAuthenticationError,
    ProviderBalanceError,
    ProviderError,
    ProviderHTTPError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
)

ThinkingMode: TypeAlias = Literal["enabled", "disabled"]
ReasoningEffort: TypeAlias = Literal[
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
]

_USER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,512}$")
_THINKING_MODES = {"enabled", "disabled"}
_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max"}


@dataclass(frozen=True, slots=True)
class DeepSeekConfig:
    """Validated configuration for one DeepSeek provider instance."""

    api_key: str = field(repr=False)
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    timeout_seconds: float = 120.0
    thinking: ThinkingMode = "disabled"
    reasoning_effort: ReasoningEffort | None = None
    max_tokens: int | None = None
    user_id: str | None = None

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ConfigurationError("DeepSeek API key must not be empty")
        if not self.model.strip():
            raise ConfigurationError("DeepSeek model must not be empty")
        parsed_url = urlparse(self.base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ConfigurationError("DeepSeek base_url must be an absolute HTTP(S) URL")
        if self.timeout_seconds <= 0:
            raise ConfigurationError("DeepSeek timeout_seconds must be greater than zero")
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise ConfigurationError("DeepSeek max_tokens must be greater than zero")
        if self.thinking not in _THINKING_MODES:
            raise ConfigurationError("DeepSeek thinking must be 'enabled' or 'disabled'")
        if (
            self.reasoning_effort is not None
            and self.reasoning_effort not in _REASONING_EFFORTS
        ):
            raise ConfigurationError("DeepSeek reasoning_effort is not supported")
        if self.reasoning_effort is not None and self.thinking == "disabled":
            raise ConfigurationError(
                "DeepSeek reasoning_effort requires thinking='enabled'"
            )
        if self.user_id is not None and not _USER_ID_PATTERN.fullmatch(self.user_id):
            raise ConfigurationError(
                "DeepSeek user_id must match [a-zA-Z0-9_-]+ and be at most 512 characters"
            )


class DeepSeekProvider:
    """Normalize DeepSeek's streaming Chat Completions API."""

    def __init__(
        self,
        config: DeepSeekConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._closed = False

    async def __aenter__(self) -> DeepSeekProvider:
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
        """Release an internally-created HTTP client."""
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.aclose()

    async def stream(
        self,
        messages: Sequence[Message],
    ) -> AsyncIterator[ModelStreamEvent]:
        """Send model-visible history and yield normalized stream events."""
        if self._closed:
            raise ProviderError("DeepSeek provider is closed")
        if not messages:
            raise ProviderError("DeepSeek requires at least one message")

        payload = self._build_payload(messages)
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"

        try:
            async with self._client.stream(
                "POST",
                url,
                headers=headers,
                json=payload,
                timeout=self._config.timeout_seconds,
            ) as response:
                if response.status_code != 200:
                    raise await _http_error(response)
                content_type = response.headers.get("Content-Type", "")
                if "text/event-stream" not in content_type:
                    raise ProviderResponseError(
                        "DeepSeek streaming response is not text/event-stream"
                    )

                async for data in _iter_sse_data(response):
                    if data == "[DONE]":
                        return
                    for event in _normalize_chunk(data):
                        yield event
        except ProviderError:
            raise
        except httpx.TimeoutException as error:
            raise ProviderTimeoutError(
                f"DeepSeek request timed out after {self._config.timeout_seconds:g}s"
            ) from error
        except httpx.HTTPError as error:
            raise ProviderError(f"DeepSeek transport failed: {type(error).__name__}") from error

    def _build_payload(self, messages: Sequence[Message]) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self._config.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            "thinking": {"type": self._config.thinking},
        }
        if self._config.reasoning_effort is not None:
            payload["reasoning_effort"] = self._config.reasoning_effort
        if self._config.max_tokens is not None:
            payload["max_tokens"] = self._config.max_tokens
        if self._config.user_id is not None:
            payload["user_id"] = self._config.user_id
        return payload


async def _iter_sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """Yield complete SSE data fields while ignoring comments and keep-alives."""
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_lines:
                yield "\n".join(data_lines)
                data_lines.clear()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    if data_lines:
        yield "\n".join(data_lines)


def _normalize_chunk(data: str) -> Iterator[ModelStreamEvent]:
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError as error:
        raise ProviderResponseError("DeepSeek returned malformed SSE JSON") from error

    if not isinstance(chunk, dict):
        raise ProviderResponseError("DeepSeek SSE payload must be a JSON object")

    api_error = chunk.get("error")
    if isinstance(api_error, dict):
        message = api_error.get("message")
        detail = message if isinstance(message, str) else "unknown streaming error"
        raise ProviderResponseError(f"DeepSeek stream failed: {detail}")

    choices = chunk.get("choices")
    if choices is not None and not isinstance(choices, list):
        raise ProviderResponseError("DeepSeek choices must be a list")
    if choices:
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ProviderResponseError("DeepSeek choice must be an object")
        delta = choice.get("delta")
        if delta is not None and not isinstance(delta, dict):
            raise ProviderResponseError("DeepSeek delta must be an object")
        if isinstance(delta, dict):
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                yield ThinkingDelta(reasoning)
            content = delta.get("content")
            if isinstance(content, str) and content:
                yield TextDelta(content)
            if delta.get("tool_calls"):
                raise ProviderResponseError(
                    "DeepSeek returned tool calls, but this milestone has no tool runtime"
                )

        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            if finish_reason == "stop":
                yield Finish("stop")
            elif finish_reason == "length":
                yield Finish("length")
            else:
                raise ProviderResponseError(
                    f"DeepSeek finish reason is not supported yet: {finish_reason}"
                )

    usage = chunk.get("usage")
    if isinstance(usage, dict):
        yield _normalize_usage(usage)


def _normalize_usage(usage: dict[object, object]) -> TokenUsage:
    input_tokens = _required_int(usage, "prompt_tokens")
    output_tokens = _required_int(usage, "completion_tokens")
    total_tokens = _required_int(usage, "total_tokens")
    cached_input_tokens: int | None = None
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = details.get("cached_tokens")
        if isinstance(cached, int) and not isinstance(cached, bool):
            cached_input_tokens = cached
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_input_tokens=cached_input_tokens,
    )


def _required_int(values: dict[object, object], key: str) -> int:
    value = values.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProviderResponseError(f"DeepSeek usage.{key} must be an integer")
    return value


async def _http_error(response: httpx.Response) -> ProviderError:
    body = await response.aread()
    detail = _extract_error_message(body)
    message = f"DeepSeek HTTP {response.status_code}: {detail}"
    if response.status_code in {401, 403}:
        return ProviderAuthenticationError(message)
    if response.status_code == 402:
        return ProviderBalanceError(message)
    if response.status_code == 429:
        return ProviderRateLimitError(message)
    return ProviderHTTPError(
        response.status_code,
        message,
        retryable=response.status_code >= 500,
    )


def _extract_error_message(body: bytes) -> str:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        text = body.decode("utf-8", errors="replace").strip()
        return text[:500] or "empty response body"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message[:500]
    return "unrecognized error response"
