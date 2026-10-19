"""Ports consumed by the core runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from .domain import (
    AgentEvent,
    ApprovalDecision,
    JSONObject,
    Message,
    ModelStreamEvent,
    SessionSnapshot,
    ToolCall,
    ToolDefinition,
    ToolOutput,
)


class ModelProvider(Protocol):
    """Produces a normalized stream independently of any model vendor SDK."""

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ModelStreamEvent]:
        """Stream one assistant response for the supplied model-visible history."""
        ...


class EventSink(Protocol):
    """Accepts ordered runtime events without performing blocking I/O."""

    def emit(self, event: AgentEvent) -> None:
        """Record or enqueue an event and return immediately."""
        ...


class SessionStore(Protocol):
    """Loads and atomically appends model-visible session history."""

    async def load(self, session_id: str) -> SessionSnapshot:
        """Load an immutable snapshot of the current session state."""
        ...

    async def append(
        self,
        session_id: str,
        expected_version: int,
        messages: Sequence[Message],
    ) -> SessionSnapshot:
        """Append messages if the persisted version still matches."""
        ...


class Tool(Protocol):
    """A validated, cancellable capability callable by the model."""

    @property
    def definition(self) -> ToolDefinition:
        """Return model-facing schema and runtime safety metadata."""
        ...

    async def execute(self, arguments: JSONObject) -> ToolOutput:
        """Execute validated arguments in the current asyncio task."""
        ...


class ApprovalPolicy(Protocol):
    """Reviews tool calls that explicitly require approval."""

    async def review(
        self,
        call: ToolCall,
        definition: ToolDefinition,
    ) -> ApprovalDecision:
        """Approve or deny one validated tool call."""
        ...
