"""Durable admission replaces local follow-up restoration; FIFO dispatcher is shared."""

import asyncio
from uuid import uuid4

from ..domain import (
    AgentResult,
    ControlTransition,
    EventProvenance,
    SessionEventContext,
    SessionEventKind,
    SessionSnapshot,
)
from ..errors import SessionControlError, SessionCommitUnknownError
from ..execution_domain import RunSubmission, CancelQueuedRequest
from ..storage.execution_codec import execution_operation
from .control import _SessionControl, _QueuedSessionRun, _consume_background_result
from .distributed_execution import _DistributedSessionExecution


class _DistributedSessionControl(_SessionControl):
    execution: _DistributedSessionExecution

    async def _enqueue_run(
        self, prompt: str, *, delivery: str, run_id: str | None = None
    ) -> _QueuedSessionRun:
        run_id = run_id or uuid4().hex
        config = self.execution.config
        settings = config.settings
        async with self._control_lock:
            record = await config.store.submit_run(
                RunSubmission(
                    self._session_id,
                    run_id,
                    prompt,
                    run_id,
                    settings.recovery_spec_ref,
                    settings.recovery_spec_hash,
                    delivery="follow_up" if delivery == "follow_up" else "prompt",
                    control_id=uuid4().hex if delivery == "follow_up" else None,
                    max_requests=self.execution.components.max_model_requests,
                    max_dispatches=settings.max_dispatches,
                    max_attempts=settings.max_attempts,
                )
            )
            return self._schedule(record.submission.run_id, record.submission.prompt)

    def _schedule(self, run_id: str, prompt: str) -> _QueuedSessionRun:
        if self._active_run is not None and self._active_run.run_id == run_id:
            return self._active_run
        for item in self._queued_runs:
            if item.run_id == run_id:
                return item
        queued = _QueuedSessionRun(
            run_id, prompt, "prompt", asyncio.get_running_loop().create_future()
        )
        queued.future.add_done_callback(_consume_background_result)
        self._queued_runs.append(queued)
        self._idle_event.clear()
        if self._dispatcher_task is None and self._active_compaction_task is None:
            self._start_dispatcher_locked()
        return queued

    async def resume(self, run_id: str) -> AgentResult:
        async with self._control_lock:
            record = await self.execution.config.store.load_execution(
                self._session_id, run_id
            )
            if record is None:
                raise SessionControlError("cannot resume an unknown durable Run")
            self.execution.validate(
                record.submission.recovery_spec_ref,
                record.submission.recovery_spec_hash,
            )
            if record.state in ("completed", "failed", "cancelled"):
                return await self.execution.result(self._session_id, run_id)
            queued = self._schedule(run_id, record.submission.prompt)
        try:
            return await asyncio.shield(queued.future)
        except asyncio.CancelledError:
            await self._cancel_submission(queued)
            raise

    async def restore_pending_controls(self, snapshot: SessionSnapshot) -> None:
        # Explicit resume uses Run journal. Never start the old follow-up recovery path.
        return

    async def _record_control(
        self,
        kind: SessionEventKind,
        control: ControlTransition,
        *,
        run_id: str,
        actor_id: str,
    ) -> SessionSnapshot:
        return await self.execution.active_writer().control(
            kind,
            control,
            SessionEventContext(
                run_id=run_id, provenance=EventProvenance("runtime", actor_id)
            ),
        )

    async def _cancel_durable_queued(self, queued: _QueuedSessionRun) -> None:
        store = self.execution.config.store
        record = await store.load_execution(self._session_id, queued.run_id)
        if record is not None and record.state == "queued":
            request = CancelQueuedRequest(
                self._session_id, queued.run_id, "user_cancelled", record.revision
            )
            operation = execution_operation(uuid4().hex, request)
            try:
                await store.cancel_queued(
                    self._session_id,
                    queued.run_id,
                    operation,
                    request.reason,
                    expected_revision=record.revision,
                )
            except SessionCommitUnknownError:
                if (
                    await store.resolve_operation(
                        self._session_id, operation.operation_id
                    )
                    is None
                ):
                    raise

    async def _cancel_submission(self, queued: _QueuedSessionRun) -> None:
        async with self._control_lock:
            if queued in self._queued_runs:
                await self._cancel_durable_queued(queued)
                self._queued_runs.remove(queued)
                self._cancel_queued_run(queued)
                return
            if (
                self._active_run is queued
                and not queued.future.done()
                and self._active_task is not None
                and not self._active_task.done()
            ):
                self._accepting_steer = False
                self._state = "aborting"
                self.execution.request_cancel()
        if self._active_task is not None:
            await asyncio.gather(
                asyncio.shield(self._active_task), return_exceptions=True
            )

    async def abort(self, *, clear_queue: bool = True) -> None:
        failures: list[Exception] = []
        async with self._control_lock:
            if clear_queue:
                for queued in tuple(self._queued_runs):
                    try:
                        await self._cancel_durable_queued(queued)
                    except Exception as error:
                        # Keep the durable Run recoverable, but stop local dispatch
                        # and report the failed cancellation to its own handle.
                        failures.append(error)
                        self._fail_queued_run(queued, error)
                    else:
                        self._cancel_queued_run(queued)
                    self._queued_runs.remove(queued)
            self._accepting_steer = False
            active = self._active_task
            if active is not None and not active.done():
                self._state = "aborting"
                self.execution.request_cancel()
            compact = self._active_compaction_task
            if compact is not None:
                self._state = "aborting"
                compact.cancel()
            if active is None and compact is None:
                self._state = "idle"
                self._idle_event.set()
        await asyncio.gather(
            *(asyncio.shield(t) for t in (active, compact) if t is not None),
            return_exceptions=True,
        )
        if failures:
            raise failures[0]
