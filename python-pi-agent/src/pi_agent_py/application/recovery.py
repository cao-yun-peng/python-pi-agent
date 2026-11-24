"""Registered configuration reconstruction for already-claimed durable Runs."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import Literal, cast

from ..domain import EventKind
from ..errors import (
    ConfigurationError,
    RecoveryConfigurationError,
    RuntimeBuildError,
    SessionControlError,
)
from ..execution_domain import (
    ExecutionReceipt,
    FinishRun,
    RecoveryOutcome,
    SessionLease,
    _text,
)
from ..lease_clock import SystemLeaseClock
from ..observability.events import ensure_isolated_event_sink, make_agent_event
from ..ports import EventSink, SessionStore
from ..runtime.distributed_execution import _Outcome
from ..runtime.execution_config import DistributedExecution
from ..runtime.execution_writer import _ExecutionSessionWriter
from ..runtime.lease_heartbeat import LeaseHeartbeat
from ..runtime.recovery import join_owned
from .runtime import AgentRuntime, RuntimeSession

RecoveryRuntimeFactory = Callable[[ExecutionReceipt], Awaitable[AgentRuntime]]


@dataclass(frozen=True, slots=True)
class RecoveryDispatchSettings:
    rebuild_timeout: float = 10.0
    close_timeout: float = 5.0

    def __post_init__(self) -> None:
        for value in (self.rebuild_timeout, self.close_timeout):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value <= 0
            ):
                raise ValueError(
                    "recovery reconstruction deadlines must be finite and positive"
                )


class RegisteredRecoveryDispatcher:
    """Borrowed infrastructure, immutable catalog, a fresh owned Runtime per Run.

    Factories must rebuild the registered profile/tools/policies/workspace and
    return a non-recovering Runtime with borrowed shared infrastructure. Builder
    rollback owns failures during construction; this dispatcher closes each
    returned Runtime after its supervised attempt has finished.
    """

    def __init__(
        self,
        reader: SessionStore,
        execution: DistributedExecution,
        factories: Mapping[tuple[str, str], RecoveryRuntimeFactory],
        *,
        settings: RecoveryDispatchSettings = RecoveryDispatchSettings(),
        event_sink: EventSink | None = None,
    ) -> None:
        catalog = dict(factories)
        for ref, digest in catalog:
            _text(ref, "recovery_spec_ref")
            _text(digest, "recovery_spec_hash")
            if not callable(catalog[ref, digest]):
                raise TypeError("recovery factories must be callable")
        self._factories = MappingProxyType(catalog)
        self.reader, self.execution, self.settings = reader, execution, settings
        self._clock = execution.clock or SystemLeaseClock()
        self._events = ensure_isolated_event_sink(event_sink)
        self._live_runtimes: set[AgentRuntime] = set()

    def _emit(
        self, kind: EventKind, receipt: ExecutionReceipt, **payload: str | int | None
    ) -> None:
        record = receipt.run
        self._events.emit(
            make_agent_event(
                kind,
                "capability",
                source="recovery_dispatcher",
                session_id=receipt.session.session_id,
                run_id=record.submission.run_id if record else None,
                payload=payload,
            )
        )

    async def recover(
        self, receipt: ExecutionReceipt, lease: SessionLease
    ) -> RecoveryOutcome:
        if (
            receipt.grant is None
            or receipt.run is None
            or receipt.run.state != "running"
        ):
            raise SessionControlError(
                "recovery dispatch requires an already-claimed running Run"
            )
        config = self.execution
        scope = LeaseHeartbeat(
            config.leases,
            config.store,
            lease,
            receipt.grant,
            settings=config.settings.heartbeat,
            clock=config.clock,
            event_sink=self._events,
        )
        runtime: AgentRuntime | None = None
        session: RuntimeSession | None = None
        writer: _ExecutionSessionWriter | None = None
        rejection: str | None = None
        finished: RecoveryOutcome | None = None
        accepted_runtime = False
        record = receipt.run

        async def work(guard: LeaseHeartbeat) -> _Outcome:
            nonlocal runtime, session, writer, rejection, accepted_runtime
            try:
                async with self._clock.timeout(self.settings.rebuild_timeout):
                    key = (
                        record.submission.recovery_spec_ref,
                        record.submission.recovery_spec_hash,
                    )
                    factory = self._factories.get(key)
                    if factory is None:
                        code = (
                            "recovery_spec_mismatch"
                            if any(ref == key[0] for ref, _ in self._factories)
                            else "recovery_spec_missing"
                        )
                        raise RecoveryConfigurationError(code)
                    self._emit(
                        "recovery_rebuilding",
                        receipt,
                        epoch=guard.grant.epoch,
                        attempt=record.attempt,
                    )
                    runtime = await factory(receipt)
                    if not isinstance(runtime, AgentRuntime):
                        raise RecoveryConfigurationError("recovery_binding_mismatch")
                    if (
                        runtime in self._live_runtimes
                        or runtime.recovery_worker is not None
                    ):
                        # Never close another job's or the hosting Runtime's resources.
                        runtime = None
                        raise RecoveryConfigurationError("recovery_binding_mismatch")
                    self._live_runtimes.add(runtime)
                    accepted_runtime = True
                    selected = runtime._distributed_execution
                    if (
                        runtime.state != "running"
                        or runtime._session_store is not self.reader
                        or selected is None
                        or selected.store is not config.store
                        or selected.leases is not config.leases
                        or selected.settings.owner_id != guard.grant.owner_id
                    ):
                        raise RecoveryConfigurationError("recovery_binding_mismatch")
                    guard.ensure_active()
                    session = await runtime.open_session(record.submission.session_id)
                    writer = await session._entry.session._begin_claimed(receipt, guard)
            except RecoveryConfigurationError as error:
                rejection = error.code
            except (ConfigurationError, RuntimeBuildError) as error:
                rejection = (
                    "recovery_spec_mismatch"
                    if isinstance(error, ConfigurationError)
                    else "recovery_build_failed"
                )
            if rejection is not None:
                self._emit(
                    "recovery_rejected",
                    receipt,
                    epoch=guard.grant.epoch,
                    error_code=rejection,
                )
                return _Outcome()
            assert session is not None and writer is not None
            return await session._entry.session._work_claimed(writer)

        async def finalize(outcome: _Outcome, unused_grant: object) -> None:
            nonlocal writer, finished
            if rejection is not None:
                writer = _ExecutionSessionWriter(
                    config.store, self.reader, receipt, scope, asyncio.Lock()
                )
                await writer.finish(FinishRun("failed", rejection))
            else:
                assert session is not None and writer is not None
                await session._entry.session._finish_claimed(writer, outcome)
            final = writer.record
            if final.state not in ("completed", "failed", "cancelled"):
                raise SessionControlError("recovery did not commit a terminal Run")
            finished = RecoveryOutcome(
                final.submission.session_id,
                final.submission.run_id,
                cast(Literal["completed", "failed", "cancelled"], final.state),
                final.epoch,
                final.attempt,
                final.error_code,
            )

        try:
            await scope.run(work, finalize=finalize)
            assert finished is not None
            return finished
        finally:

            async def cleanup() -> None:
                try:
                    async with self._clock.timeout(self.settings.close_timeout):
                        if session is not None:
                            await session._entry.session._end_claimed()
                            await session.close()
                        if runtime is not None and accepted_runtime:
                            await runtime.aclose()
                except Exception as error:
                    self._emit(
                        "recovery_cleanup_failed",
                        receipt,
                        stage="runtime",
                        error_type=type(error).__name__,
                    )
                finally:
                    if runtime is not None and accepted_runtime:
                        self._live_runtimes.discard(runtime)

            task = asyncio.create_task(
                cleanup(), name="recovery-runtime-close:" + lease.session_id
            )
            if await join_owned(task):
                raise asyncio.CancelledError()
