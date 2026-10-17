"""Teaching-first building blocks for a modular Python agent harness."""

from .domain import (
    AgentEvent,
    AgentResult,
    Finish,
    Message,
    TextDelta,
)
from .errors import AgentError, ModelProtocolError, ProviderError
from .events import CallbackEventSink, InMemoryEventSink, NullEventSink
from .runtime import Agent
from .testing import FakeProvider, WaitUntilCancelled

__all__ = [
    "Agent",
    "AgentError",
    "AgentEvent",
    "AgentResult",
    "CallbackEventSink",
    "FakeProvider",
    "Finish",
    "InMemoryEventSink",
    "Message",
    "ModelProtocolError",
    "NullEventSink",
    "ProviderError",
    "TextDelta",
    "WaitUntilCancelled",
]
