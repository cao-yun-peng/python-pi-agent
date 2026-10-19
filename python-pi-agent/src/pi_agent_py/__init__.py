"""Teaching-first building blocks for a modular Python agent harness."""

from .domain import (
    AgentEvent,
    AgentResult,
    Finish,
    Message,
    TextDelta,
    ThinkingDelta,
    TokenUsage,
)
from .errors import (
    AgentError,
    ConfigurationError,
    ModelProtocolError,
    ProviderAuthenticationError,
    ProviderBalanceError,
    ProviderError,
    ProviderHTTPError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from .events import CallbackEventSink, InMemoryEventSink, NullEventSink
from .runtime import Agent
from .testing import FakeProvider, WaitUntilCancelled

__all__ = [
    "Agent",
    "AgentError",
    "AgentEvent",
    "AgentResult",
    "CallbackEventSink",
    "ConfigurationError",
    "FakeProvider",
    "Finish",
    "InMemoryEventSink",
    "Message",
    "ModelProtocolError",
    "NullEventSink",
    "ProviderError",
    "ProviderAuthenticationError",
    "ProviderBalanceError",
    "ProviderHTTPError",
    "ProviderRateLimitError",
    "ProviderResponseError",
    "ProviderTimeoutError",
    "TextDelta",
    "ThinkingDelta",
    "TokenUsage",
    "WaitUntilCancelled",
]
