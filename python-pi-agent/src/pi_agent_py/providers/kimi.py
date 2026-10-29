"""Kimi (Moonshot AI) Chat Completions adapter.

This module owns Kimi-specific HTTP, authentication, error mapping, and model
capability rules. Shared OpenAI-compatible wire codecs stay internal.
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
    KIMI_DEFAULT_BASE_URL,
    KIMI_DEFAULT_MODEL,
    classify_http_error,
    parse_retry_after_seconds,
    validate_openai_compatible_config,
)

KimiThinkingMode: TypeAlias = Literal["enabled", "disabled"]
KimiThinkingKeep: TypeAlias = Literal["all"]
KimiReasoningEffort: TypeAlias = Literal["low", "high", "max"]
_THINKING_MODES = {"enabled", "disabled"}
_THINKING_KEEP_VALUES = {"all"}
_REASONING_EFFORTS = {"low", "high", "max"}
_KIMI_K3_MODELS = {"kimi-k3"}
_KIMI_K27_CODE_MODELS = {
    "kimi-k2.7-code",
    "kimi-k2.7-code-highspeed",
}
_KIMI_K26_MODELS = {"kimi-k2.6"}


@dataclass(frozen=True, slots=True)
class KimiConfig:
    """Validated configuration for one Kimi provider instance."""

    api_key: str = field(repr=False)
    model: str = KIMI_DEFAULT_MODEL
    base_url: str = KIMI_DEFAULT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    # None = 不发送 thinking 字段，由模型自身决定是否思考。
    # 只有显式设为 "enabled"/"disabled" 时才写进 payload。
    thinking: KimiThinkingMode | None = None
    thinking_keep: KimiThinkingKeep | None = None
    reasoning_effort: KimiReasoningEffort | None = None
    max_tokens: int | None = None

    def __post_init__(self) -> None:
        validate_openai_compatible_config(
            provider_name="Kimi",
            api_key=self.api_key,
            model=self.model,
            base_url=self.base_url,
            timeout_seconds=self.timeout_seconds,
            max_tokens=self.max_tokens,
        )
        if self.thinking is not None and self.thinking not in _THINKING_MODES:
            raise ConfigurationError("Kimi thinking must be 'enabled', 'disabled', or None")
        if (
            self.thinking_keep is not None
            and self.thinking_keep not in _THINKING_KEEP_VALUES
        ):
            raise ConfigurationError("Kimi thinking_keep must be 'all' or None")
        if (
            self.reasoning_effort is not None
            and self.reasoning_effort not in _REASONING_EFFORTS
        ):
            raise ConfigurationError("Kimi reasoning_effort is not supported")
        self._validate_model_capabilities()

    def _validate_model_capabilities(self) -> None:
        if self.model in _KIMI_K3_MODELS:
            if self.thinking is not None or self.thinking_keep is not None:
                raise ConfigurationError(
                    "Kimi K3 does not accept the thinking parameter"
                )
            return
        if self.model in _KIMI_K27_CODE_MODELS:
            if self.thinking == "disabled":
                raise ConfigurationError("Kimi K2.7 Code thinking cannot be disabled")
            if self.reasoning_effort is not None:
                raise ConfigurationError(
                    "Kimi K2.7 Code does not support reasoning_effort"
                )
            return
        if self.model in _KIMI_K26_MODELS:
            if self.reasoning_effort is not None:
                raise ConfigurationError(
                    "Kimi K2.6 does not support reasoning_effort"
                )
            if self.thinking == "disabled" and self.thinking_keep is not None:
                raise ConfigurationError(
                    "Kimi K2.6 thinking_keep requires thinking to be enabled"
                )
            return
        if any(
            value is not None
            for value in (
                self.thinking,
                self.thinking_keep,
                self.reasoning_effort,
            )
        ):
            raise ConfigurationError(
                "unknown Kimi models cannot use model-specific thinking settings"
            )


class KimiProvider:
    """Normalize Kimi's streaming Chat Completions API."""

    def __init__(
        self,
        config: KimiConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._closed = False

    async def __aenter__(self) -> KimiProvider:
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
            raise ProviderError("Kimi provider is closed")
        if not messages:
            raise ProviderError("Kimi requires at least one message")

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
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/event-stream" not in content_type:
                    raise ProviderResponseError(
                        "Kimi streaming response is not text/event-stream"
                    )

                tool_calls = ToolCallAccumulator("Kimi")
                finish: Finish | None = None
                usage: TokenUsage | None = None
                received_done = False
                async for event_name, data in iter_sse(response):
                    # Kimi 在流中出错时会发 event: error，错误体在 data 行。
                    if event_name == "error":
                        raise ProviderResponseError(
                            f"Kimi stream failed: {_extract_stream_error(data)}"
                        )
                    # 只认 [DONE] 作为完成判据，不用 finish_reason 提前退出。
                    if data == "[DONE]":
                        received_done = True
                        break
                    for event in normalize_chunk(
                        data,
                        tool_calls,
                        provider_name="Kimi",
                    ):
                        if isinstance(event, Finish):
                            if finish is not None:
                                raise ProviderResponseError(
                                    "Kimi emitted more than one finish reason"
                                )
                            finish = event
                        elif isinstance(event, TokenUsage):
                            usage = event
                        else:
                            yield event
                if not received_done:
                    raise ProviderResponseError(
                        "Kimi stream ended before the [DONE] marker"
                    )
                if finish is None:
                    raise ProviderResponseError(
                        "Kimi stream completed without a finish reason"
                    )
                if usage is not None:
                    yield usage
                yield finish
        except ProviderError:
            raise
        except httpx.TimeoutException as error:
            raise ProviderTimeoutError(
                f"Kimi request timed out after {self._config.timeout_seconds:g}s"
            ) from error
        except httpx.HTTPError as error:
            raise ProviderTransportError(
                f"Kimi transport failed: {type(error).__name__}"
            ) from error

    def _build_payload(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
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
                    include_reasoning_content=_should_replay_reasoning(
                        self._config,
                        message_index=index,
                        last_user_index=last_user_index,
                    ),
                )
                for index, message in enumerate(messages)
            ],
            "stream": True,
            # 不传这个就拿不到流式 usage；传了会在 [DONE] 前多一个 usage 分块。
            "stream_options": {"include_usage": True},
        }
        thinking: dict[str, object] = {}
        if self._config.thinking is not None:
            thinking["type"] = self._config.thinking
        if self._config.thinking_keep is not None:
            thinking["keep"] = self._config.thinking_keep
        if thinking:
            payload["thinking"] = thinking
        if self._config.reasoning_effort is not None:
            payload["reasoning_effort"] = self._config.reasoning_effort
        if self._config.max_tokens is not None:
            payload["max_tokens"] = self._config.max_tokens
        if tools:
            payload["tools"] = [serialize_tool(definition) for definition in tools]
        return payload


def _should_replay_reasoning(
    config: KimiConfig,
    *,
    message_index: int,
    last_user_index: int,
) -> bool:
    if config.model in _KIMI_K3_MODELS | _KIMI_K27_CODE_MODELS:
        return True
    if config.thinking_keep == "all":
        return True
    return (
        config.model in _KIMI_K26_MODELS
        and config.thinking != "disabled"
        and message_index > last_user_index
    )


async def _http_error(response: httpx.Response) -> ProviderError:
    body = await response.aread()
    error_type, detail = _extract_error(body)
    return classify_http_error(
        provider_name="Kimi",
        status_code=response.status_code,
        detail=detail,
        retry_after_seconds=parse_retry_after_seconds(
            response.headers.get("Retry-After")
        ),
        # Kimi 的 429 分多种 error.type，余额不足不是限流。
        quota_exhausted=(
            response.status_code == 429
            and error_type == "exceeded_current_quota_error"
        ),
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
            error_type = error.get("type")
            message = error.get("message")
            if isinstance(message, str):
                return (
                    error_type if isinstance(error_type, str) else None,
                    message[:500],
                )
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
