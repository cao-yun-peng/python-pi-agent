"""Long-lived application facade above the small AgentSession core."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from .components import AgentComponents, _resolve_components
from .domain import AgentResult, EventKind, SessionSnapshot, SessionState
from .errors import (
    RuntimeCapacityError,
    RuntimeCloseError,
    RuntimeStateError,
)
from .events import EventSinkFailure, ensure_isolated_event_sink, make_agent_event
from .lifecycle import LifecycleScope
from .plugins import PluginHost
from .ports import (
    ApprovalPolicy,
    CompactionPolicy,
    CompactionStrategy,
    ContextStrategy,
    EventSink,
    ModelProvider,
    NextTurnHook,
    SessionStore,
    Tool,
    ToolExecutionGate,
)
from .runtime import AgentSession, _call_session_store

RuntimeState = Literal["running", "closing", "closed"]
Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class SessionPoolPolicy:
    """Bounds live session controllers while durable history stays in the store."""

    max_active_sessions: int = 64
    idle_ttl_seconds: float | None = 900.0

    def __post_init__(self) -> None:
        if type(self.max_active_sessions) is not int or self.max_active_sessions <= 0:
            raise ValueError("max_active_sessions must be a positive integer")
        ttl = self.idle_ttl_seconds
        if ttl is not None and (
            not isinstance(ttl, (int, float))
            or isinstance(ttl, bool)
            or not math.isfinite(ttl)
            or ttl < 0
        ):
            raise ValueError("idle_ttl_seconds must be a finite non-negative number")


@dataclass(slots=True)
class _SessionEntry:
    session_id: str
    session: AgentSession
    ready: asyncio.Task[None]
    leases: int
    last_used_at: float
    opened_emitted: bool = False


class RuntimeSession:
    """Lease-scoped facade that prevents transports from owning AgentSession."""

    def __init__(self, runtime: AgentRuntime, entry: _SessionEntry) -> None:
        self._runtime = runtime
        self._entry = entry
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._entry.session_id

    @property
    def state(self) -> SessionState:
        self._ensure_open()
        return self._entry.session.state

    @property
    def active_run_id(self) -> str | None:
        self._ensure_open()
        return self._entry.session.active_run_id

    async def run(self, prompt: str) -> AgentResult:
        self._ensure_usable()
        return await self._entry.session.run(prompt)

    async def follow_up(self, prompt: str) -> str:
        self._ensure_usable()
        return await self._entry.session.follow_up(prompt)

    async def steer(self, prompt: str) -> None:
        self._ensure_usable()
        await self._entry.session.steer(prompt)

    async def abort(self, *, clear_queue: bool = True) -> None:
        self._ensure_open()
        await self._entry.session.abort(clear_queue=clear_queue)

    async def wait_until_idle(self) -> None:
        self._ensure_open()
        await self._entry.session.wait_until_idle()

    async def compact(self, *, keep_recent_turns: int = 1) -> SessionSnapshot:
        self._ensure_usable()
        return await self._entry.session.compact(
            keep_recent_turns=keep_recent_turns
        )

    async def snapshot(self) -> SessionSnapshot:
        self._ensure_open()
        return await self._runtime.snapshot(self.session_id)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._runtime._release_session(self._entry)

    async def __aenter__(self) -> RuntimeSession:
        self._ensure_open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        del exc_info
        await self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeStateError("runtime session lease is closed")

    def _ensure_usable(self) -> None:
        self._ensure_open()
        if self._runtime.state != "running":
            raise RuntimeStateError("agent runtime is not accepting new work")


class AgentRuntime:
    """Owns application resources and reusable session controllers."""

    def __init__(
        self,
        provider: AgentComponents | ModelProvider | None = None,
        session_store: SessionStore | None = None,
        *,
        components: AgentComponents | None = None,
        event_sink: EventSink | None = None,
        tools: Sequence[Tool] | None = None,
        approval_policy: ApprovalPolicy | None = None,
        execution_gate: ToolExecutionGate | None = None,
        compaction_policy: CompactionPolicy | None = None,
        compaction_strategy: CompactionStrategy | None = None,
        context_strategy: ContextStrategy | None = None,
        system_prompt: str | None = None,
        max_model_requests: int | None = None,
        plugin_host: PluginHost | None = None,
        next_turn_hook: NextTurnHook | None = None,
        session_pool_policy: SessionPoolPolicy | None = None,
        graceful_shutdown_seconds: float = 5.0,
        lifecycle: LifecycleScope | None = None,
        observer_lifecycle: LifecycleScope | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        if (
            not isinstance(graceful_shutdown_seconds, (int, float))
            or isinstance(graceful_shutdown_seconds, bool)
            or not math.isfinite(graceful_shutdown_seconds)
            or graceful_shutdown_seconds < 0
        ):
            raise ValueError(
                "graceful_shutdown_seconds must be a finite non-negative number"
            )
        if session_store is None:
            raise TypeError("session_store is required")
        self._components = _resolve_components(
            provider,
            components,
            tools=tools,
            approval_policy=approval_policy,
            execution_gate=execution_gate,
            context_strategy=context_strategy,
            compaction_policy=compaction_policy,
            compaction_strategy=compaction_strategy,
            next_turn_hook=next_turn_hook,
            system_prompt=system_prompt,
            max_model_requests=max_model_requests,
        )
        self._session_store = session_store
        self._event_sink = ensure_isolated_event_sink(event_sink)
        self._plugin_host = plugin_host
        self._pool_policy = session_pool_policy or SessionPoolPolicy()
        self._graceful_shutdown_seconds = float(graceful_shutdown_seconds)
        self._lifecycle = lifecycle or LifecycleScope(name="runtime-components")
        self._observer_lifecycle = observer_lifecycle or LifecycleScope(
            name="runtime-observers"
        )
        self._clock = clock
        self._sessions: dict[str, _SessionEntry] = {}
        self._state: RuntimeState = "running"
        self._lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._emit("runtime_started")

    @property
    def state(self) -> RuntimeState:
        return self._state

    @property
    def active_session_count(self) -> int:
        return len(self._sessions)

    @property
    def event_sink_failures(self) -> tuple[EventSinkFailure, ...]:
        return self._event_sink.failures

    async def open_session(self, session_id: str) -> RuntimeSession:
        """Acquire one reusable session controller behind a releasable lease."""
        if not session_id.strip():
            raise ValueError("session_id must not be empty")

        evicted: list[_SessionEntry] = []
        async with self._lock:
            self._ensure_running()
            now = self._clock()
            evicted.extend(self._remove_expired_locked(now))
            entry = self._sessions.get(session_id)
            if entry is None:
                if len(self._sessions) >= self._pool_policy.max_active_sessions:
                    candidate = self._oldest_evictable_locked()
                    if candidate is None:
                        raise RuntimeCapacityError(
                            "agent runtime session capacity is exhausted"
                        )
                    self._sessions.pop(candidate.session_id)
                    evicted.append(candidate)
                session = self._new_session(session_id)
                entry = _SessionEntry(
                    session_id=session_id,
                    session=session,
                    ready=asyncio.create_task(session.restore()),
                    leases=0,
                    last_used_at=now,
                )
                self._sessions[session_id] = entry
            entry.leases += 1

        try:
            for expired in evicted:
                await self._close_evicted(expired)
        except BaseException:
            await self._release_session(entry)
            raise

        try:
            await asyncio.shield(entry.ready)
        except asyncio.CancelledError:
            await self._release_session(entry)
            raise
        except BaseException:
            await self._discard_failed_session(entry)
            raise

        should_emit = False
        runtime_stopped = False
        async with self._lock:
            if self._sessions.get(session_id) is entry and self._state == "running":
                if not entry.opened_emitted:
                    entry.opened_emitted = True
                    should_emit = True
            else:
                runtime_stopped = True
        if should_emit:
            self._emit("runtime_session_opened", session_id=session_id)
        if runtime_stopped:
            await self._release_session(entry)
            raise RuntimeStateError("agent runtime closed while opening a session")
        return RuntimeSession(self, entry)

    async def run(self, session_id: str, prompt: str) -> AgentResult:
        """Convenience API for one awaited run with an automatically released lease."""
        async with await self.open_session(session_id) as session:
            return await session.run(prompt)

    async def snapshot(self, session_id: str) -> SessionSnapshot:
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        if self._state == "closed":
            raise RuntimeStateError("agent runtime is closed")
        return await _call_session_store(self._session_store.load, session_id)

    async def aclose(self) -> None:
        """Stop accepting work, drain briefly, then close every owned resource."""
        async with self._close_lock:
            if self._state == "closed":
                return
            self._state = "closing"
            self._emit("runtime_closing")
            async with self._lock:
                entries = tuple(self._sessions.values())
                self._sessions.clear()

            failures: list[BaseException] = []
            if entries and self._graceful_shutdown_seconds > 0:
                try:
                    async with asyncio.timeout(self._graceful_shutdown_seconds):
                        await asyncio.gather(
                            *(entry.session.wait_until_idle() for entry in entries)
                        )
                except TimeoutError:
                    pass
                except BaseException as error:
                    failures.append(error)

            for entry in entries:
                try:
                    await entry.session.close()
                except BaseException as error:
                    failures.append(error)

            failures.extend(await _close_scope(self._lifecycle))
            self._state = "closed"
            self._emit(
                "runtime_closed",
                cleanup_error_count=len(failures),
            )
            failures.extend(await _close_scope(self._observer_lifecycle))
            if failures:
                raise RuntimeCloseError(
                    f"agent runtime had {len(failures)} shutdown failure(s)",
                    failures=tuple(failures),
                ) from failures[0]

    async def __aenter__(self) -> AgentRuntime:
        self._ensure_running()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        del exc_info
        await self.aclose()

    async def _release_session(self, entry: _SessionEntry) -> None:
        async with self._lock:
            current = self._sessions.get(entry.session_id)
            if current is not entry:
                return
            if entry.leases <= 0:
                raise RuntimeStateError("runtime session lease count underflow")
            entry.leases -= 1
            entry.last_used_at = self._clock()

    def _new_session(self, session_id: str) -> AgentSession:
        return AgentSession(
            session_id,
            self._session_store,
            self._components,
            event_sink=self._event_sink,
            plugin_host=self._plugin_host,
        )

    def _remove_expired_locked(self, now: float) -> list[_SessionEntry]:
        ttl = self._pool_policy.idle_ttl_seconds
        if ttl is None:
            return []
        expired = [
            entry
            for entry in self._sessions.values()
            if entry.leases == 0
            and entry.ready.done()
            and entry.session.is_idle
            and now - entry.last_used_at >= ttl
        ]
        for entry in expired:
            self._sessions.pop(entry.session_id, None)
        return expired

    def _oldest_evictable_locked(self) -> _SessionEntry | None:
        candidates = [
            entry
            for entry in self._sessions.values()
            if entry.leases == 0 and entry.ready.done() and entry.session.is_idle
        ]
        return min(candidates, key=lambda item: item.last_used_at, default=None)

    async def _close_evicted(self, entry: _SessionEntry) -> None:
        await entry.session.close()
        self._emit("runtime_session_evicted", session_id=entry.session_id)

    async def _discard_failed_session(self, entry: _SessionEntry) -> None:
        should_close = False
        async with self._lock:
            if self._sessions.get(entry.session_id) is entry:
                self._sessions.pop(entry.session_id)
                should_close = True
        if should_close:
            try:
                await entry.session.close()
            except BaseException:
                pass

    def _ensure_running(self) -> None:
        if self._state != "running":
            raise RuntimeStateError("agent runtime is not accepting new work")

    def _emit(
        self,
        kind: EventKind,
        *,
        session_id: str | None = None,
        **payload: object,
    ) -> None:
        self._event_sink.emit(
            make_agent_event(
                kind,
                "capability",
                source="runtime",
                payload=payload,
                session_id=session_id,
            )
        )


async def _close_scope(scope: LifecycleScope) -> list[BaseException]:
    try:
        await scope.aclose()
    except RuntimeCloseError as error:
        return list(error.failures)
    except BaseException as error:
        return [error]
    return []
