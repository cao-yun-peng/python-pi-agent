"""FIFO run dispatch, durable controls, and Session cancellation."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from uuid import uuid4

from ..domain import (
    AgentResult,
    ControlCancellation,
    ControlClaim,
    ControlTransition,
    EventProvenance,
    QueuedControl,
    SessionEventContext,
    SessionEventKind,
    SessionSnapshot,
    SessionState,
)
from ..errors import AgentError, InternalError, SessionControlError
from ..ports import SessionStore
from ._observation import _SessionEvents, _run_error_fields
from ._persistence import _call_session_store
from .loop import _SteeringInstruction


@dataclass(slots=True)
class _QueuedSessionRun:
    run_id: str
    prompt: str
    delivery: str
    future: asyncio.Future[AgentResult]
    control_id: str | None = None
    claim_id: str | None = None
    dispatch_started: bool = False
    entered_agent_loop: bool = False


class _SessionControl:
    """Own the control lock, queue, active work, and idle barrier for one Session."""

    def __init__(
        self,
        *,
        session_id: str,
        session_store: SessionStore,
        durable_lock: asyncio.Lock,
        clock: Callable[[], float],
        control_claim_lease_seconds: float,
        run: Callable[[_QueuedSessionRun], Coroutine[object, object, AgentResult]],
        notify_run_settled: Callable[[str], None],
        events: _SessionEvents,
    ) -> None:
        self._session_id = session_id
        self._session_store = session_store
        self._durable_lock = durable_lock
        self._clock = clock
        self._control_claim_lease_seconds = control_claim_lease_seconds
        self._run = run
        self._notify_run_settled = notify_run_settled
        self._events = events
        self._owner_id = uuid4().hex
        self._control_lock = asyncio.Lock()
        self._queued_runs: deque[_QueuedSessionRun] = deque()
        self._steering: deque[QueuedControl] = deque()
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._active_task: asyncio.Task[AgentResult] | None = None
        self._active_compaction_task: asyncio.Task[SessionSnapshot] | None = None
        self._active_run: _QueuedSessionRun | None = None
        self._state: SessionState = "idle"
        self._accepting_steer = False
        self._idle_event = asyncio.Event()
        self._idle_event.set()
        self.observe_dispatch_start = False

    @property
    def state(self) -> SessionState:
        """Return the current explicit control-plane state."""
        return self._state

    @property
    def is_idle(self) -> bool:
        """Return whether no run is active and no follow-up is queued."""
        return self._state == "idle"

    @property
    def active_run_id(self) -> str | None:
        active = self._active_run
        return active.run_id if active is not None else None

    @property
    def pending_follow_up_count(self) -> int:
        return len(self._queued_runs)

    @property
    def pending_steer_count(self) -> int:
        return len(self._steering)

    async def run(self, prompt: str) -> AgentResult:
        """Queue one run and await its own settled result."""
        queued = await self._enqueue_run(prompt, delivery="prompt")
        try:
            return await asyncio.shield(queued.future)
        except asyncio.CancelledError:
            await self._cancel_submission(queued)
            raise

    async def follow_up(self, prompt: str) -> str:
        """Queue a new run after earlier work and return its run identity."""
        queued = await self.submit(prompt, delivery="follow_up")
        return queued.run_id

    async def submit(
        self, prompt: str, *, delivery: str, run_id: str | None = None
    ) -> _QueuedSessionRun:
        queued = await self._enqueue_run(prompt, delivery=delivery, run_id=run_id)
        queued.future.add_done_callback(_consume_background_result)
        return queued

    async def steer(self, prompt: str) -> None:
        """Insert a user instruction at the active run's next safe boundary."""
        async with self._control_lock:
            if self._active_run is None or not self._accepting_steer:
                raise SessionControlError(
                    "steer requires an active run that still accepts input"
                )
            active = self._active_run
            control = QueuedControl(
                control_id=uuid4().hex,
                delivery="steer",
                prompt=prompt,
                run_id=active.run_id,
                target_run_id=active.run_id,
                cause_run_id=active.run_id,
            )
            await self._record_control(
                "control_enqueued",
                control,
                run_id=active.run_id,
                actor_id="steer",
            )
            self._steering.append(control)

    async def abort(self, *, clear_queue: bool = True) -> None:
        """Cancel the active run and, by default, all queued follow-ups."""
        active_tasks: tuple[asyncio.Task[object], ...]
        async with self._control_lock:
            if clear_queue:
                while self._queued_runs:
                    queued = self._queued_runs[0]
                    if queued.control_id is not None:
                        await self._cancel_control(
                            queued.control_id,
                            run_id=queued.run_id,
                            reason="abort",
                        )
                    self._queued_runs.popleft()
                    self._cancel_queued_run(queued)
            while self._steering:
                control = self._steering.popleft()
                await self._cancel_control(
                    control.control_id,
                    run_id=control.run_id,
                    reason="abort",
                )
            self._accepting_steer = False
            active_tasks = tuple(
                task
                for task in (self._active_task, self._active_compaction_task)
                if task is not None and not task.done()
            )
            if active_tasks:
                self._state = "aborting"
                for task in active_tasks:
                    task.cancel()
            elif not self._queued_runs:
                self._state = "idle"
                self._idle_event.set()

        for active_task in active_tasks:
            try:
                await asyncio.shield(active_task)
            except asyncio.CancelledError:
                pass

    async def wait_until_idle(self) -> None:
        await self._idle_event.wait()

    async def restore_pending_controls(self, snapshot: SessionSnapshot) -> None:
        loop = asyncio.get_running_loop()
        try:
            for pending in snapshot.pending_controls:
                control = pending.control
                if control.delivery == "steer":
                    await self._cancel_control(
                        control.control_id,
                        run_id=control.run_id,
                        reason="target_run_not_active_after_restore",
                    )
                    continue
                if (
                    pending.claim is not None
                    and pending.claim.lease_expires_at > self._clock()
                ):
                    continue
                queued = _QueuedSessionRun(
                    run_id=control.run_id,
                    prompt=control.prompt,
                    delivery="follow_up",
                    future=loop.create_future(),
                    control_id=control.control_id,
                )
                queued.future.add_done_callback(_consume_background_result)
                self._queued_runs.append(queued)
        except BaseException:
            self._queued_runs.clear()
            raise

    def resume_pending_runs(self) -> None:
        if self._queued_runs:
            self._idle_event.clear()
            self._start_dispatcher_locked()

    async def _enqueue_run(
        self,
        prompt: str,
        *,
        delivery: str,
        run_id: str | None = None,
    ) -> _QueuedSessionRun:
        loop = asyncio.get_running_loop()
        queued = _QueuedSessionRun(
            run_id=run_id or uuid4().hex,
            prompt=prompt,
            delivery=delivery,
            future=loop.create_future(),
        )
        async with self._control_lock:
            if delivery == "follow_up":
                control = QueuedControl(
                    control_id=uuid4().hex,
                    delivery="follow_up",
                    prompt=prompt,
                    run_id=queued.run_id,
                    cause_run_id=(
                        self._active_run.run_id
                        if self._active_run is not None
                        else None
                    ),
                )
                await self._record_control(
                    "control_enqueued",
                    control,
                    run_id=queued.run_id,
                    actor_id="follow-up",
                )
                queued.control_id = control.control_id
            self._queued_runs.append(queued)
            self._idle_event.clear()
            if (
                self._dispatcher_task is None
                and self._active_compaction_task is None
            ):
                self._start_dispatcher_locked()
        return queued

    async def _dispatch_runs(self) -> None:
        while True:
            async with self._control_lock:
                if not self._queued_runs:
                    self._active_run = None
                    self._active_task = None
                    self._accepting_steer = False
                    self._state = "idle"
                    self._idle_event.set()
                    self._dispatcher_task = None
                    return

                queued = self._queued_runs.popleft()
                if self.observe_dispatch_start:
                    queued.dispatch_started = True
                    self._events.emit("run_started", run_id=queued.run_id, channel="live")
                if queued.control_id is not None:
                    try:
                        queued.claim_id = await self._claim_control(
                            queued.control_id,
                            run_id=queued.run_id,
                        )
                    except Exception as error:
                        self._fail_queued_run(queued, error)
                        continue
                self._active_run = queued
                self._accepting_steer = True
                self._state = "running"
                self._active_task = asyncio.create_task(
                    self._run(queued),
                    name=f"agent-run:{queued.run_id}",
                )
                active_task = self._active_task

            try:
                result = await active_task
            except asyncio.CancelledError:
                if not queued.entered_agent_loop:
                    self._events.emit("run_cancelled", run_id=queued.run_id, channel="live")
                    self._events.emit("run_settled", run_id=queued.run_id, channel="live")
                if not queued.future.done():
                    queued.future.cancel()
            except Exception as error:
                self._fail_queued_run(queued, error)
            except BaseException as error:
                if not queued.future.done():
                    queued.future.set_exception(error)
            else:
                if not queued.future.done():
                    queued.future.set_result(result)
            finally:
                self._notify_run_settled(queued.run_id)
                async with self._control_lock:
                    if self._active_run is queued:
                        while self._steering:
                            control = self._steering.popleft()
                            try:
                                await self._cancel_control(
                                    control.control_id,
                                    run_id=control.run_id,
                                    reason="target_run_settled",
                                )
                            except AgentError as error:
                                self._events.emit(
                                    "session_control_cleanup_failed",
                                    run_id=control.run_id,
                                    **_run_error_fields(error),
                                )
                        self._active_run = None
                        self._active_task = None
                        self._accepting_steer = False
                        self._steering.clear()
                        self._state = "settled"
                await asyncio.sleep(0)

    def _fail_queued_run(self, queued: _QueuedSessionRun, error: Exception) -> None:
        if isinstance(error, AgentError):
            failure = error
        else:
            failure = InternalError("session run failed unexpectedly")
            failure.__cause__ = error
        if not queued.entered_agent_loop:
            self._events.emit(
                "run_failed",
                run_id=queued.run_id,
                channel="live",
                **_run_error_fields(failure),
            )
            self._events.emit("run_settled", run_id=queued.run_id, channel="live")
        if not queued.future.done():
            queued.future.set_exception(failure)

    def _start_dispatcher_locked(self) -> None:
        self._dispatcher_task = asyncio.create_task(
            self._dispatch_runs(),
            name=f"agent-session:{self._session_id}",
        )

    def _cancel_queued_run(self, queued: _QueuedSessionRun) -> None:
        self._events.emit("run_cancelled", run_id=queued.run_id, channel="live")
        self._events.emit("run_settled", run_id=queued.run_id, channel="live")
        queued.future.cancel()

    async def _cancel_submission(self, queued: _QueuedSessionRun) -> None:
        active_task: asyncio.Task[AgentResult] | None = None
        async with self._control_lock:
            if queued in self._queued_runs:
                if queued.control_id is not None:
                    await self._cancel_control(
                        queued.control_id,
                        run_id=queued.run_id,
                        reason="submission_cancelled",
                    )
                self._queued_runs.remove(queued)
                self._cancel_queued_run(queued)
                if (
                    self._active_run is None
                    and self._active_compaction_task is None
                    and not self._queued_runs
                ):
                    self._state = "idle"
                    self._idle_event.set()
                return
            if self._active_run is queued and not queued.future.done():
                while self._steering:
                    control = self._steering.popleft()
                    await self._cancel_control(
                        control.control_id,
                        run_id=control.run_id,
                        reason="active_submission_cancelled",
                    )
                self._accepting_steer = False
                self._state = "aborting"
                active_task = self._active_task
                if active_task is not None and not active_task.done():
                    active_task.cancel()

        if active_task is not None:
            try:
                await asyncio.shield(active_task)
            except asyncio.CancelledError:
                pass

    async def receive_steering(
        self,
        close_if_empty: bool,
    ) -> tuple[_SteeringInstruction, ...]:
        async with self._control_lock:
            claimed: list[_SteeringInstruction] = []
            for control in self._steering:
                claim_id = await self._claim_control(
                    control.control_id,
                    run_id=control.run_id,
                )
                claimed.append(
                    _SteeringInstruction(
                        prompt=control.prompt,
                        control_id=control.control_id,
                        claim_id=claim_id,
                    )
                )
            self._steering.clear()
            if close_if_empty and not claimed:
                self._accepting_steer = False
            return tuple(claimed)

    async def _record_control(
        self,
        kind: SessionEventKind,
        control: ControlTransition,
        *,
        run_id: str,
        actor_id: str,
    ) -> SessionSnapshot:
        async with self._durable_lock:
            snapshot = await _call_session_store(
                self._session_store.load, self._session_id
            )
            return await _call_session_store(
                self._session_store.record_control,
                self._session_id,
                snapshot.version,
                kind,
                control,
                event_context=SessionEventContext(
                    run_id=run_id,
                    provenance=EventProvenance(
                        source="runtime",
                        actor_id=actor_id,
                    ),
                ),
            )

    async def _claim_control(self, control_id: str, *, run_id: str) -> str:
        async with self._durable_lock:
            snapshot = await _call_session_store(
                self._session_store.load, self._session_id
            )
            pending = next(
                (
                    item
                    for item in snapshot.pending_controls
                    if item.control.control_id == control_id
                ),
                None,
            )
            if pending is None:
                raise SessionControlError(
                    f"control {control_id!r} is no longer pending"
                )
            if (
                pending.claim is not None
                and pending.claim.owner_id != self._owner_id
                and pending.claim.lease_expires_at > self._clock()
            ):
                raise SessionControlError(
                    f"control {control_id!r} is claimed by another dispatcher"
                )
            claim_id = uuid4().hex
            claim = ControlClaim(
                control_id=control_id,
                claim_id=claim_id,
                owner_id=self._owner_id,
                lease_expires_at=(
                    self._clock() + self._control_claim_lease_seconds
                ),
            )
            await _call_session_store(
                self._session_store.record_control,
                self._session_id,
                snapshot.version,
                "control_claimed",
                claim,
                event_context=SessionEventContext(
                    run_id=run_id,
                    provenance=EventProvenance(
                        source="runtime",
                        actor_id="control-dispatcher",
                    ),
                ),
            )
            return claim_id

    async def _cancel_control(
        self,
        control_id: str,
        *,
        run_id: str,
        reason: str,
    ) -> None:
        await self._record_control(
            "control_cancelled",
            ControlCancellation(control_id, reason),
            run_id=run_id,
            actor_id="control-dispatcher",
        )

    async def begin_compaction(self) -> None:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("compaction requires an asyncio task")
        async with self._control_lock:
            if (
                self._state != "idle"
                or self._dispatcher_task is not None
                or self._queued_runs
            ):
                raise SessionControlError(
                    "manual compaction requires an idle session"
                )
            self._active_compaction_task = task
            self._state = "running"
            self._idle_event.clear()

    async def finish_compaction(self) -> None:
        async with self._control_lock:
            self._active_compaction_task = None
            self._state = "settled"
        await asyncio.sleep(0)
        async with self._control_lock:
            if self._queued_runs:
                if self._dispatcher_task is None:
                    self._start_dispatcher_locked()
            else:
                self._state = "idle"
                self._idle_event.set()


def _consume_background_result(future: asyncio.Future[AgentResult]) -> None:
    """Retrieve unobserved follow-up failures so asyncio does not warn."""
    if future.cancelled():
        return
    future.exception()
