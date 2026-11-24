"""Explicit Run claim/resume and settlement above the single shared Agent loop."""

import asyncio
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from ..domain import (
    AgentResult,
    EventProvenance,
    Finish,
    Message,
    SessionEventContext,
    SessionSnapshot,
    ControlDelivery,
)
from ..errors import (
    AgentError,
    ConfigurationError,
    SessionCommitUnknownError,
    SessionConflictError,
    SessionControlError,
    SessionStorageError,
)
from ..execution_domain import (
    ClaimExecutionRequest,
    ClaimRunExecutionRequest,
    CommitToolResults,
    ExecutionReceipt,
    FinishRun,
    RepairToolTail,
    AppendExecutionMessages,
)
from ..observability._session_events import (
    _SessionEvents,
    _turn_id,
    _run_error_fields,
    _message_payload,
)
from ..ports import SessionStore
from ..providers.dispatch import bind_model_dispatch
from ..storage.execution_codec import execution_operation
from ..storage.transitions import find_unresolved_tool_calls, replay_session
from .components import AgentComponents
from .compaction_orchestration import _SessionCompactor
from .execution_config import DistributedExecution
from .execution_writer import _ExecutionSessionWriter
from .lease_heartbeat import LeaseHeartbeat
from .loop import Agent, _AgentWiring, _LoopResume, _ModelTurn, _BoundaryDecision
from .tool_recovery import interrupted_tool_result

if TYPE_CHECKING:
    from .distributed_control import _DistributedSessionControl
    from ..plugins import PluginSessionLease


@dataclass(frozen=True, slots=True)
class _Outcome:
    result: AgentResult | None = None
    error: BaseException | None = None


