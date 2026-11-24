"""Explicit FakeProvider benchmark using the production recovery Worker and loop."""

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from uuid import uuid4

from ..domain import (
    AgentEvent,
    JSONValue,
    JSONObject,
    Message,
    ModelStreamEvent,
    ProviderRequestContext,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
    Finish,
    TextDelta,
)
from ..errors import SessionStorageError
from ..execution_domain import (
    CommitAssistant,
    CommitToolResults,
    ExecutionGrant,
    ExecutionMutation,
    ExecutionReceipt,
    OperationIdentity,
    ProgressCommand,
    RunSubmission,
)
from ..lease_clock import SystemLeaseClock
from ..observability.events import CallbackEventSink
from ..ports import (
    ExecutionWriter,
    LeaseClock,
    SessionExecutionStore,
    SessionLeaseManager,
    SessionStore,
)
from ..runtime.execution_config import (
    DistributedExecution,
    DistributedExecutionSettings,
)
from ..runtime.recovery import RecoveryWorkerSettings, SessionRecoveryWorker, join_owned
from ..storage.execution import PostgresExecutionStore
from ..storage._postgres_driver import PostgresPool
from ..testing import FakeProvider, FakeStep
from ..tools.function import FunctionTool
from .benchmark_report import (
    BenchmarkSample,
    BenchmarkSettings,
    DATASET_VERSION,
    Scenario,
    summarize,
)
from .builder import AgentRuntimeBuilder
from .runtime import AgentRuntime
from .recovery import RegisteredRecoveryDispatcher

_REF = "benchmark:recovery-fake-v1"
_HASH = sha256(b"recovery-fake-v1:formal-loop:unknown-no-replay").hexdigest()


@dataclass(slots=True)
class _Observation:
    run_id: str
    scenario: Scenario
    accepted_at: float
    injected_at: float | None = None
    completed_at: float | None = None
    requests: int = 0
    tools: list[str] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)


class BenchmarkTrace:
    """Per invocation instrumentation; never used as execution authority."""

    def __init__(self, clock: LeaseClock | None = None) -> None:
        self.clock = clock or SystemLeaseClock()
        self.observations: dict[str, _Observation] = {}
        self.attempts: Counter[str] = Counter()
        self.events: list[dict[str, JSONValue]] = []
        self.done = asyncio.Event()

    def add(self, sid: str, rid: str, scenario: Scenario) -> None:
        self.observations[sid] = _Observation(rid, scenario, self.clock.monotonic())

    def inject(self, sid: str) -> None:
        observation = self.observations[sid]
        if observation.injected_at is None:
            observation.injected_at = self.clock.monotonic()
            self.events.append(
                {
                    "kind": "fault_injected",
                    "session_id": sid,
                    "run_id": observation.run_id,
                    "fault": observation.scenario,
                }
            )

    def event(self, owner: str, event: AgentEvent) -> None:
        if event.kind in ("recovery_scan_failed", "recovery_worker_failed"):
            error = event.payload.get("error_type")
            safe_error = error if isinstance(error, str) else "unknown"
            self.attempts[
                ("scan:" if event.kind == "recovery_scan_failed" else "fatal:")
                + safe_error
            ] += 1
            self.events.append(
                {"kind": event.kind, "owner": owner, "error_type": safe_error}
            )
            return
        sid = event.envelope.session_id
        if (
            sid is None
            or sid not in self.observations
            or not event.kind.startswith("recovery_")
        ):
            return
        observation = self.observations[sid]
        if event.kind == "recovery_started":
            self.attempts["claimed"] += 1
            observation.owners.append(owner)
        elif event.kind == "recovery_busy":
            self.attempts["busy"] += 1
        elif event.kind == "recovery_failed":
            error = event.payload.get("error_type")
            self.attempts[
                "retry:" + (error if isinstance(error, str) else "unknown")
            ] += 1
        elif event.kind == "recovery_cleanup_failed":
            self.attempts["cleanup_failed"] += 1
        elif event.kind == "recovery_completed":
            observation.completed_at = self.clock.monotonic()
            if all(
                item.completed_at is not None for item in self.observations.values()
            ):
                self.done.set()
        # Explicit allowlist, never serialize arbitrary event payloads.
        entry: dict[str, JSONValue] = {
            "kind": event.kind,
            "owner": owner,
            "session_id": sid,
            "run_id": event.envelope.run_id,
        }
        for key in ("epoch", "attempt", "error_type"):
            value = event.payload.get(key)
            if value is None or type(value) in (str, int):
                entry[key] = value if isinstance(value, (str, int)) else None
        self.events.append(entry)

    def bind_writer(
        self, grant: ExecutionGrant, writer: ExecutionWriter
    ) -> ExecutionWriter:
        return _FaultWriter(grant, writer, self)


