"""Run-scoped history writer: actual facts and progress share a fenced commit."""

import asyncio
from typing import Literal, cast
from uuid import uuid4

from ..domain import (
    ControlClaim,
    ControlDelivery,
    ControlTransition,
    DurableEventIdentity,
    Finish,
    Message,
    ProviderRequestContext,
    SessionCompaction,
    SessionEventContext,
    SessionEventKind,
    SessionSnapshot,
    session_event_id,
)
from ..errors import (
    AgentLoopLimitError,
    SessionCommitUnknownError,
    SessionValidationError,
)
from ..execution_domain import (
    AppendExecutionMessages,
    CommitAssistant,
    CommitPrompt,
    CommitToolResults,
    CompactExecutionHistory,
    ContinueRun,
    DeliverExecutionControl,
    DispatchModel,
    ExecutionMutation,
    ExecutionReceipt,
    ExecutionStepRequest,
    FinishExecutionRequest,
    FinishRun,
    ProgressCommand,
    RecordExecutionControl,
    RunRecord,
)
from ..ports import SessionExecutionStore, SessionStore
from .lease_heartbeat import LeaseHeartbeat
from ..storage.execution_codec import execution_operation
from ..storage._writer import _latest_durable_identity


class _ExecutionSessionWriter:
    def __init__(
        self,
        store: SessionExecutionStore,
        reader: SessionStore,
        receipt: ExecutionReceipt,
        scope: LeaseHeartbeat,
        lock: asyncio.Lock,
    ) -> None:
        self.store, self.reader = store, reader
        self.receipt, self.scope, self.lock = receipt, scope, lock

    @property
    def snapshot(self) -> SessionSnapshot:
        return self.receipt.session

    @property
    def record(self) -> RunRecord:
        assert self.receipt.run is not None
        return self.receipt.run

    def ensure_active(self) -> None:
        self.scope.ensure_active()

    def identity(self, offset: int = 1) -> DurableEventIdentity:
        sid, seq = self.snapshot.session_id, self.snapshot.version + offset
        return DurableEventIdentity(sid, seq, session_event_id(sid, seq))

    async def refresh(self) -> None:
        snapshot = await self.reader.load(self.snapshot.session_id)
        record = (
            await self.store.load_execution(
                snapshot.session_id, self.record.submission.run_id
            )
            if self.receipt.run is not None
            else None
        )
        self.receipt = ExecutionReceipt(snapshot, record, self.scope.grant)

    async def step(
        self,
        command: ProgressCommand | None,
        mutations: tuple[ExecutionMutation, ...] = (),
    ) -> ExecutionReceipt:
        async with self.lock:
            await self.refresh()
            return await self.step_locked(command, mutations)

    async def step_locked(
        self,
        command: ProgressCommand | None,
        mutations: tuple[ExecutionMutation, ...] = (),
    ) -> ExecutionReceipt:
        self.scope.ensure_active()
        grant = self.scope.grant
        request = ExecutionStepRequest(
            grant,
            command,
            mutations,
            self.snapshot.version,
            self.record.revision if self.receipt.run is not None else 0,
        )
        operation = execution_operation(uuid4().hex, request)
        try:
            receipt = await self.store.bind_writer(grant).commit_step(
                operation,
                command,
                mutations,
                expected_session_version=request.expected_session_version,
                expected_revision=request.expected_revision,
            )
        except SessionCommitUnknownError:
            resolved = await self.store.resolve_operation(
                grant.session_id, operation.operation_id
            )
            if resolved is None:
                raise
            receipt = resolved
        self.receipt = receipt
        return receipt

    async def prompt(
        self,
        prompt: str,
        context: SessionEventContext,
        system_prompt: str | None = None,
    ) -> None:
        async with self.lock:
            await self.refresh()
            mutations: list[ExecutionMutation] = []
            if not self.snapshot.messages and system_prompt is not None:
                mutations.append(
                    AppendExecutionMessages(
                        (Message("system", system_prompt),), context
                    )
                )
            message = Message("user", prompt)
            control_id = self.record.submission.control_id
            if control_id is None:
                mutations.append(AppendExecutionMessages((message,), context))
            else:
                claim_id = await self.claim_locked(control_id, context)
                mutations.append(
                    DeliverExecutionControl(
                        ControlDelivery(control_id, claim_id, message), context
                    )
                )
            # claim_locked may advance the session version, but never adds visible messages.
            await self.step_locked(
                CommitPrompt(self.identity(len(mutations))), tuple(mutations)
            )

    async def assistant(
        self, message: Message, context: SessionEventContext, finish: Finish
    ) -> DurableEventIdentity:
        async with self.lock:
            await self.refresh()
            await self.step_locked(
                CommitAssistant(
                    self.identity(),
                    finish.reason,
                    tuple(c.id for c in message.tool_calls),
                ),
                (AppendExecutionMessages((message,), context),),
            )
            return _latest_durable_identity(self.snapshot)

    async def commit(
        self, message: Message, context: SessionEventContext
    ) -> DurableEventIdentity:
        await self.step(
            CommitToolResults((message.tool_call_id or "",)),
            (AppendExecutionMessages((message,), context),),
        )
        return _latest_durable_identity(self.snapshot)

    async def compact(
        self, compaction: SessionCompaction, context: SessionEventContext
    ) -> SessionSnapshot:
        await self.step(None, (CompactExecutionHistory(compaction, context),))
        return self.snapshot

    async def control(
        self,
        kind: SessionEventKind,
        control: ControlTransition,
        context: SessionEventContext,
    ) -> SessionSnapshot:
        if kind not in (
            "control_enqueued",
            "control_claimed",
            "control_cancelled",
        ) or isinstance(control, ControlDelivery):
            raise SessionValidationError("unsupported execution control transition")
        await self.step(
            None,
            (
                RecordExecutionControl(
                    cast(
                        Literal[
                            "control_enqueued", "control_claimed", "control_cancelled"
                        ],
                        kind,
                    ),
                    control,
                    context,
                ),
            ),
        )
        return self.snapshot

    async def claim_locked(self, control_id: str, context: SessionEventContext) -> str:
        grant = self.scope.grant
        claim_id = uuid4().hex
        await self.step_locked(
            None,
            (
                RecordExecutionControl(
                    "control_claimed",
                    ControlClaim(
                        control_id, claim_id, grant.owner_id, grant.expires_at
                    ),
                    context,
                ),
            ),
        )
        return claim_id

    async def boundary(self, context: SessionEventContext) -> bool:
        async with self.lock:
            await self.refresh()
            steering = [
                p.control
                for p in self.snapshot.pending_controls
                if p.control.delivery == "steer"
                and p.control.run_id == self.scope.grant.run_id
            ]
            cp = self.record.checkpoint
            if not cp.requires_continuation and not steering:
                return False
            if cp.request_number >= self.record.submission.max_requests:
                raise AgentLoopLimitError("durable logical request budget exhausted")
            deliveries: list[ExecutionMutation] = []
            for control in steering:
                claim = await self.claim_locked(control.control_id, context)
                deliveries.append(
                    DeliverExecutionControl(
                        ControlDelivery(
                            control.control_id, claim, Message("user", control.prompt)
                        ),
                        context,
                    )
                )
            refs = tuple(self.identity(i + 1) for i in range(len(deliveries)))
            await self.step_locked(ContinueRun(refs), tuple(deliveries))
            return True

    async def before_dispatch(self, context: ProviderRequestContext) -> None:
        self.scope.ensure_active()
        if context.purpose != "agent_turn":
            return
        if (context.session_id, context.run_id) != (
            self.scope.grant.session_id,
            self.scope.grant.run_id,
        ):
            raise SessionValidationError("model request differs from granted execution")
        async with self.lock:
            await self.refresh()
            if (
                self.record.checkpoint.dispatches
                >= self.record.submission.max_dispatches
            ):
                raise AgentLoopLimitError("durable physical dispatch budget exhausted")
            if context.request_number != self.record.checkpoint.request_number:
                raise AgentLoopLimitError(
                    "model request number differs from checkpoint"
                )
            await self.step_locked(DispatchModel())
        self.scope.ensure_active()

    async def finish(self, finish: FinishRun) -> None:
        async with self.lock:
            self.scope.ensure_active()
            await self.refresh()
            request = FinishExecutionRequest(
                self.scope.grant, finish, self.snapshot.version, self.record.revision
            )
            operation = execution_operation(uuid4().hex, request)
            try:
                receipt = await self.store.finish_run(
                    request.grant,
                    operation,
                    finish,
                    expected_session_version=request.expected_session_version,
                    expected_revision=request.expected_revision,
                )
            except SessionCommitUnknownError:
                resolved = await self.store.resolve_operation(
                    request.grant.session_id, operation.operation_id
                )
                if resolved is None:
                    raise
                receipt = resolved
            self.receipt = receipt
