"""Session lifecycle, durable history, and assembly of individual runs."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from uuid import uuid4

from ..components import AgentComponents, _resolve_components
from ..domain import (
    AgentResult,
    EventProvenance,
    Message,
    SessionEventContext,
    SessionSnapshot,
    SessionState,
)
from ..errors import ConfigurationError, SessionControlError
from ..observability.event_hub import EventFilter, EventHub
from ..observability.events import EventSinkFailure, ensure_isolated_event_sink, make_agent_event
from ..plugins import PluginHost, PluginSessionLease
from ..ports import (
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
from ..run_handle import RunHandle
from ..storage.transitions import find_unresolved_tool_calls
from ..observability._session_events import _SessionEvents, _message_payload, _turn_id
from ..storage._writer import _SessionWriter, _call_session_store
from .compaction_orchestration import _SessionCompactor
from .control import _QueuedSessionRun, _SessionControl
from .loop import Agent, _AgentWiring


_INTERRUPTED_TOOL_RESULT = (
    "Tool execution did not produce a durable result because the previous "
    "agent run was interrupted. The external side-effect outcome is unknown. "
    "Do not assume the call failed or retry it automatically."
)


class AgentSession:
    """Loads one session, runs the agent loop, and persists new messages."""

    def __init__(
        self,
        session_id: str,
        session_store: SessionStore,
        provider: AgentComponents | ModelProvider | None = None,
        *,
        components: AgentComponents | None = None,
        tools: Sequence[Tool] | None = None,
        approval_policy: ApprovalPolicy | None = None,
        execution_gate: ToolExecutionGate | None = None,
        compaction_policy: CompactionPolicy | None = None,
        compaction_strategy: CompactionStrategy | None = None,
        context_strategy: ContextStrategy | None = None,
        event_sink: EventSink | None = None,
        system_prompt: str | None = None,
        max_model_requests: int | None = None,
        plugin_host: PluginHost | None = None,
        next_turn_hook: NextTurnHook | None = None,
        control_claim_lease_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if control_claim_lease_seconds <= 0:
            raise ValueError("control_claim_lease_seconds must be greater than zero")
        self._session_id = session_id
        self._session_store = session_store
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
        self._event_sink = ensure_isolated_event_sink(event_sink)
        self._plugin_host = plugin_host
        self._plugin_lease: PluginSessionLease | None = None
        self._run_lock = asyncio.Lock()
        self._durable_lock = asyncio.Lock()
        self._restore_lock = asyncio.Lock()
        self._restored = False
        self._closed = False
        self._events = _SessionEvents(session_id, self._event_sink)
        self._control = _SessionControl(
            session_id=session_id,
            session_store=session_store,
            durable_lock=self._durable_lock,
            clock=clock,
            control_claim_lease_seconds=control_claim_lease_seconds,
            run=self._run_once,
            notify_run_settled=self._notify_run_settled,
            events=self._events,
        )
        self._compactor = _SessionCompactor(
            session_id, self._components.compaction, self._events
        )

    @classmethod
    def _for_runtime(
        cls, session_id: str, store: SessionStore, components: AgentComponents, *,
        event_sink: EventSink, plugin_host: PluginHost | None,
    ) -> AgentSession:
        session = cls(
            session_id, store, components, event_sink=event_sink, plugin_host=plugin_host
        )
        session._control.observe_dispatch_start = True
        return session

    @property
    def event_sink_failures(self) -> tuple[EventSinkFailure, ...]:
        """Return observer failures that were isolated from session execution."""
        return self._event_sink.failures

    @property
    def state(self) -> SessionState:
        """Return the current explicit control-plane state."""
        return self._control.state

    @property
    def is_idle(self) -> bool:
        """Return whether no run is active and no follow-up is queued."""
        return self._control.is_idle

    @property
    def active_run_id(self) -> str | None:
        return self._control.active_run_id

    @property
    def pending_follow_up_count(self) -> int:
        return self._control.pending_follow_up_count

    @property
    def pending_steer_count(self) -> int:
        return self._control.pending_steer_count

    async def run(self, prompt: str) -> AgentResult:
        """Queue one run and await its own settled result."""
        if not prompt.strip():
            raise ValueError("prompt must contain non-whitespace text")

        await self.restore()
        return await self._control.run(prompt)

    async def follow_up(self, prompt: str) -> str:
        """Queue a new run after earlier work and return its run identity."""
        if not prompt.strip():
            raise ValueError("prompt must contain non-whitespace text")

        await self.restore()
        return await self._control.follow_up(prompt)

    async def _submit_handle(
        self, prompt: str, hub: EventHub, *, follow_up: bool, max_buffer: int
    ) -> RunHandle:
        if not prompt.strip():
            raise ValueError("prompt must contain non-whitespace text")
        await self.restore()
        run_id = uuid4().hex
        events = hub._subscribe(
            EventFilter(session_id=self._session_id, run_id=run_id),
            max_buffer, "drop_newest", until_settled=True,
        )
        try:
            queued = await self._control.submit(
                prompt, delivery="follow_up" if follow_up else "prompt", run_id=run_id
            )
        except BaseException:
            await events.aclose()
            raise
        return RunHandle(
            run_id, events, queued.future, lambda: self._control._cancel_submission(queued)
        )

    async def steer(self, prompt: str) -> None:
        """Insert a user instruction at the active run's next safe boundary."""
        if not prompt.strip():
            raise ValueError("prompt must contain non-whitespace text")

        await self.restore()
        await self._control.steer(prompt)

    async def abort(self, *, clear_queue: bool = True) -> None:
        """Cancel the active run and, by default, all queued follow-ups."""
        await self.restore()
        await self._control.abort(clear_queue=clear_queue)
        if clear_queue:
            await self.wait_until_idle()

    async def wait_until_idle(self) -> None:
        """Wait until no run is active and the follow-up queue is empty."""
        await self.restore()
        await self._control.wait_until_idle()

    async def restore(self) -> None:
        """Restore pending durable controls once and resume eligible follow-ups."""
        if self._closed:
            raise SessionControlError("agent session is closed")
        if self._restored:
            return
        async with self._restore_lock:
            if self._restored:
                return
            async with self._durable_lock:
                snapshot = await _call_session_store(
                    self._session_store.load, self._session_id
                )
            plugin_lease = None
            if self._plugin_host is not None:
                plugin_lease = await self._plugin_host.open_session(
                    self._session_id
                )
            try:
                await self._control.restore_pending_controls(snapshot)
            except BaseException:
                if plugin_lease is not None:
                    await plugin_lease.close()
                raise
            self._plugin_lease = plugin_lease
            self._restored = True
            self._control.resume_pending_runs()

    async def close(self) -> None:
        """Stop work and release all per-session plugin resources once."""
        if self._closed:
            return
        await self.abort()
        self._closed = True
        lease = self._plugin_lease
        self._plugin_lease = None
        if lease is not None:
            await lease.close()

    async def __aenter__(self) -> AgentSession:
        await self.restore()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        del exc_info
        await self.close()

    def _notify_run_settled(self, run_id: str) -> None:
        if self._plugin_lease is not None:
            self._plugin_lease.notify_run_settled(run_id)

    async def _run_once(self, queued: _QueuedSessionRun) -> AgentResult:
        """Execute one queued run against the latest durable snapshot."""
        prompt = queued.prompt
        run_id = queued.run_id
        async with self._run_lock:
            writer = await self._load_recovered_writer(run_id=run_id)

            components = self._components
            system_prompt = components.system_prompt
            if not writer.snapshot.messages and system_prompt is not None:
                await writer.commit(
                    Message(role="system", content=system_prompt),
                    SessionEventContext(
                        run_id=run_id,
                        provenance=EventProvenance(
                            source="runtime",
                            actor_id="system-prompt",
                        ),
                    ),
                )

            await self._compactor.maybe_auto_compact(
                writer,
                pending_messages=(Message(role="user", content=prompt),),
                request_number=1,
                event_context=SessionEventContext(
                    run_id=run_id,
                    turn_id=_turn_id(run_id, 1),
                    provenance=EventProvenance(
                        source="compaction",
                        actor_id="automatic-compaction",
                    ),
                ),
            )

            control_id = queued.control_id
            if control_id is not None:
                if queued.claim_id is None:
                    raise SessionControlError("follow-up control is not claimed")
                await writer.deliver_control(
                    Message(role="user", content=prompt),
                    SessionEventContext(
                        run_id=run_id,
                        turn_id=_turn_id(run_id, 1),
                        provenance=EventProvenance(
                            source="user",
                            actor_id="follow-up",
                        ),
                    ),
                    control_id,
                    queued.claim_id,
                )

            agent = Agent._for_session(
                components,
                event_sink=self._event_sink,
                initial_messages=writer.snapshot.messages,
                wiring=_AgentWiring(
                    message_committer=writer.commit,
                    steering_committer=writer.deliver_control,
                    provider_request_hook=self._plugin_lease,
                    steering_receiver=self._control.receive_steering,
                    dispatch_started=queued.dispatch_started,
                ),
                next_turn_hook=self._compactor.next_turn_hook(
                    writer, components.next_turn_hook
                ),
                session_id=self._session_id,
            )
            queued.entered_agent_loop = True
            result = await agent.run(
                prompt,
                run_id=run_id,
                prompt_already_committed=control_id is not None,
            )

            return AgentResult(
                message=result.message,
                messages=writer.snapshot.messages,
                finish_reason=result.finish_reason,
                usage=result.usage,
            )

    async def compact(
        self,
        *,
        keep_recent_turns: int = 1,
    ) -> SessionSnapshot:
        """Summarize old complete turns and durably append the replacement."""
        if self._closed:
            raise SessionControlError("agent session is closed")
        if self._components.compaction is None:
            raise ConfigurationError(
                "AgentSession.compact requires a compaction_strategy"
            )
        await self._control.begin_compaction()
        try:
            async with self._run_lock:
                return await self._compactor.compact(
                    self._load_recovered_writer,
                    keep_recent_turns=keep_recent_turns,
                )
        finally:
            await self._control.finish_compaction()

    async def _load_recovered_writer(
        self, *, run_id: str | None = None
    ) -> _SessionWriter:
        snapshot = await _call_session_store(
            self._session_store.load, self._session_id
        )
        writer = _SessionWriter(self._session_store, snapshot, self._durable_lock)
        await self._recover_unresolved_tool_calls(writer, run_id=run_id)
        return writer

    async def _recover_unresolved_tool_calls(
        self,
        writer: _SessionWriter,
        *,
        run_id: str | None = None,
    ) -> None:
        """Close an interrupted trailing tool batch before continuing a session."""

        unresolved = find_unresolved_tool_calls(writer.snapshot.messages)
        if not unresolved:
            return

        source_version = writer.snapshot.version
        for call in unresolved:
            recovered = Message(
                role="tool",
                content=_INTERRUPTED_TOOL_RESULT,
                tool_call_id=call.id,
                tool_name=call.name,
                is_error=True,
                structured_content={
                    "status": "interrupted",
                    "outcome": "unknown",
                    "recovered": True,
                },
            )
            recovery_context = SessionEventContext(
                run_id=run_id,
                provenance=EventProvenance(
                    source="recovery",
                    actor_id="tool-call-recovery",
                ),
            )
            durable_identity = await writer.commit(
                recovered,
                recovery_context,
            )
            self._event_sink.emit(
                make_agent_event(
                    "message_committed",
                    "durable",
                    source="agent_session",
                    payload={**_message_payload(recovered), "recovered": True},
                    session_id=self._session_id,
                    run_id=run_id,
                    durable_event_id=durable_identity.event_id,
                )
            )
            self._events.emit(
                "tool_call_recovered",
                event_context=recovery_context,
                durable_event_id=durable_identity.event_id,
                tool_call_id=call.id,
                tool_name=call.name,
                outcome="unknown",
                source_version=source_version,
                recovered_version=writer.snapshot.version,
            )
