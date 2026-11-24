"""Bounded candidate discovery and supervised, per-Session recovery dispatch."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from math import isfinite
from typing import Literal, cast
from uuid import uuid4

from ..domain import EventKind
from ..errors import (
    RuntimeCloseError,
    RuntimeStateError,
    SessionConflictError,
    SessionStorageError,
)
from ..execution_domain import (
    ClaimExecutionRequest,
    ExecutionGrant,
    SessionLease,
    RecoveryOutcome,
)
from ..lease_clock import SystemLeaseClock
from ..observability.events import ensure_isolated_event_sink, make_agent_event
from ..ports import EventSink, RecoveryDispatcher
from ..storage.execution_codec import execution_operation
from .execution_config import DistributedExecution


@dataclass(frozen=True, slots=True)
class RecoveryWorkerSettings:
    scan_limit: int = 32
    max_concurrent_sessions: int = 4
    poll_interval: float = 1.0
    retry_delay: float = 0.5
    max_retry_delay: float = 30.0
    max_cooldowns: int = 1024
    scan_timeout: float = 5.0
    claim_timeout: float = 5.0
    cleanup_timeout: float = 2.0
    shutdown_grace: float = 0.0

    def __post_init__(self) -> None:
        for capacity in (
            self.scan_limit,
            self.max_concurrent_sessions,
            self.max_cooldowns,
        ):
            if type(capacity) is not int or capacity <= 0:
                raise ValueError("recovery capacities must be positive integers")
        for value in (
            self.poll_interval,
            self.retry_delay,
            self.max_retry_delay,
            self.scan_timeout,
            self.claim_timeout,
            self.cleanup_timeout,
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value <= 0
            ):
                raise ValueError("recovery deadlines must be finite and positive")
        if self.retry_delay > self.max_retry_delay:
            raise ValueError("retry delay must fit the maximum backoff")
        if (
            isinstance(self.shutdown_grace, bool)
            or not isinstance(self.shutdown_grace, (int, float))
            or not isfinite(self.shutdown_grace)
            or self.shutdown_grace < 0
        ):
            raise ValueError("shutdown grace must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class RecoveryAttempt:
    session_id: str
    status: Literal["completed", "failed", "cancelled", "busy", "empty", "retry"]
    run_id: str | None = None
    epoch: int | None = None
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryPass:
    discovered: tuple[str, ...]
    attempts: tuple[RecoveryAttempt, ...]


async def join_owned(task: asyncio.Task[None]) -> bool:
    """Join shielded cleanup even if its caller is cancelled repeatedly."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    return cancelled


