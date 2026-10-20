"""Ports consumed by the core runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from .domain import (
    AgentEvent,
    ApprovalDecision,
    CompactionDecision,
    ContextPlan,
    DurableSessionEvent,
    JSONObject,
    Message,
    ModelStreamEvent,
    SessionSnapshot,
    SessionCompaction,
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


class ContextStrategy(Protocol):
    """Builds the model-visible projection for one provider request."""

    async def prepare(self, messages: Sequence[Message]) -> ContextPlan:
        """Prepare context without mutating the committed session history."""
        ...


class NextTurnHook(Protocol):
    """Intercepts the safe boundary before another model request."""

    async def prepare(
        self,
        messages: Sequence[Message],
        *,
        next_request_number: int,
    ) -> Sequence[Message]:
        """Return the canonical history for the next model request."""
        ...


class TokenEstimator(Protocol):
    """Estimates model-context tokens for an ordered message sequence."""

    def estimate(self, messages: Sequence[Message]) -> int:
        """Return a deterministic, non-negative token estimate."""
        ...


class CompactionStrategy(Protocol):
    """Summarizes a validated prefix selected for durable compaction."""

    async def summarize(self, messages: Sequence[Message]) -> str:
        """Return non-empty summary text without mutating the messages."""
        ...


class CompactionPolicy(Protocol):
    """Decides whether persisted history should be compacted before a run."""

    def evaluate(self, messages: Sequence[Message]) -> CompactionDecision:
        """Return an observable decision without mutating session history."""
        ...


class SessionStore(Protocol):
    """Loads, appends, and forks model-visible session history."""

    async def load(self, session_id: str) -> SessionSnapshot:
        """Load an immutable snapshot of the current session state."""
        ...

    async def read_events(
        self,
        session_id: str,
    ) -> tuple[DurableSessionEvent, ...]:
        """Read the ordered durable facts used to rebuild the session."""
        ...

    async def append(
        self,
        session_id: str,
        expected_version: int,
        messages: Sequence[Message],
    ) -> SessionSnapshot:
        """Append messages if the persisted version still matches."""
        ...

    async def compact(
        self,
        session_id: str,
        expected_version: int,
        compaction: SessionCompaction,
    ) -> SessionSnapshot:
        """Atomically append a compaction event at the expected version."""
        ...

    async def fork(
        self,
        source_session_id: str,
        target_session_id: str,
        at_version: int | None = None,
    ) -> SessionSnapshot:
        """Persist a non-empty source prefix as a new target session."""
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