@dataclass(frozen=True, slots=True)
class _FaultWriter:
    grant: ExecutionGrant
    writer: ExecutionWriter
    trace: BenchmarkTrace

    async def commit_step(
        self,
        operation: OperationIdentity,
        command: ProgressCommand | None,
        mutations: tuple[ExecutionMutation, ...],
        *,
        expected_session_version: int,
        expected_revision: int,
    ) -> ExecutionReceipt:
        receipt = await self.writer.commit_step(
            operation,
            command,
            mutations,
            expected_session_version=expected_session_version,
            expected_revision=expected_revision,
        )
        item = self.trace.observations.get(self.grant.session_id)
        if (
            item is not None
            and item.injected_at is None
            and (
                (
                    item.scenario == "final_assistant"
                    and isinstance(command, CommitAssistant)
                )
                or (
                    item.scenario == "tool_tail"
                    and isinstance(command, CommitToolResults)
                )
            )
        ):
            self.trace.inject(self.grant.session_id)
            raise SessionStorageError(
                "benchmark injected interruption after durable checkpoint"
            )
        return receipt


class BenchmarkExecutionStore(PostgresExecutionStore):
    """Only inject a lost process reply; storage decisions remain in the real adapter."""

    def __init__(self, pool: PostgresPool, trace: BenchmarkTrace) -> None:
        super().__init__(pool)
        self.trace = trace

    def bind_writer(self, grant: ExecutionGrant) -> ExecutionWriter:
        return self.trace.bind_writer(grant, super().bind_writer(grant))


@dataclass(frozen=True, slots=True)
class BenchmarkPeer:
    reader: SessionStore
    store: SessionExecutionStore
    leases: SessionLeaseManager


def _provider(trace: BenchmarkTrace, sid: str, attempt: int) -> FakeProvider:
    observation = trace.observations[sid]
    script: list[FakeStep]
    if attempt == 1 and observation.scenario == "partial_stream":
        script = [
            TextDelta("partial"),
            SessionStorageError("benchmark interrupted stream"),
        ]
    elif attempt == 1 and observation.scenario == "tool_tail":
        script = [
            ToolCall("done", "demo", {}),
            ToolCall("pending", "demo", {}),
            Finish("tool_calls"),
        ]
    else:
        script = [TextDelta("answer"), Finish()]

    class Provider(FakeProvider):
        async def stream(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolDefinition] = (),
            *,
            context: ProviderRequestContext,
        ) -> AsyncIterator[ModelStreamEvent]:
            observation.requests += 1
            async for event in super().stream(messages, tools, context=context):
                yield event

        # Mark the precise stream failure instant without changing the formal loop.

    if attempt == 1 and observation.scenario == "partial_stream":

        class PartialProvider(Provider):
            async def stream(
                self,
                messages: Sequence[Message],
                tools: Sequence[ToolDefinition] = (),
                *,
                context: ProviderRequestContext,
            ) -> AsyncIterator[ModelStreamEvent]:
                try:
                    async for event in super().stream(messages, tools, context=context):
                        yield event
                except SessionStorageError:
                    trace.inject(sid)
                    raise

        return PartialProvider([script])
    return Provider([script])