class SessionRecoveryWorker:
    def __init__(
        self,
        execution: DistributedExecution,
        dispatcher: RecoveryDispatcher,
        *,
        settings: RecoveryWorkerSettings = RecoveryWorkerSettings(),
        event_sink: EventSink | None = None,
    ) -> None:
        if (
            settings.claim_timeout
            >= execution.settings.heartbeat.ttl_seconds
            - execution.settings.heartbeat.safety_margin
        ):
            raise ValueError("claim deadline must fit the coordination TTL")
        self.execution, self.dispatcher, self.settings = execution, dispatcher, settings
        self._clock = execution.clock or SystemLeaseClock()
        self._events = ensure_isolated_event_sink(event_sink)
        self._state: Literal[
            "ready", "running_once", "running", "closing", "closed", "failed"
        ] = "ready"
        self._active: dict[str, asyncio.Task[RecoveryAttempt]] = {}
        self._cooldowns: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._main: asyncio.Task[None] | None = None
        self._manual: asyncio.Task[object] | None = None
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._close_lock = asyncio.Lock()
        self._failure: BaseException | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def active_sessions(self) -> tuple[str, ...]:
        return tuple(self._active)

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    def _emit(
        self,
        kind: EventKind,
        sid: str | None = None,
        rid: str | None = None,
        **payload: str | int | None,
    ) -> None:
        self._events.emit(
            make_agent_event(
                kind,
                "capability",
                source="session_recovery",
                session_id=sid,
                run_id=rid,
                payload=payload,
            )
        )

    def _backoff(self, sid: str) -> None:
        count = min(self._cooldowns.get(sid, (0, 0))[1] + 1, 32)
        delay = min(
            self.settings.max_retry_delay,
            self.settings.retry_delay * (2 ** (count - 1)),
        )
        self._cooldowns[sid] = (self._clock.monotonic() + delay, count)
        self._cooldowns.move_to_end(sid)
        while len(self._cooldowns) > self.settings.max_cooldowns:
            self._cooldowns.popitem(last=False)

    async def _discover(
        self, group: asyncio.TaskGroup
    ) -> tuple[tuple[str, ...], list[asyncio.Task[RecoveryAttempt]]]:
        room = self.settings.max_concurrent_sessions - len(self._active)
        if room <= 0:
            return (), []
        now = self._clock.monotonic()
        exclude = tuple(
            dict.fromkeys(
                (
                    *self._active,
                    *(
                        sid
                        for sid, (deadline, _) in self._cooldowns.items()
                        if deadline > now
                    ),
                )
            )
        )
        async with self._clock.timeout(self.settings.scan_timeout):
            candidates = await self.execution.store.list_candidates(
                self.settings.scan_limit, exclude=exclude
            )
        unique = tuple(dict.fromkeys(candidates))
        self._emit("recovery_candidates", count=len(unique))
        tasks: list[asyncio.Task[RecoveryAttempt]] = []
        for sid in unique:
            if len(tasks) >= room:
                break
            if sid in exclude:
                continue
            task = group.create_task(self._attempt(sid), name="recovery-session:" + sid)
            self._active[sid] = task

            def done(
                completed: asyncio.Task[RecoveryAttempt], session_id: str = sid
            ) -> None:
                if self._active.get(session_id) is completed:
                    self._active.pop(session_id)

            task.add_done_callback(done)
            tasks.append(task)
        return unique, tasks

    async def run_once(self) -> RecoveryPass:
        if self._state != "ready":
            raise RuntimeStateError("run_once requires a ready, non-background worker")
        self._state = "running_once"
        self._manual = cast(asyncio.Task[object], asyncio.current_task())
        try:
            try:
                async with asyncio.TaskGroup() as group:
                    discovered, tasks = await self._discover(group)
            except ExceptionGroup as errors:
                if len(errors.exceptions) == 1:
                    raise errors.exceptions[0] from errors
                raise
            return RecoveryPass(discovered, tuple(task.result() for task in tasks))
        finally:
            self._manual = None
            if self._state == "running_once":
                self._state = "ready"

    async def _attempt(self, sid: str) -> RecoveryAttempt:
        lease: SessionLease | None = None
        grant: ExecutionGrant | None = None
        rid: str | None = None
        try:
            async with self._clock.timeout(self.settings.claim_timeout):
                lease = await self.execution.leases.acquire(
                    sid,
                    self.execution.settings.owner_id,
                    uuid4().hex,
                    self.execution.settings.heartbeat.ttl_seconds,
                )
                if lease is None:
                    self._backoff(sid)
                    self._emit("recovery_busy", sid)
                    return RecoveryAttempt(sid, "busy")
                request = ClaimExecutionRequest(
                    lease, self.execution.settings.heartbeat.ttl_seconds
                )
                operation = execution_operation(uuid4().hex, request)
                from ..errors import SessionCommitUnknownError

                try:
                    receipt = await self.execution.store.claim_next(
                        lease, request.ttl, operation
                    )
                except SessionCommitUnknownError:
                    receipt = await self.execution.store.resolve_operation(
                        sid, operation.operation_id
                    )
                    if receipt is None:
                        raise
                if receipt is None or receipt.run is None:
                    self._cooldowns.pop(sid, None)
                    return RecoveryAttempt(sid, "empty")
                grant = receipt.grant
                record = receipt.run
                rid = record.submission.run_id
                if grant is None:
                    if record.state not in ("completed", "failed", "cancelled"):
                        raise SessionStorageError("ungranted claim is not terminal")
                    self._emit(
                        "recovery_completed",
                        sid,
                        rid,
                        state=record.state,
                        attempt=record.attempt,
                        error_code=record.error_code,
                    )
                    self._cooldowns.pop(sid, None)
                    return RecoveryAttempt(
                        sid,
                        cast(Literal["completed", "failed", "cancelled"], record.state),
                        rid,
                        record.epoch,
                    )
            self._emit(
                "recovery_started",
                sid,
                rid,
                epoch=grant.epoch,
                attempt=record.attempt,
                operation_id=operation.operation_id,
                source_version=receipt.session.version,
            )
            outcome: RecoveryOutcome = await self.dispatcher.recover(receipt, lease)
            if (outcome.session_id, outcome.run_id, outcome.epoch) != (
                sid,
                rid,
                grant.epoch,
            ):
                raise SessionStorageError(
                    "recovery dispatcher returned a foreign outcome"
                )
            async with self._clock.timeout(self.settings.scan_timeout):
                committed = await self.execution.store.load_execution(sid, rid)
            if committed is None or (
                committed.state,
                committed.epoch,
                committed.attempt,
                committed.error_code,
            ) != (outcome.state, outcome.epoch, outcome.attempt, outcome.error_code):
                raise SessionStorageError("recovery outcome is not durably terminal")
            self._cooldowns.pop(sid, None)
            self._emit(
                "recovery_completed",
                sid,
                rid,
                epoch=outcome.epoch,
                attempt=outcome.attempt,
                state=outcome.state,
                error_code=outcome.error_code,
            )
            return RecoveryAttempt(sid, outcome.state, rid, outcome.epoch)
        except asyncio.CancelledError:
            self._emit(
                "recovery_stopped", sid, rid, epoch=grant.epoch if grant else None
            )
            raise
        except SessionConflictError as error:
            self._backoff(sid)
            status: Literal["busy", "retry"] = "busy" if grant is None else "retry"
            self._emit(
                "recovery_busy" if grant is None else "recovery_failed",
                sid,
                rid,
                epoch=grant.epoch if grant else None,
                error_type=type(error).__name__,
            )
            return RecoveryAttempt(
                sid, status, rid, grant.epoch if grant else None, type(error).__name__
            )
        except Exception as error:
            self._backoff(sid)
            self._emit(
                "recovery_failed",
                sid,
                rid,
                epoch=grant.epoch if grant else None,
                error_type=type(error).__name__,
            )
            return RecoveryAttempt(
                sid, "retry", rid, grant.epoch if grant else None, type(error).__name__
            )
        finally:

            async def cleanup() -> None:
                if grant is not None:
                    try:
                        async with self._clock.timeout(self.settings.cleanup_timeout):
                            await self.execution.store.release_grant(grant)
                    except Exception as error:
                        self._emit(
                            "recovery_cleanup_failed",
                            sid,
                            rid,
                            stage="grant",
                            error_type=type(error).__name__,
                        )
                if lease is not None:
                    try:
                        async with self._clock.timeout(self.settings.cleanup_timeout):
                            await self.execution.leases.release(lease)
                    except Exception as error:
                        self._emit(
                            "recovery_cleanup_failed",
                            sid,
                            rid,
                            stage="redis",
                            error_type=type(error).__name__,
                        )

            task = asyncio.create_task(cleanup(), name="recovery-cleanup:" + sid)
            if await join_owned(task):
                raise asyncio.CancelledError()

    async def start(self) -> None:
        if self._state != "ready":
            raise RuntimeStateError("recovery worker is already started or closed")
        self._state = "running"
        self._main = asyncio.create_task(
            self._serve(), name="recovery-worker:" + self.execution.settings.owner_id
        )
        await self._ready.wait()
        if self._failure is not None:
            raise RuntimeStateError(
                "recovery worker failed during startup"
            ) from self._failure
        if self._state != "running":
            raise RuntimeStateError("recovery worker closed during startup")

    async def _tick(self) -> None:
        async with asyncio.TaskGroup() as group:
            sleep = group.create_task(
                self._clock.sleep(self.settings.poll_interval), name="recovery-poll"
            )
            stop = group.create_task(self._stop.wait(), name="recovery-stop")
            await asyncio.wait((sleep, stop), return_when=asyncio.FIRST_COMPLETED)
            sleep.cancel()
            stop.cancel()

    async def _serve(self) -> None:
        try:
            async with asyncio.TaskGroup() as group:
                self._ready.set()
                self._emit("recovery_worker_started")
                while not self._stop.is_set():
                    try:
                        await self._discover(group)
                    except (SessionStorageError, TimeoutError) as error:
                        self._emit(
                            "recovery_scan_failed", error_type=type(error).__name__
                        )
                    await self._tick()
                tasks = tuple(self._active.values())
                if tasks and self.settings.shutdown_grace > 0:
                    try:
                        async with self._clock.timeout(self.settings.shutdown_grace):
                            await asyncio.gather(*(asyncio.shield(t) for t in tasks))
                    except TimeoutError:
                        pass
                for task in tasks:
                    if not task.done():
                        task.cancel()
        except asyncio.CancelledError:
            return
        except Exception as error:
            self._failure = error
            self._state = "failed"
            self._emit("recovery_worker_failed", error_type=type(error).__name__)
        finally:
            self._ready.set()
            self._emit("recovery_worker_stopped")

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._state == "closed":
                return
            current = asyncio.current_task()
            if (
                current in (self._main, self._manual)
                or current in self._active.values()
            ):
                raise RuntimeStateError(
                    "a recovery worker cannot close itself from its job"
                )
            self._state = "closing"
            self._stop.set()
            cancelled = False
            if self._manual is not None:
                manual = self._manual
                manual.cancel()

                async def join_manual() -> None:
                    await asyncio.gather(manual, return_exceptions=True)

                cancelled = await join_owned(
                    asyncio.create_task(join_manual(), name="recovery-manual-close")
                )
            if self._main is not None:
                if self.settings.shutdown_grace == 0 and self._ready.is_set():
                    self._main.cancel()
                cancelled = await join_owned(self._main) or cancelled
            self._state = "closed"
            if cancelled:
                raise asyncio.CancelledError()
            if self._failure is not None:
                raise RuntimeCloseError(
                    "recovery worker failed", failures=(self._failure,)
                ) from self._failure