class _DistributedSessionExecution:
    def __init__(
        self,
        config: DistributedExecution,
        reader: SessionStore,
        components: AgentComponents,
        compactor: _SessionCompactor,
        events: _SessionEvents,
        lock: asyncio.Lock,
    ) -> None:
        self.config, self.reader, self.components = config, reader, components
        self.compactor, self.events, self.lock = compactor, events, lock
        self.writer: _ExecutionSessionWriter | None = None
        self.work_task: asyncio.Task[object] | None = None
        self.cancel_requested = False

    def request_cancel(self) -> None:
        self.cancel_requested = True
        if self.work_task is not None:
            self.work_task.cancel()

    def active_writer(self) -> _ExecutionSessionWriter:
        if self.writer is None:
            raise SessionControlError("execution has not obtained a live grant")
        self.writer.scope.ensure_active()
        return self.writer

    async def _emit_messages_since(
        self, writer: _ExecutionSessionWriter, version: int
    ) -> None:
        for event in await self.reader.read_events(writer.snapshot.session_id):
            if event.sequence <= version or event.run_id != writer.scope.grant.run_id:
                continue
            message = (
                event.control.message
                if isinstance(event.control, ControlDelivery)
                else event.message
            )
            if message is not None:
                self.events.emit(
                    "message_committed",
                    channel="durable",
                    run_id=event.run_id,
                    turn_id=event.turn_id,
                    durable_event_id=event.event_id,
                    provenance_source=event.provenance.source,
                    **_message_payload(message),
                )
                if event.provenance.source == "recovery" and message.role == "tool":
                    self.events.emit(
                        "tool_call_recovered",
                        run_id=event.run_id,
                        durable_event_id=event.event_id,
                        tool_call_id=message.tool_call_id,
                        tool_name=message.tool_name,
                        outcome="unknown",
                    )

    async def result(self, session_id: str, run_id: str) -> AgentResult:
        record = await self.config.store.load_execution(session_id, run_id)
        if record is None or record.state not in ("completed", "failed", "cancelled"):
            raise SessionControlError("durable Run is not terminal")
        if record.state == "cancelled":
            raise asyncio.CancelledError()
        if record.state == "failed":
            raise SessionControlError(
                "durable Run failed: " + (record.error_code or "unknown")
            )
        reference = record.checkpoint.assistant_event
        events = await self.reader.read_events(session_id)
        for event in events:
            if (
                reference is not None
                and event.event_id == reference.event_id
                and event.message is not None
            ):
                snapshot = replay_session(
                    session_id, tuple(e for e in events if e.sequence <= event.sequence)
                )
                return AgentResult(
                    event.message,
                    snapshot.messages,
                    record.checkpoint.finish_reason or "stop",
                )
        raise SessionStorageError("terminal assistant reference cannot be resolved")

    def validate(self, record_ref: str, record_hash: str) -> None:
        settings = self.config.settings
        if (record_ref, record_hash) != (
            settings.recovery_spec_ref,
            settings.recovery_spec_hash,
        ):
            raise ConfigurationError("durable Run recovery configuration differs")

    async def execute(
        self,
        session_id: str,
        run_id: str,
        control: "_DistributedSessionControl",
        plugin: "PluginSessionLease | None",
    ) -> AgentResult:
        config = self.config
        try:
            record = await config.store.load_execution(session_id, run_id)
            if record is None:
                raise SessionControlError("Run has not been durably accepted")
            self.validate(
                record.submission.recovery_spec_ref,
                record.submission.recovery_spec_hash,
            )
            if record.state in ("completed", "failed", "cancelled"):
                result = await self.result(session_id, run_id)
            else:
                result = await self._claim_and_run(session_id, run_id, control, plugin)
        except asyncio.CancelledError:
            self.events.emit("run_cancelled", run_id=run_id, channel="live")
            raise
        except AgentError as error:
            self.events.emit(
                "run_failed", run_id=run_id, channel="live", **_run_error_fields(error)
            )
            raise
        else:
            finished = self.writer.record if self.writer is not None else record
            self.events.emit(
                "run_completed",
                run_id=run_id,
                channel="live",
                finish_reason=result.finish_reason,
                model_requests=finished.checkpoint.request_number,
                dispatches=finished.checkpoint.dispatches,
                attempt=finished.attempt,
                usage=asdict(result.usage) if result.usage is not None else None,
                usage_scope="current_attempt",
            )
            return result
        finally:
            self.writer, self.work_task = None, None
            self.cancel_requested = False
            control._steering.clear()  # Durable pending controls remain authoritative after loss.
            self.events.emit("run_settled", run_id=run_id, channel="live")

    async def _claim_and_run(
        self,
        session_id: str,
        run_id: str,
        control: "_DistributedSessionControl",
        plugin: "PluginSessionLease | None",
    ) -> AgentResult:
        config, settings = self.config, self.config.settings
        ttl = settings.heartbeat.ttl_seconds
        lease = await config.leases.acquire(
            session_id, settings.owner_id, uuid4().hex, ttl
        )
        if lease is None:
            raise SessionConflictError(
                "coordination lease is occupied; Run remains durable"
            )
        scope = None
        grant = None
        try:
            claim = execution_operation(
                uuid4().hex, ClaimRunExecutionRequest(lease, ttl, run_id)
            )
            try:
                receipt = await config.store.claim_run(lease, run_id, ttl, claim)
            except SessionCommitUnknownError:
                receipt = await config.store.resolve_operation(
                    session_id, claim.operation_id
                )
                if receipt is None:
                    raise
            if receipt is None or receipt.run is None:
                raise SessionControlError("no runnable durable Run")
            grant = receipt.grant
            if receipt.run.submission.run_id != run_id:
                raise SessionConflictError(
                    "an earlier durable Run must be resumed first"
                )
            if grant is None:
                return await self.result(session_id, run_id)
            self.validate(
                receipt.run.submission.recovery_spec_ref,
                receipt.run.submission.recovery_spec_hash,
            )
            scope = LeaseHeartbeat(
                config.leases,
                config.store,
                lease,
                grant,
                settings=settings.heartbeat,
                clock=config.clock,
                event_sink=self.events.sink,
            )
            writer = _ExecutionSessionWriter(
                config.store, self.reader, receipt, scope, self.lock
            )
            self.writer = writer

            async def work(guard: LeaseHeartbeat) -> _Outcome:
                from typing import cast

                self.work_task = cast(asyncio.Task[object], asyncio.current_task())
                try:
                    if self.cancel_requested:
                        raise asyncio.CancelledError()
                    return _Outcome(
                        result=await self._resume_claimed(writer, control, plugin)
                    )
                except asyncio.CancelledError as error:
                    if not self.cancel_requested or guard.lost_reason is not None:
                        raise
                    return _Outcome(error=error)
                except (SessionStorageError, SessionConflictError):
                    raise  # Unknown storage outcome or lost authority is recoverable, not a new terminal fact.
                except AgentError as error:
                    return _Outcome(error=error)
                finally:
                    self.work_task = None

            async def finalize(outcome: _Outcome, unused_grant: object) -> None:
                async with control._control_lock:
                    control._accepting_steer = False
                    if isinstance(outcome.error, asyncio.CancelledError):
                        finish = FinishRun("cancelled", "user_cancelled")
                    elif outcome.error is not None:
                        finish = FinishRun("failed", type(outcome.error).__name__)
                    else:
                        finish = FinishRun("completed")
                    await writer.finish(finish)

            outcome = await scope.run(work, finalize=finalize)
            if outcome.error is not None:
                raise outcome.error
            assert outcome.result is not None
            return outcome.result
        finally:
            if scope is not None:
                await scope.aclose()
            else:
                try:
                    if grant is not None:
                        await config.store.release_grant(grant)
                finally:
                    await config.leases.release(lease)

    async def _resume_claimed(
        self,
        writer: _ExecutionSessionWriter,
        control: "_DistributedSessionControl",
        plugin: "PluginSessionLease | None",
    ) -> AgentResult:
        record = writer.record
        run_id, session_id = record.submission.run_id, record.submission.session_id
        context = SessionEventContext(
            run_id=run_id,
            turn_id=_turn_id(run_id, record.checkpoint.request_number),
            provenance=EventProvenance("recovery", "tool-call-recovery"),
        )
        unresolved = find_unresolved_tool_calls(writer.snapshot.messages)
        if unresolved:
            before = writer.snapshot.version
            command = (
                RepairToolTail(tuple(c.id for c in unresolved))
                if record.checkpoint.phase == "before_prompt"
                else CommitToolResults(tuple(c.id for c in unresolved))
            )
            await writer.step(
                command,
                (
                    AppendExecutionMessages(
                        tuple(interrupted_tool_result(c) for c in unresolved), context
                    ),
                ),
            )
            await self._emit_messages_since(writer, before)
        if writer.record.checkpoint.phase == "before_prompt":
            await self.compactor.maybe_auto_compact(
                writer,
                pending_messages=(Message("user", record.submission.prompt),),
                event_context=SessionEventContext(
                    run_id=run_id,
                    provenance=EventProvenance("compaction", "automatic-compaction"),
                ),
            )
            before = writer.snapshot.version
            await writer.prompt(
                record.submission.prompt,
                SessionEventContext(
                    run_id=run_id,
                    turn_id=_turn_id(run_id, 1),
                    provenance=EventProvenance("user", "user"),
                ),
                self.components.resolve_system_prompt(),
            )
            await self._emit_messages_since(writer, before)
        cp = writer.record.checkpoint
        turn = None
        if cp.phase == "awaiting_boundary":
            for event in await self.reader.read_events(session_id):
                if (
                    cp.assistant_event is not None
                    and event.event_id == cp.assistant_event.event_id
                    and event.message is not None
                ):
                    turn = _ModelTurn(
                        event.message, Finish(cp.finish_reason or "stop"), None
                    )
                    break
            if turn is None:
                raise SessionStorageError(
                    "checkpoint assistant reference cannot be resolved"
                )

        async def boundary() -> _BoundaryDecision:
            async with control._control_lock:
                number = writer.record.checkpoint.request_number + 1
                before = writer.snapshot.version
                more = await writer.boundary(
                    SessionEventContext(
                        run_id=run_id,
                        turn_id=_turn_id(run_id, number),
                        provenance=EventProvenance("user", "steer"),
                    )
                )
                await self._emit_messages_since(writer, before)
                control._steering.clear()
                if not more:
                    control._accepting_steer = False
                return _BoundaryDecision(more, writer.snapshot.messages)

        components = self.components.replace(
            provider=bind_model_dispatch(self.components.provider, writer),
            max_model_requests=record.submission.max_requests,
        )
        agent = Agent._for_session(
            components,
            event_sink=self.events.sink,
            initial_messages=writer.snapshot.messages,
            wiring=_AgentWiring(
                message_committer=writer.commit,
                assistant_committer=writer.assistant,
                boundary=boundary,
                dispatch_gate=writer.scope.ensure_active,
                provider_request_hook=plugin,
                dispatch_started=True,
                external_settlement=True,
            ),
            next_turn_hook=self.compactor.next_turn_hook(
                writer, components.next_turn_hook
            ),
            session_id=session_id,
        )
        result = await agent.run(
            record.submission.prompt,
            run_id=run_id,
            prompt_already_committed=True,
            _resume=_LoopResume(
                cp.request_number,
                turn,
                cp.phase == "awaiting_model" and cp.request_number > 1,
            ),
        )
        return AgentResult(
            result.message, writer.snapshot.messages, result.finish_reason, result.usage
        )

    async def compact(self, session_id: str, keep_recent_turns: int) -> SessionSnapshot:
        config, settings = self.config, self.config.settings
        ttl = settings.heartbeat.ttl_seconds
        lease = await config.leases.acquire(
            session_id, settings.owner_id, uuid4().hex, ttl
        )
        if lease is None:
            raise SessionConflictError("maintenance coordination lease is occupied")
        scope, grant = None, None
        try:
            # Maintenance uses the same claim request envelope and a distinct method digest.
            request = ClaimExecutionRequest(lease, ttl, "maintenance")
            operation = execution_operation(uuid4().hex, request)
            try:
                grant = await config.store.claim_maintenance(lease, ttl, operation)
            except SessionCommitUnknownError:
                confirmed = await config.store.resolve_operation(
                    session_id, operation.operation_id
                )
                if confirmed is None or confirmed.grant is None:
                    raise
                grant = confirmed.grant
            scope = LeaseHeartbeat(
                config.leases,
                config.store,
                lease,
                grant,
                settings=settings.heartbeat,
                clock=config.clock,
                event_sink=self.events.sink,
            )
            writer = _ExecutionSessionWriter(
                config.store,
                self.reader,
                ExecutionReceipt(await self.reader.load(session_id), None, grant),
                scope,
                self.lock,
            )

            async def work(guard: LeaseHeartbeat) -> SessionSnapshot:
                guard.ensure_active()

                async def load() -> _ExecutionSessionWriter:
                    return writer

                return await self.compactor.compact(
                    load, keep_recent_turns=keep_recent_turns
                )

            return await scope.run(work)
        finally:
            if scope is not None:
                await scope.aclose()
            else:
                try:
                    if grant is not None:
                        await config.store.release_grant(grant)
                finally:
                    await config.leases.release(lease)