async def _sample(
    peer: BenchmarkPeer, trace: BenchmarkTrace, sid: str
) -> BenchmarkSample:
    observation = trace.observations[sid]
    record = await peer.store.load_execution(sid, observation.run_id)
    events = await peer.reader.read_events(sid)
    snapshot = await peer.reader.load(sid)
    prompt_count = sum(message.role == "user" for message in snapshot.messages)
    unknown = [
        message
        for message in snapshot.messages
        if message.role == "tool"
        and isinstance(message.structured_content, dict)
        and message.structured_content.get("outcome") == "unknown"
    ]
    expected = 1 if observation.scenario in ("text", "final_assistant") else 2
    is_fault = observation.scenario != "text"
    tool_valid = not observation.tools and not unknown
    if observation.scenario == "tool_tail":
        tool_valid = (
            observation.tools == ["done"]
            and len(unknown) == 1
            and (unknown[0].tool_call_id, unknown[0].tool_name, unknown[0].is_error)
            == ("pending", "demo", True)
            and any(
                event.message == unknown[0] and event.provenance.source == "recovery"
                for event in events
            )
        )
    correct = (
        record is not None
        and record.state == "completed"
        and prompt_count == 1
        and (
            record.attempt == (2 if is_fault else 1)
            and record.checkpoint.dispatches == expected
            and observation.requests == expected
            and tool_valid
            and (observation.injected_at is not None) == is_fault
            and not any(
                message.role == "assistant" and message.content == "partial"
                for message in snapshot.messages
            )
        )
    )
    end = observation.completed_at
    return BenchmarkSample(
        sid,
        observation.run_id,
        observation.scenario,
        record.state if record else "missing",
        record.error_code if record else "missing",
        record.attempt if record else 0,
        record.epoch if record else 0,
        record.checkpoint.dispatches if record else 0,
        observation.requests,
        len(observation.tools),
        prompt_count,
        len(unknown),
        len(events),
        correct,
        (end - observation.accepted_at) * 1000 if end is not None else None,
        (end - observation.injected_at) * 1000
        if end is not None and observation.injected_at is not None
        else None,
        tuple(observation.owners),
    )


async def run_batch(
    peers: tuple[BenchmarkPeer, ...],
    trace: BenchmarkTrace,
    settings: BenchmarkSettings,
    scenario: Scenario,
    count: int,
) -> dict[str, JSONValue]:
    """Admission first; throughput timer includes scheduling/recovery, excludes admission."""
    if len(peers) != settings.workers or count <= 0:
        raise ValueError(
            "batch requires the configured independent peers and a positive sample count"
        )
    trace.observations.clear()
    trace.attempts.clear()
    trace.events.clear()
    trace.done.clear()
    for _ in range(count):
        sid, rid = "bench-" + uuid4().hex, uuid4().hex
        trace.add(sid, rid, scenario)
        await peers[0].store.submit_run(
            RunSubmission(
                sid,
                rid,
                "hello",
                rid,
                _REF,
                _HASH,
                max_requests=4,
                max_dispatches=6,
                max_attempts=3,
            )
        )
    workers: list[SessionRecoveryWorker] = []
    start = trace.clock.monotonic()
    timed_out = False
    try:
        for index, peer in enumerate(peers):
            owner = "benchmark-worker-" + str(index)
            config = DistributedExecution(
                peer.store,
                peer.leases,
                DistributedExecutionSettings(
                    owner,
                    _REF,
                    _HASH,
                    max_dispatches=6,
                    max_attempts=3,
                    heartbeat=settings.heartbeat,
                ),
                trace.clock,
            )

            def observe(event: AgentEvent, owner: str = owner) -> None:
                trace.event(owner, event)

            sink = CallbackEventSink(observe)

            async def factory(
                receipt: ExecutionReceipt,
                peer: BenchmarkPeer = peer,
                config: DistributedExecution = config,
                sink: CallbackEventSink = sink,
            ) -> AgentRuntime:
                assert receipt.run is not None
                sid = receipt.run.submission.session_id

                async def execute(
                    arguments: JSONObject, context: ToolExecutionContext
                ) -> ToolOutput:
                    trace.observations[sid].tools.append(context.call.id)
                    return ToolOutput("done")

                return await (
                    AgentRuntimeBuilder()
                    .with_provider(_provider(trace, sid, receipt.run.attempt))
                    .with_tools(
                        (
                            FunctionTool(
                                ToolDefinition(
                                    "demo", "benchmark fixture", {"type": "object"}
                                ),
                                execute,
                            ),
                        )
                    )
                    .with_session_store(peer.reader)
                    .with_event_sink(sink)
                    .with_graceful_shutdown(0)
                    .with_distributed_execution(
                        peer.store,
                        peer.leases,
                        settings=config.settings,
                        clock=trace.clock,
                    )
                    .build()
                )

            dispatcher = RegisteredRecoveryDispatcher(
                peer.reader, config, {(_REF, _HASH): factory}, event_sink=sink
            )
            worker = SessionRecoveryWorker(
                config,
                dispatcher,
                event_sink=sink,
                settings=RecoveryWorkerSettings(
                    scan_limit=settings.scan_limit,
                    max_concurrent_sessions=settings.concurrency,
                    poll_interval=settings.poll_interval,
                    retry_delay=0.5,
                    max_retry_delay=0.5,
                ),
            )
            workers.append(worker)
            await worker.start()
        try:
            async with trace.clock.timeout(settings.timeout):
                await trace.done.wait()
        except TimeoutError:
            timed_out = True
    finally:
        elapsed = max(trace.clock.monotonic() - start, 1e-9)

        # Join every Worker before inspecting durable evidence or releasing clients.
        async def cleanup() -> None:
            for worker in reversed(workers):
                try:
                    await worker.aclose()
                except Exception as error:
                    trace.attempts["worker_close:" + type(error).__name__] += 1

        if await join_owned(
            asyncio.create_task(cleanup(), name="benchmark-workers-close")
        ):
            raise asyncio.CancelledError()
    samples = [await _sample(peers[0], trace, sid) for sid in trace.observations]
    result = summarize(scenario, samples, elapsed, trace.attempts, timed_out=timed_out)
    result["trace"] = list(trace.events)
    result["injected_faults"] = sum(
        item.injected_at is not None for item in trace.observations.values()
    )
    result["cleanup_success"] = not any(
        key == "cleanup_failed" or key.startswith(("worker_close:", "fatal:"))
        for key in trace.attempts
    )
    return result


