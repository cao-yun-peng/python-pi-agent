"""GLM (Z.ai / Zhipu AI) Chat Completions adapter.

The adapter targets the official OpenAI-compatible standard endpoint. Shared
wire codecs handle compatible SSE chunks; GLM model capabilities and API error
codes remain local to this module.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal, TypeAlias

import httpx

from ..domain import (
    Finish,
    Message,
    ModelStreamEvent,
    ProviderRequestContext,
    TokenUsage,
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
    DEFAULT_TIMEOUT_SECONDS,
    GLM_DEFAULT_BASE_URL,
    GLM_DEFAULT_MODEL,
    classify_http_error,
    parse_retry_after_seconds,
    validate_openai_compatible_config,
)

GLMThinkingMode: TypeAlias = Literal["enabled", "disabled"]
GLMReasoningEffort: TypeAlias = Literal[
    "max",
    "xhigh",
    "high",
    "medium",
    "low",
    "minimal",
    "none",
]

_THINKING_MODES = {"enabled", "disabled"}
_REASONING_EFFORTS = {
    "max",
    "xhigh",
    "high",
    "medium",
    "low",
    "minimal",
    "none",
}
_GLM_53_REASONING_EFFORTS = {"max", "high", "low"}
_TOOL_STREAM_MODEL_PREFIXES = (
    "glm-5.3",
    "glm-5.2",
    "glm-5.1",
    "glm-5",
    "glm-4.7",
    "glm-4.6",
)


@dataclass(frozen=True, slots=True)
class GLMConfig:
    """Validated configuration for one GLM provider instance."""

    api_key: str = field(repr=False)
    model: str = GLM_DEFAULT_MODEL
    base_url: str = GLM_DEFAULT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    # None means the field is omitted and the selected model uses its default.
    thinking: GLMThinkingMode | None = None
    reasoning_effort: GLMReasoningEffort | None = None
    max_tokens: int | None = None
    user_id: str | None = None

    def __post_init__(self) -> None:
        validate_openai_compatible_config(
            provider_name="GLM",
            api_key=self.api_key,
            model=self.model,
            base_url=self.base_url,
            timeout_seconds=self.timeout_seconds,
            max_tokens=self.max_tokens,
        )
        if self.thinking is not None and self.thinking not in _THINKING_MODES:
            raise ConfigurationError("GLM thinking must be 'enabled', 'disabled', or None")
        if (
            self.reasoning_effort is not None
            and self.reasoning_effort not in _REASONING_EFFORTS
        ):
            raise ConfigurationError("GLM reasoning_effort is not supported")
        if self.max_tokens is not None and self.max_tokens > 131_072:
            raise ConfigurationError("GLM max_tokens must be at most 131072")
        if self.user_id is not None and not 6 <= len(self.user_id) <= 128:
            raise ConfigurationError("GLM user_id must contain 6 to 128 characters")
        self._validate_model_capabilities()

    def _validate_model_capabilities(self) -> None:
        model = self.model.lower()
        if model.startswith("glm-5.3"):
            if self.thinking == "disabled":
                raise ConfigurationError("GLM 5.3 thinking cannot be disabled")
            if (
                self.reasoning_effort is not None
                and self.reasoning_effort not in _GLM_53_REASONING_EFFORTS
            ):
                raise ConfigurationError(
                    "GLM 5.3 reasoning_effort supports only 'low', 'high', or 'max'"
                )
            return
        if model.startswith("glm-5.2"):
            return
        if model.startswith(("glm-5.1", "glm-5", "glm-4.7", "glm-4.6", "glm-4.5")):
            if self.reasoning_effort is not None:
                raise ConfigurationError(
                    f"{self.model} does not support reasoning_effort"
                )
            return
        if self.thinking is not None or self.reasoning_effort is not None:
            raise ConfigurationError(
                "unknown GLM models cannot use model-specific thinking settings"
            )


class GLMProvider:
    """Normalize GLM's standard streaming Chat Completions API."""

    def __init__(
        self,
        config: GLMConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._closed = False

    async def __aenter__(self) -> GLMProvider:
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
        """Send history to GLM and yield provider-neutral stream events."""
        if self._closed:
            raise ProviderError("GLM provider is closed")
        if not messages:
            raise ProviderError("GLM requires at least one message")
        if not 6 <= len(context.request_id) <= 64:
            raise ProviderError("GLM request_id must contain 6 to 64 characters")

        payload = self._build_payload(messages, tools, context=context)
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
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/event-stream" not in content_type:
                    raise ProviderResponseError(
                        "GLM streaming response is not text/event-stream"
                    )

                tool_calls = ToolCallAccumulator("GLM")
                finish: Finish | None = None
                usage: TokenUsage | None = None
                received_done = False
                async for event_name, data in iter_sse(response):
                    if event_name == "error":
                        raise ProviderResponseError(
                            f"GLM stream failed: {_extract_stream_error(data)}"
                        )
                    if data == "[DONE]":
                        received_done = True
                        break
                    for event in normalize_chunk(
                        data,
                        tool_calls,
                        provider_name="GLM",
                    ):
                        if isinstance(event, Finish):
                            if finish is not None:
                                raise ProviderResponseError(
                                    "GLM emitted more than one finish reason"
                                )
                            finish = event
                        elif isinstance(event, TokenUsage):
                            usage = event
                        else:
                            yield event
                if not received_done:
                    raise ProviderResponseError(
                        "GLM stream ended before the [DONE] marker"
                    )
                if finish is None:
                    raise ProviderResponseError(
                        "GLM stream completed without a finish reason"
                    )
                if usage is not None:
                    yield usage
                yield finish
        except ProviderError:
            raise
        except httpx.TimeoutException as error:
            raise ProviderTimeoutError(
                f"GLM request timed out after {self._config.timeout_seconds:g}s"
            ) from error
        except httpx.HTTPError as error:
            raise ProviderTransportError(
                f"GLM transport failed: {type(error).__name__}"
            ) from error

    def _build_payload(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        context: ProviderRequestContext,
    ) -> dict[str, object]:
        last_user_index = max(
            (index for index, message in enumerate(messages) if message.role == "user"),
            default=-1,
        )
        payload: dict[str, object] = {
            "model": self._config.model,
            "messages": [
                serialize_message(
                    message,
                    include_reasoning_content=(
                        index > last_user_index
                        and message.role == "assistant"
                        and bool(message.tool_calls)
                    ),
                )
                for index, message in enumerate(messages)
            ],
            "stream": True,
            "request_id": context.request_id,
        }
        if self._config.thinking is not None:
            payload["thinking"] = {"type": self._config.thinking}
        if self._config.reasoning_effort is not None:
            payload["reasoning_effort"] = self._config.reasoning_effort
        if self._config.max_tokens is not None:
            payload["max_tokens"] = self._config.max_tokens
        if self._config.user_id is not None:
            payload["user_id"] = self._config.user_id
        if tools:
            payload["tools"] = [serialize_tool(definition) for definition in tools]
            if _supports_tool_stream(self._config.model):
                payload["tool_stream"] = True
        return payload


def _supports_tool_stream(model: str) -> bool:
    normalized = model.lower()
    return normalized.startswith(_TOOL_STREAM_MODEL_PREFIXES)


async def _http_error(response: httpx.Response) -> ProviderError:
    body = await response.aread()
    code, detail = _extract_error(body)
    return classify_http_error(
        provider_name="GLM",
        status_code=response.status_code,
        detail=detail,
        retry_after_seconds=parse_retry_after_seconds(
            response.headers.get("Retry-After")
        ),
        quota_exhausted=response.status_code == 429 and code == "1113",
    )


def _extract_error(body: bytes) -> tuple[str | None, str]:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        text = body.decode("utf-8", errors="replace").strip()
        return None, text[:500] or "empty response body"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message")
            normalized_code = str(code) if isinstance(code, (str, int)) else None
            if isinstance(message, str):
                return normalized_code, message[:500]
    return None, "unrecognized error response"


def _extract_stream_error(data: str) -> str:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return data[:500] or "empty error event"
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str):
            return message[:500]
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message[:500]
    return "unrecognized stream error"
