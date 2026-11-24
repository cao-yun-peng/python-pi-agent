"""Ports consumed by the core runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from contextlib import AbstractAsyncContextManager
from typing import Literal, Protocol

from .execution_domain import (
    ExecutionGrant, ExecutionMutation, ExecutionReceipt, FinishRun,
    OperationIdentity, ProgressCommand, RunRecord, RunSubmission, SessionLease,
)

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


class ModelDispatchHook(Protocol):
    """Reserve a physical model dispatch before network I/O, including retries."""

    async def before_dispatch(self, context: ProviderRequestContext) -> None: ...


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


class DispatchBindableModelProvider(ModelProvider, Protocol):
    """Retrying/wrapping providers bind hooks per run without mutating themselves.

    Raw providers without this optional capability must perform one physical
    invocation per stream; SDK retries must be disabled at their boundary.
    """

    def bind_dispatch(self, hook: ModelDispatchHook) -> ModelProvider: ...


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


class SessionLeaseManager(Protocol):
    """Borrowed coordination client; None means occupied/lost, not I/O failure.

    Adapters preserve cancellation and classify infrastructure errors. The
    resource owner closes clients only after all users and heartbeats stop.
    Successful acquisition/renewal installs the requested TTL (floored to ms)
    from that operation, not merely a positive remainder of the old expiry.
    """

    async def acquire(
        self, session_id: str, owner_id: str, claim_id: str, ttl: float,
    ) -> SessionLease | None: ...

    async def renew(self, lease: SessionLease, ttl: float) -> SessionLease | None: ...

    async def release(self, lease: SessionLease) -> bool: ...


class LeaseClock(Protocol):
    """Inject both display time and monotonic deadlines, sleep and cancellation."""

    def unix_time(self) -> float: ...
    def monotonic(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...
    def timeout(self, seconds: float) -> AbstractAsyncContextManager[object]: ...


class ExecutionGrantManager(Protocol):
    """Narrow heartbeat dependency already implemented by execution stores.

    Renewal revalidates live authority and extends it by at least ttl from DB
    time, without reviving expired grants. Both calls cooperate with cancellation.
    """

    async def renew_grant(self, grant: ExecutionGrant, ttl: float) -> ExecutionGrant: ...
    async def release_grant(self, grant: ExecutionGrant) -> bool:
        """Compare-release even after expiry; preserve epoch and interrupted Run."""
        ...


class ExecutionWriter(Protocol):
    """Writer bound to one immutable grant; each commit revalidates PG authority."""

    async def commit_step(
        self, operation: OperationIdentity, command: ProgressCommand | None,
        mutations: tuple[ExecutionMutation, ...], *, expected_session_version: int,
        expected_revision: int,
    ) -> ExecutionReceipt:
        """Atomically persist session facts, checkpoint and deduplication receipt.

        Empty mutations are allowed for dispatch/boundary decisions without
        session writes. A missing command is allowed only for compaction or
        control enqueue/claim/cancellation that preserves Run progress. Multiple
        steering deliveries can share one ContinueRun transaction. Maintenance
        grants authorize compaction only, never control or message mutations.
        Adapters validate command/mutation/reference agreement,
        copy mutable message payloads and reject stale boundary observations.
        The operation digest covers the full command AND mutation request.
        """
        ...


class SessionExecutionStore(ExecutionGrantManager, Protocol):
    """Durable distributed execution boundary, independent of SessionStore.

    No method performs model/tool I/O. Implementations transact under a Session
    lock, use DB time and preserve cancellation. Admission authorization belongs
    to the application. Submission-key and operation-id collisions with different
    inputs are validation errors; lost authority is a conflict. Borrowed pools
    are not closed by this port. Explicit migration/startup precedes consumption.
    """

    async def submit_run(self, submission: RunSubmission) -> RunRecord:
        """Durably accept prompt or atomically enqueue the follow-up control.

        Enforce (session, submission_key) idempotence, assign queue order under
        the Session lock, and reject another submission reusing the run id.
        """
        ...

    async def list_candidates(self, limit: int) -> tuple[str, ...]:
        """Discover unique Session IDs, without granting execution ownership."""
        ...

    async def claim_next(
        self, lease: SessionLease, ttl: float, operation: OperationIdentity,
    ) -> ExecutionReceipt | None:
        """Recover active Run first, else FIFO; atomically advance epoch/attempt.

        None means no eligible work. Redis coordination must still be valid.
        An exhausted active Run is durably failed in this transaction instead of
        being endlessly rescanned; it must not dispatch another attempt.
        """
        ...

    async def claim_maintenance(
        self, lease: SessionLease, ttl: float, operation: OperationIdentity,
    ) -> ExecutionGrant:
        """Acquire compact-only permission; interrupted active Runs take priority."""
        ...

    async def claim_run(self, lease: SessionLease, run_id: str, ttl: float,
                        operation: OperationIdentity) -> ExecutionReceipt | None:
        """Claim only if this Run has FIFO/active priority; mismatch spends no attempt."""
        ...

    async def load_execution(self, session_id: str, run_id: str) -> RunRecord | None: ...

    async def cancel_queued(
        self, session_id: str, run_id: str, operation: OperationIdentity,
        reason: str, *, expected_revision: int,
    ) -> ExecutionReceipt:
        """Authorized admission cancellation without an execution grant.

        Serialize with claim_next; reject if already running. Cancel the linked
        pending follow-up control atomically, without cancelling another Run.
        """
        ...

    def bind_writer(self, grant: ExecutionGrant) -> ExecutionWriter:
        """Bind identity only; no connection acquisition or import side effects."""
        ...

    async def commit_step(
        self, grant: ExecutionGrant, operation: OperationIdentity,
        command: ProgressCommand | None, mutations: tuple[ExecutionMutation, ...], *,
        expected_session_version: int, expected_revision: int,
    ) -> ExecutionReceipt: ...

    async def finish_run(
        self, grant: ExecutionGrant, operation: OperationIdentity, finish: FinishRun,
        *, expected_session_version: int, expected_revision: int,
    ) -> ExecutionReceipt:
        """Atomically settle at a validated boundary and release active Run.

        Check accepted steering before completion. This must serialize with
        control admission; stale expected versions cannot close the window.
        """
        ...

    async def resolve_operation(
        self, session_id: str, operation_id: str,
    ) -> ExecutionReceipt | None:
        """Return committed receipt; None is not proof a concurrent commit failed."""
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