async def run_benchmark(
    peers: tuple[BenchmarkPeer, ...],
    trace: BenchmarkTrace,
    settings: BenchmarkSettings,
    metadata: Mapping[str, JSONValue],
) -> dict[str, JSONValue]:
    batches: list[JSONValue] = []
    warmup: JSONValue = None
    if settings.warmup:
        warmup = await run_batch(peers, trace, settings, "text", settings.warmup)
        if (
            warmup["correct"] != settings.warmup
            or warmup["timed_out"]
            or not warmup["cleanup_success"]
        ):
            return {
                "schema_version": 1,
                "status": "invalid",
                "metadata": dict(metadata),
                "settings": settings.data(),
                "warmup": warmup,
                "batches": [],
            }
    for scenario in settings.scenarios:
        batch = await run_batch(peers, trace, settings, scenario, settings.samples)
        batches.append(batch)
        if (
            batch["correct"] != settings.samples
            or batch["timed_out"]
            or not batch["cleanup_success"]
        ):
            break  # Never mix an incomplete previous batch into a later measurement.
    valid = len(batches) == len(settings.scenarios) and all(
        isinstance(batch, dict)
        and batch["correct"] == settings.samples
        and not batch["timed_out"]
        and bool(batch["cleanup_success"])
        for batch in batches
    )
    return {
        "schema_version": 1,
        "status": "valid" if valid else "invalid",
        "dataset_version": DATASET_VERSION,
        "provider": "FakeProvider",
        "paid_model_calls": 0,
        "metadata": dict(metadata),
        "settings": settings.data(),
        "dataset": {
            "scenario_count": len(settings.scenarios),
            "measured_sessions_requested": settings.samples * len(settings.scenarios),
            "one_run_per_session": True,
            "prompt_utf8_bytes_per_run": 5,
            "registered_tool_count": 1,
            "session_schema_version": 6,
            "run_journal_version": 1,
            "sql_migrations": [1, 2],
        },
        "unexecuted_scenarios": list(settings.scenarios[len(batches) :]),
        "warmup": warmup,
        "batches": batches,
        "measurement": {
            "completion": "admission start through Worker observation of durable terminal state",
            "recovery": "fault injection through Worker observation of durable terminal state",
            "throughput": "completed Runs / batch worker-start-to-terminal-observation wall time; admission and warmup excluded",
            "fault_model": "cooperative checkpoint interruption with compare-release; no process kill or TTL wait",
            "conflict_rate": "busy events / (busy events + claimed events); includes Redis contention and PG rejection",
            "worker_model": "independent pools/clients, shared process/event loop, no shared execution-authority lock",
        },
    }
