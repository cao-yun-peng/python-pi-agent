"""Ports consumed by the core runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from .domain import (
    AgentEvent,
    ApprovalDecision,
    CompactionDecision,
    ControlTransition,
    ContextPlan,
    DurableSessionEvent,
    JSONObject,
    JSONValue,
    Message,
    ModelStreamEvent,
    NextTurnContext,
    ProviderRequestContext,
    ProviderRequestPurpose,
    RecordedModelRequest,
    SandboxProcessRequest,
    SandboxProcessResult,
    ShellExecRequest,
    ShellExecSpec,
    ShellRunResult,
    SessionEventContext,
    SessionEventKind,
    SessionSnapshot,
    SessionCompaction,
    TelemetryEvent,
    ToolCall as ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
    ToolResultContext,
    WorkspaceReadRequest,
    WorkspaceReadResult,
    WorkspaceWriteRequest, WorkspaceEditRequest, WorkspaceMutationResult,
    WorkspaceQueryRequest, WorkspaceQueryResult,
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


class ModelRequestStore(Protocol):
    """Required request persistence, separate from best-effort EventSink delivery."""

    async def record(self, request: RecordedModelRequest) -> None:
        """Commit before dispatch; reject an already recorded request/attempt id."""
        ...

    async def read_requests(
        self, session_id: str | None = None,
    ) -> tuple[RecordedModelRequest, ...]:
        """Read detached inputs offline; None selects all sessions and standalone runs."""
        ...


RetryAction = Literal["retry_same", "try_next", "fail"]


@dataclass(frozen=True, slots=True)
class ProviderCandidate:
    """One explicitly assembled raw provider plus routing metadata."""

    name: str
    provider: ModelProvider
    supports_tools: bool = True
    purposes: frozenset[ProviderRequestPurpose] = frozenset(
        {"agent_turn", "compaction"}
    )
    state_owners: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("provider candidate name must not be empty")
        if not self.purposes:
            raise ValueError("provider candidate purposes must not be empty")
        if not self.purposes <= {"agent_turn", "compaction"}:
            raise ValueError("provider candidate contains an unknown purpose")
        if any(not owner.strip() for owner in self.state_owners):
            raise ValueError("provider state owner names must not be empty")


@dataclass(frozen=True, slots=True)
class AttemptFailure:
    """Immutable evidence supplied to retry and backoff policies."""

    provider_name: str
    error: Exception
    retryable: bool
    output_started: bool
    provider_attempt: int
    total_attempt: int
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """Pure retry-policy result consumed by ResilientModelRuntime."""

    action: RetryAction
    reason: str

    def __post_init__(self) -> None:
        if self.action not in {"retry_same", "try_next", "fail"}:
            raise ValueError("unknown retry action")
        if not self.reason.strip():
            raise ValueError("retry decision reason must not be empty")


class RetryPolicy(Protocol):
    """Decides whether one failed provider attempt may be retried or routed."""

    def decide(self, failure: AttemptFailure) -> RetryDecision:
        """Return a deterministic action without sleeping or performing I/O."""
        ...


class BackoffStrategy(Protocol):
    """Calculates the delay before a retry of the same provider."""

    def delay_seconds(self, failure: AttemptFailure) -> float:
        """Return a finite, non-negative delay."""
        ...


class RoutePolicy(Protocol):
    """Selects compatible provider candidates for one logical request."""

    def select(
        self,
        candidates: Sequence[ProviderCandidate],
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        context: ProviderRequestContext,
    ) -> Sequence[ProviderCandidate]:
        """Return candidates in the exact order they should be attempted."""
        ...


class EventSink(Protocol):
    """Accepts ordered observer events without changing runtime decisions."""

    def emit(self, event: AgentEvent) -> None:
        """Record or enqueue an event quickly; runtime isolates raised errors."""
        ...


class TelemetrySink(Protocol):
    """Consume projected observations; perform slow I/O outside on_event."""

    def on_event(self, event: TelemetryEvent) -> None: ...

    async def aclose(self) -> None: ...


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


class ToolResultHook(Protocol):
    """Transform the executed result before its durable commit."""

    async def transform(self, context: ToolResultContext, output: ToolOutput) -> ToolOutput:
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
    """Loads and persists model-visible session history and durable controls.

    Implementations can raise domain errors such as SessionConflictError or
    SessionStorageError. Runtime callers wrap unexpected adapter exceptions in
    SessionStorageError and let cancellation propagate.
    """

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


class SchemaValidator(Protocol):
    """Validate arguments locally, before approval or any tool side effect."""

    def supports(self, schema: Mapping[str, JSONValue]) -> bool:
        """Return whether the complete schema fits this implementation's profile."""
        ...

    def validate(self, schema: Mapping[str, JSONValue], arguments: JSONObject) -> None:
        """Raise ToolValidationError for invalid arguments; configuration errors for invalid schemas."""
        ...


class Tool(Protocol):
    """A validated, cancellable capability callable by the model."""

    @property
    def definition(self) -> ToolDefinition:
        """Return a contract captured when an Agent is constructed; rebuild to update it."""
        ...

    async def execute(
        self,
        arguments: JSONObject,
        context: ToolExecutionContext,
    ) -> ToolOutput:
        """Execute using context.definition as the authoritative invocation contract."""
        ...


class ToolExecutionGate(Protocol):
    """Applies in-process policy before dispatching a validated tool call."""

    async def execute(
        self,
        tool: Tool,
        arguments: JSONObject,
        context: ToolExecutionContext,
    ) -> ToolOutput:
        """Apply policy to the bound Tool contract and invoke it; this is not OS isolation."""
        ...


class WorkspaceFiles(Protocol):
    """Borrowed workspace access; implementations enforce paths and read budgets.

    Reads raise WorkspaceFileError with a safe code, preserve cancellation, and
    acquire/release per-call resources. Construction performs no file reads.
    Future mutations will extend this boundary with their own contracts.
    """

    async def read_text(self, request: WorkspaceReadRequest) -> WorkspaceReadResult:
        """Read one bounded UTF-8 page, without following symbolic links."""
        ...


class WorkspaceMutations(Protocol):
    """Serialized file mutations; ambiguous side-effect outcomes must not be retried."""

    async def write_text(self, request: WorkspaceWriteRequest) -> WorkspaceMutationResult: ...

    async def edit_text(self, request: WorkspaceEditRequest) -> WorkspaceMutationResult: ...


class WorkspaceQueries(Protocol):
    """Bounded directory, glob-name and literal-text queries without following links."""

    async def query(self, request: WorkspaceQueryRequest) -> WorkspaceQueryResult: ...


class ShellExecutor(Protocol):
    """Resolves and runs one-shot Bash commands behind a replaceable seam."""

    def resolve(self, request: ShellExecRequest) -> ShellExecSpec:
        """Apply implementation-owned defaults and caps without performing I/O."""
        ...

    async def execute(self, spec: ShellExecSpec) -> ShellRunResult:
        """Execute a fully resolved spec or raise for infrastructure failure."""
        ...


class ProcessSandbox(Protocol):
    """Runs exact argv behind an enforceable process/workspace boundary."""

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
