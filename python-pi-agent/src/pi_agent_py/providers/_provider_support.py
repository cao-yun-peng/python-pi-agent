"""Small shared helpers for first-party OpenAI-compatible providers.

The module centralizes stable defaults, basic configuration validation, and
HTTP status classification. Streaming behavior and model capability rules stay
inside each provider adapter; this is deliberately not a provider base class.
"""

from __future__ import annotations

import math
from typing import Final
from urllib.parse import urlparse

from ..errors import (
    ConfigurationError,
    ProviderAuthenticationError,
    ProviderBalanceError,
    ProviderError,
    ProviderHTTPError,
    ProviderRateLimitError,
)

DEFAULT_TIMEOUT_SECONDS: Final = 120.0

DEEPSEEK_DEFAULT_API_KEY_ENV: Final = "DEEPSEEK_API_KEY"
DEEPSEEK_DEFAULT_MODEL: Final = "deepseek-flash"
DEEPSEEK_DEFAULT_BASE_URL: Final = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_THINKING: Final = "disabled"

KIMI_DEFAULT_API_KEY_ENV: Final = "MOONSHOT_API_KEY"
KIMI_DEFAULT_MODEL: Final = "kimi-k2.7-code"
KIMI_DEFAULT_BASE_URL: Final = "https://api.moonshot.cn/v1"

GLM_DEFAULT_API_KEY_ENV: Final = "ZAI_API_KEY"
GLM_DEFAULT_MODEL: Final = "glm-5.3"
GLM_DEFAULT_BASE_URL: Final = "https://open.bigmodel.cn/api/paas/v4"

TIMICC_DEFAULT_API_KEY_ENV: Final = "TIMI_CC_API_KEY"
TIMICC_DEFAULT_BASE_URL: Final = "https://timicc.com/v1"


def validate_openai_compatible_config(
    *,
    provider_name: str,
    api_key: str,
    model: str,
    base_url: str,
    timeout_seconds: float,
    max_tokens: int | None,
) -> None:
    """Validate fields whose semantics are shared by compatible providers."""
    if not isinstance(api_key, str) or not api_key.strip():
        raise ConfigurationError(f"{provider_name} API key must not be empty")
    if not isinstance(model, str) or not model.strip():
        raise ConfigurationError(f"{provider_name} model must not be empty")
    if not isinstance(base_url, str):
        raise ConfigurationError(
            f"{provider_name} base_url must be an absolute HTTP(S) URL"
        )
    parsed_url = urlparse(base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ConfigurationError(
            f"{provider_name} base_url must be an absolute HTTP(S) URL"
        )
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ConfigurationError(
            f"{provider_name} timeout_seconds must be greater than zero"
        )
    if (
        max_tokens is not None
        and (
            not isinstance(max_tokens, int)
            or isinstance(max_tokens, bool)
            or max_tokens <= 0
        )
    ):
        raise ConfigurationError(
            f"{provider_name} max_tokens must be greater than zero"
        )


def classify_http_error(
    *,
    provider_name: str,
    status_code: int,
    detail: str,
    quota_exhausted: bool = False,
    retry_after_seconds: float | None = None,
) -> ProviderError:
    """Map common HTTP statuses while allowing provider quota overrides."""
    message = f"{provider_name} HTTP {status_code}: {detail}"
    if status_code in {401, 403}:
        return ProviderAuthenticationError(message)
    if status_code == 402 or quota_exhausted:
        return ProviderBalanceError(message)
    if status_code == 429:
        return ProviderRateLimitError(
            message,
            retry_after_seconds=retry_after_seconds,
        )
    return ProviderHTTPError(
        status_code,
        message,
        retryable=status_code >= 500,
    )


def parse_retry_after_seconds(value: str | None) -> float | None:
    """Parse the numeric Retry-After form without guessing HTTP-date clocks."""
    if value is None:
        return None
    try:
        parsed = float(value.strip())
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed
