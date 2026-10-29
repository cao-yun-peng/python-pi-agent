"""DeepSeek Chat Completions adapter.

This module owns DeepSeek-specific HTTP, authentication, error mapping, and
model options. Shared OpenAI-compatible wire codecs stay internal.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal, TypeAlias

import httpx

from ..domain import (
    Message,
    ModelStreamEvent,
    ProviderRequestContext,
    ToolDefinition,
)
from ..errors import (
    ConfigurationError,
    ProviderError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderTransportError,
)
from ._openai_compat import (
    ToolCallAccumulator,
    iter_sse,
    normalize_chunk,
    serialize_message,
    serialize_tool,
)
from ._provider_support import (
    DEEPSEEK_DEFAULT_BASE_URL,
    DEEPSEEK_DEFAULT_MODEL,
    DEEPSEEK_DEFAULT_THINKING,
    DEFAULT_TIMEOUT_SECONDS,
    classify_http_error,
    parse_retry_after_seconds,
    validate_openai_compatible_config,
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
    model: str = DEEPSEEK_DEFAULT_MODEL
    base_url: str = DEEPSEEK_DEFAULT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    thinking: ThinkingMode = DEEPSEEK_DEFAULT_THINKING
    reasoning_effort: ReasoningEffort | None = None
    max_tokens: int | None = None
    user_id: str | None = None

    def __post_init__(self) -> None:
        validate_openai_compatible_config(
            provider_name="DeepSeek",
            api_key=self.api_key,
            model=self.model,
            base_url=self.base_url,
            timeout_seconds=self.timeout_seconds,
            max_tokens=self.max_tokens,
        )
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
        tools: Sequence[ToolDefinition] = (),
        *,
        context: ProviderRequestContext,
    ) -> AsyncIterator[ModelStreamEvent]:
        """Send model-visible history and yield normalized stream events."""
        del context
        if self._closed:
            raise ProviderError("DeepSeek provider is closed")
        if not messages:
            raise ProviderError("DeepSeek requires at least one message")

        payload = self._build_payload(messages, tools)
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

                tool_calls = ToolCallAccumulator("DeepSeek")
                async for _event_name, data in iter_sse(response):
                    if data == "[DONE]":
                        return
                    for event in normalize_chunk(
                        data,
                        tool_calls,
                        provider_name="DeepSeek",
                    ):
                        yield event
        except ProviderError:
            raise
        except httpx.TimeoutException as error:
            raise ProviderTimeoutError(
                f"DeepSeek request timed out after {self._config.timeout_seconds:g}s"
            ) from error
        except httpx.HTTPError as error:
            raise ProviderTransportError(
                f"DeepSeek transport failed: {type(error).__name__}"
            ) from error

    def _build_payload(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> dict[str, object]:
        include_reasoning_content = (
            self._config.thinking == "enabled" and bool(tools)
        )
        payload: dict[str, object] = {
            "model": self._config.model,
            "messages": [
                serialize_message(
                    message,
                    include_reasoning_content=include_reasoning_content,
                    include_empty_reasoning_content=True,
                )
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
        if tools:
            payload["tools"] = [serialize_tool(definition) for definition in tools]
        return payload


async def _http_error(response: httpx.Response) -> ProviderError:
    body = await response.aread()
    detail = _extract_error_message(body)
    return classify_http_error(
        provider_name="DeepSeek",
        status_code=response.status_code,
        detail=detail,
        retry_after_seconds=parse_retry_after_seconds(
            response.headers.get("Retry-After")
        ),
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
