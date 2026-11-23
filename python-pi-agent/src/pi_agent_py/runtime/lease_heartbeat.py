"""Lexically supervised lease heartbeats, cancellation and compare-release."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from math import isfinite
from typing import Generic, Literal, TypeVar, cast

from ..domain import EventKind, JSONValue
from ..errors import (
    LeaseLostError,
    LeaseTimeoutError,
    RuntimeStateError,
    SessionConflictError,
    SessionValidationError,
)
from ..execution_domain import ExecutionGrant, SessionLease, lease_ttl_ms, same_lease
from ..lease_clock import SystemLeaseClock
from ..observability.events import ensure_isolated_event_sink, make_agent_event
from ..ports import EventSink, ExecutionGrantManager, LeaseClock, SessionLeaseManager


T = TypeVar("T")
HeartbeatState = Literal["ready", "starting", "running", "lost", "closing", "closed"]


@dataclass(frozen=True, slots=True)
class LeaseHeartbeatSettings:
    ttl_seconds: float = 30.0
    heartbeat_interval: float = 10.0
    redis_timeout: float = 2.0
    grant_timeout: float = 2.0
    cleanup_timeout: float = 2.0
    safety_margin: float = 1.0

    def __post_init__(self) -> None:
        ttl = lease_ttl_ms(self.ttl_seconds) / 1000
        for value in (
            self.heartbeat_interval,
            self.redis_timeout,
            self.grant_timeout,
            self.cleanup_timeout,
            self.safety_margin,
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value <= 0
            ):
                raise SessionValidationError(
                    "heartbeat durations must be finite and positive"
                )
        if (
            self.heartbeat_interval
            + self.redis_timeout
            + self.grant_timeout
            + self.safety_margin
            >= ttl
        ):
            raise SessionValidationError(
                "heartbeat interval, I/O deadlines and margin must fit within TTL"
            )


@dataclass(frozen=True, slots=True)
class LeaseCleanupFailure:
    stage: Literal["grant", "redis"]
    error_type: str


@dataclass(frozen=True, slots=True)
class _WorkResult(Generic[T]):
    value: T | None = None
    error: BaseException | None = None


class LeaseHeartbeat:
    """One-shot owner of a lease/grant scope, not of the borrowed clients.

    A work callback must cooperate with cancellation and check ensure_active()
    before dispatch. finalize runs only after the heartbeat has been joined, so
    successful durable settlement cannot race a renewal of the cleared grant.
    """

    def __init__(
        self,
        lease_manager: SessionLeaseManager,
        grant_manager: ExecutionGrantManager,
        lease: SessionLease,
        grant: ExecutionGrant,
        *,
        settings: LeaseHeartbeatSettings = LeaseHeartbeatSettings(),
        clock: LeaseClock | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        if (lease.session_id, lease.owner_id, lease.claim_id) != (
            grant.session_id,
            grant.owner_id,
            grant.claim_id,
        ):
            raise SessionValidationError(
                "coordination lease and PG grant identities differ"
            )
        self._leases, self._grants = lease_manager, grant_manager
        self._lease, self._grant = lease, grant
        self._settings = settings
        self._ttl = lease_ttl_ms(settings.ttl_seconds) / 1000
        self._clock = clock if clock is not None else SystemLeaseClock()
        self._events = ensure_isolated_event_sink(event_sink)
        self._state: HeartbeatState = "ready"
        self._deadline: float | None = None
        self._lost: LeaseLostError | None = None
        self._work_task: asyncio.Task[object] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._runner: asyncio.Task[object] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._close_requested = False
        self._closed = asyncio.Event()
        self._cleanup_failures: list[LeaseCleanupFailure] = []

    @property
    def state(self) -> HeartbeatState:
        return self._state

    @property
    def lease(self) -> SessionLease:
        return self._lease

    @property
    def grant(self) -> ExecutionGrant:
        return self._grant

    @property
    def lost_reason(self) -> str | None:
        return self._lost.reason if self._lost else None

    @property
    def cleanup_failures(self) -> tuple[LeaseCleanupFailure, ...]:
        return tuple(self._cleanup_failures)

    def _emit(self, kind: EventKind, **fields: JSONValue) -> None:
        self._events.emit(
            make_agent_event(
                kind,
                "capability",
                source="lease_heartbeat",
                session_id=self._grant.session_id,
                run_id=self._grant.run_id,
                payload={"epoch": self._grant.epoch, **fields},
            )
        )

    def _lose(self, reason: str, cause: BaseException | None = None) -> LeaseLostError:
        if self._lost is None:
            self._lost = LeaseLostError(reason)
            self._lost.__cause__ = cause
            self._state = "lost"
            self._emit(
                "lease_lost",
                reason=reason,
                error_type=type(cause).__name__ if cause else None,
            )
        return self._lost

    def ensure_active(self) -> None:
        if self._lost is not None:
            raise self._lost
        if self._state != "running":
            raise LeaseLostError("not_running")
        if self._deadline is None or self._clock.monotonic() >= self._deadline:
            raise self._lose("deadline_exceeded")

    async def _bounded(self, call: Callable[[], Awaitable[T]], seconds: float) -> T:
        if seconds <= 0:
            raise LeaseTimeoutError("lease operation has no remaining time budget")
        end = self._clock.monotonic() + seconds
        try:
            async with self._clock.timeout(seconds):
                result = await call()
        except TimeoutError as error:
            raise LeaseTimeoutError("supervised lease operation timed out") from error
        if self._clock.monotonic() >= end:
            raise LeaseTimeoutError("lease operation completed after its deadline")
        return result

    async def _pulse(self) -> None:
        start = self._clock.monotonic()
        if self._deadline is not None and start >= self._deadline:
            raise self._lose("deadline_exceeded")
        remaining = (
            self._deadline - start
            if self._deadline is not None
            else self._settings.redis_timeout
        )
        try:
            renewed = await self._bounded(
                lambda: self._leases.renew(self._lease, self._ttl),
                min(self._settings.redis_timeout, remaining),
            )
            if renewed is None:
                raise self._lose("redis_lost")
            if not same_lease(renewed, self._lease):
                raise SessionValidationError("Redis renewal changed lease identity")
            self._lease = renewed
        except LeaseLostError:
            raise
        except LeaseTimeoutError as error:
            raise self._lose("redis_timeout", error)
        except Exception as error:
            raise self._lose("redis_error", error)
        deadline = start + self._ttl - self._settings.safety_margin
        if self._deadline is not None:
            deadline = min(
                deadline, self._deadline
            )  # old PG grant must remain live during renewal
        try:
            current = self._grant
            updated = await self._bounded(
                lambda: self._grants.renew_grant(current, self._ttl),
                min(self._settings.grant_timeout, deadline - self._clock.monotonic()),
            )
            if (
                updated.session_id,
                updated.owner_id,
                updated.claim_id,
                updated.epoch,
                updated.purpose,
                updated.run_id,
                updated.maintenance,
            ) != (
                current.session_id,
                current.owner_id,
                current.claim_id,
                current.epoch,
                current.purpose,
                current.run_id,
                current.maintenance,
            ):
                raise SessionValidationError("PG renewal changed grant identity")
            self._grant = updated
        except LeaseTimeoutError as error:
            raise self._lose("grant_timeout", error)
        except SessionConflictError as error:
            raise self._lose("grant_lost", error)
        except Exception as error:
            raise self._lose("grant_error", error)
        self._deadline = start + self._ttl - self._settings.safety_margin
        self._emit("lease_renewed")

    async def _heartbeats(self) -> None:
        try:
            while True:
                await self._clock.sleep(self._settings.heartbeat_interval)
                await self._pulse()
        except asyncio.CancelledError:
            return
        except Exception as error:
            self._lose("heartbeat_error", error)

    async def run(
        self,
        work: Callable[["LeaseHeartbeat"], Awaitable[T]],
        *,
        finalize: Callable[[T, ExecutionGrant], Awaitable[None]] | None = None,
    ) -> T:
        if self._state != "ready":
            raise RuntimeStateError(
                "lease heartbeat is one-shot and already started/closed"
            )
        self._runner = cast(asyncio.Task[object], asyncio.current_task())
        self._state = "starting"
        failure: BaseException | None = None
        result: T | None = None

        async def invoke() -> _WorkResult[T]:
            try:
                return _WorkResult(value=await work(self))
            except (Exception, asyncio.CancelledError) as error:
                return _WorkResult(error=error)

        try:
            # Revalidate both live services before allowing a cached grant to dispatch.
            self._emit("lease_heartbeat_started")
            await self._pulse()
            self._state = "running"
            async with asyncio.TaskGroup() as group:
                work_task = group.create_task(
                    invoke(), name="lease-work:" + self._grant.session_id
                )
                heartbeat = group.create_task(
                    self._heartbeats(), name="lease-heartbeat:" + self._grant.session_id
                )
                self._work_task = cast(asyncio.Task[object], work_task)
                self._heartbeat_task = heartbeat
                done, _ = await asyncio.wait(
                    (work_task, heartbeat), return_when=asyncio.FIRST_COMPLETED
                )
                if heartbeat in done:
                    if self._lost is None:
                        self._lose("heartbeat_stopped")
                    work_task.cancel()
                outcome = await work_task
                heartbeat.cancel()
            # Business exceptions are raised outside TaskGroup, without an ExceptionGroup wrapper.
            if self._lost is not None:
                raise self._lost
            if outcome.error is not None:
                raise outcome.error
            self.ensure_active()
            result = outcome.value
            if finalize is not None:
                assert self._deadline is not None
                await self._bounded(
                    lambda: finalize(cast(T, result), self._grant),
                    min(
                        self._settings.grant_timeout,
                        self._deadline - self._clock.monotonic(),
                    ),
                )
        except BaseException as error:
            failure = error
        finally:
            # Every child has exited its TaskGroup before the clients can be released.
            self._work_task = None
            self._heartbeat_task = None
            cancelled = await self._close()
            if cancelled and failure is None:
                failure = asyncio.CancelledError()
        if failure is not None:
            raise failure
        return cast(T, result)

    async def _cleanup(self) -> None:
        for stage, call in (
            ("grant", lambda: self._grants.release_grant(self._grant)),
            ("redis", lambda: self._leases.release(self._lease)),
        ):
            try:
                await self._bounded(call, self._settings.cleanup_timeout)
            except (Exception, asyncio.CancelledError) as error:
                self._cleanup_failures.append(
                    LeaseCleanupFailure(
                        cast(Literal["grant", "redis"], stage), type(error).__name__
                    )
                )
                self._emit(
                    "lease_cleanup_failed", stage=stage, error_type=type(error).__name__
                )
        self._emit("lease_heartbeat_stopped", lost=self._lost is not None)

    async def _close(self) -> bool:
        self._state = "closing"
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(
                self._cleanup(), name="lease-cleanup:" + self._grant.session_id
            )
        cancelled = False
        try:
            while not self._cleanup_task.done():
                try:
                    await asyncio.shield(self._cleanup_task)
                except asyncio.CancelledError:
                    cancelled = True
            self._cleanup_task.result()
        finally:
            self._state = "closed"
            self._runner = None
            self._closed.set()
        return cancelled

    async def aclose(self) -> None:
        """Cancel the owned run and join cleanup, or release an unstarted scope."""
        if self._state == "closed":
            return
        current = asyncio.current_task()
        if (
            current in (self._runner, self._work_task, self._heartbeat_task)
            and self._runner is not None
        ):
            raise RuntimeStateError("a lease scope cannot close itself from a callback")
        if self._runner is not None:
            if not self._close_requested:
                self._close_requested = True
                self._runner.cancel()
            await self._closed.wait()
        elif await self._close():
            raise asyncio.CancelledError()
