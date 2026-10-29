"""Ports consumed by the core runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .model_runtime import (
        AttemptFailure,
        ProviderCandidate,
        RetryDecision,
    )

from .domain import (
    AgentEvent,
    ApprovalDecision,
    CompactionDecision,
    ControlTransition,
    ContextPlan,
    DurableSessionEvent,
    JSONObject,
    Message,
    ModelStreamEvent,
    NextTurnContext,
    ProviderRequestContext,
    SandboxProcessRequest,
    SandboxProcessResult,
    SessionEventContext,
    SessionEventKind,
    SessionSnapshot,
    SessionCompaction,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
)


class ModelProvider(Protocol):
    """Produces a normalized stream independently of any model vendor SDK."""

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
        *,
        context: ProviderRequestContext,
    ) -> AsyncIterator[ModelStreamEvent]:
        """Stream one assistant response for the supplied model-visible history."""
        ...


class RetryPolicy(Protocol):
    """Decides whether one failed provider attempt may be retried or routed."""

    def decide(self, failure: "AttemptFailure") -> "RetryDecision":
        """Return a deterministic action without sleeping or performing I/O."""
        ...


class BackoffStrategy(Protocol):
    """Calculates the delay before a retry of the same provider."""

    def delay_seconds(self, failure: "AttemptFailure") -> float:
        """Return a finite, non-negative delay."""
        ...


class RoutePolicy(Protocol):
    """Selects compatible provider candidates for one logical request."""

    def select(
        self,
        candidates: Sequence["ProviderCandidate"],
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        context: ProviderRequestContext,
    ) -> Sequence["ProviderCandidate"]:
        """Return candidates in the exact order they should be attempted."""
        ...


class EventSink(Protocol):
    """Accepts ordered observer events without changing runtime decisions."""

    def emit(self, event: AgentEvent) -> None:
        """Record or enqueue an event quickly; runtime isolates raised errors."""
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
        context: NextTurnContext,
    ) -> Sequence[Message]:
        """Return the canonical history for the next model request."""
        ...


class ProviderRequestHook(Protocol):
    """Transforms only the model-visible projection for one provider request."""

    async def prepare(
        self,
        context: ProviderRequestContext,
        plan: ContextPlan,
    ) -> ContextPlan:
        """Return a request projection without mutating canonical history."""
        ...


class TokenEstimator(Protocol):
    """Estimates model-context tokens for an ordered message sequence."""

    def estimate(self, messages: Sequence[Message]) -> int:
        """Return a deterministic, non-negative token estimate."""
        ...


class CompactionStrategy(Protocol):
    """Summarizes a validated prefix selected for durable compaction."""

    async def summarize(
        self,
        messages: Sequence[Message],
        *,
        context: ProviderRequestContext,
    ) -> str:
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
        *,
        event_context: SessionEventContext | None = None,
    ) -> SessionSnapshot:
        """Append messages if the persisted version still matches."""
        ...

    async def compact(
        self,
        session_id: str,
        expected_version: int,
        compaction: SessionCompaction,
        *,
        event_context: SessionEventContext | None = None,
    ) -> SessionSnapshot:
        """Atomically append a compaction event at the expected version."""
        ...

    async def record_control(
        self,
        session_id: str,
        expected_version: int,
        kind: SessionEventKind,
        control: ControlTransition,
        *,
        event_context: SessionEventContext | None = None,
    ) -> SessionSnapshot:
        """Atomically append one validated control-plane transition."""
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

    async def execute(
        self,
        arguments: JSONObject,
        context: ToolExecutionContext,
    ) -> ToolOutput:
        """Execute validated arguments with a frozen invocation snapshot."""
        ...


class ToolExecutionGate(Protocol):
    """Applies in-process policy before dispatching a validated tool call."""

    async def execute(
        self,
        tool: Tool,
        arguments: JSONObject,
        context: ToolExecutionContext,
    ) -> ToolOutput:
        """Invoke the tool or raise ToolExecutionGateError without claiming isolation."""
        ...


class ProcessSandbox(Protocol):
    """Runs commands behind an enforceable process and workspace boundary."""

    async def run(self, request: SandboxProcessRequest) -> SandboxProcessResult:
        """Execute one command or fail closed when isolation is unavailable."""
        ...


class ApprovalPolicy(Protocol):
    """Reviews tool calls that explicitly require approval."""

    async def review(
        self,
        context: ToolExecutionContext,
    ) -> ApprovalDecision:
        """Approve or deny one validated invocation using its full context."""
        ...
