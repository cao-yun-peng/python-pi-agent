"""The provider-neutral agent and tool-call loop."""

from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from uuid import uuid4

from .compaction import (
    CompactionPreparation,
    build_summary_message,
    prepare_compaction,
)
from .context import FullHistoryContextStrategy
from .domain import (
    AgentEvent,
    AgentResult,
    ApprovalDecision,
    CompactionDecision,
    ContextPlan,
    EventChannel,
    EventKind,
    EventProvenance,
    Finish,
    Message,
    SessionCompaction,
    SessionEventContext,
    SessionSnapshot,
    SessionState,
    TextDelta,
    ThinkingDelta,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
)
from .errors import (
    AgentError,
    AgentLoopLimitError,
    CompactionError,
    CompactionNotNeededError,
    CompactionPolicyError,
    ConfigurationError,
    ContextPreparationError,
    ModelProtocolError,
    ProviderError,
    SandboxError,
    SandboxViolationError,
    SessionControlError,
    ToolValidationError,
)
from .events import EventSinkFailure, ensure_isolated_event_sink
from .ports import (
    ApprovalPolicy,
    CompactionPolicy,
    CompactionStrategy,
    ContextStrategy,
    EventSink,
    ModelProvider,
    NextTurnHook,
    Sandbox,
    SessionStore,
    Tool,
)
from .session_transitions import find_unresolved_tool_calls
from .tools import (
    DeclaredSideEffectSandbox,
    DenyByDefaultApprovalPolicy,
    validate_tool_arguments,
)


_INTERRUPTED_TOOL_RESULT = (
    "Tool execution did not produce a durable result because the previous "
    "agent run was interrupted. The external side-effect outcome is unknown. "
    "Do not assume the call failed or retry it automatically."
)


@dataclass(frozen=True, slots=True)
class _ModelTurn:
    message: Message
    finish: Finish
    usage: TokenUsage | None


class Agent:
    """A stateful, single-session agent with a serialized run loop."""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        tools: Sequence[Tool] = (),
        approval_policy: ApprovalPolicy | None = None,
        sandbox: Sandbox | None = None,
        context_strategy: ContextStrategy | None = None,
        event_sink: EventSink | None = None,
        initial_messages: Sequence[Message] = (),
        message_committer: (
            Callable[[Message, SessionEventContext], Awaitable[None]] | None
        ) = None,
        next_turn_hook: NextTurnHook | None = None,
        steering_receiver: (
            Callable[[bool], Awaitable[tuple[str, ...]]] | None
        ) = None,
        session_id: str | None = None,
        system_prompt: str | None = None,
        max_model_requests: int = 8,
    ) -> None:
        if max_model_requests <= 0:
            raise ValueError("max_model_requests must be greater than zero")
        definitions = [tool.definition for tool in tools]
        duplicate_names = _duplicates(definition.name for definition in definitions)
        if duplicate_names:
            names = ", ".join(sorted(duplicate_names))
            raise ValueError(f"duplicate tool names: {names}")
        if initial_messages and system_prompt is not None:
            raise ValueError(
                "initial_messages and system_prompt cannot be supplied together"
            )
        if session_id is not None and not session_id.strip():
            raise ValueError("session_id must not be empty")

        self._provider = provider
        self._tools = {tool.definition.name: tool for tool in tools}
        self._tool_definitions = tuple(definitions)
        self._approval_policy = (
            approval_policy
            if approval_policy is not None
            else DenyByDefaultApprovalPolicy()
        )
        self._sandbox = (
            sandbox if sandbox is not None else DeclaredSideEffectSandbox()
        )
        self._context_strategy = context_strategy or FullHistoryContextStrategy()
        self._event_sink = ensure_isolated_event_sink(event_sink)
        self._max_model_requests = max_model_requests
        self._message_committer = message_committer
        self._next_turn_hook = next_turn_hook
        self._steering_receiver = steering_receiver
        self._session_id = session_id
        self._active_run_id: str | None = None
        self._active_turn_id: str | None = None
        self._messages = list(initial_messages)
        if system_prompt is not None:
            self._messages.append(Message(role="system", content=system_prompt))
        self._run_lock = asyncio.Lock()

    @property
    def messages(self) -> tuple[Message, ...]:
        """Return an immutable snapshot of committed model-visible history."""
        return tuple(self._messages)

    @property
    def event_sink_failures(self) -> tuple[EventSinkFailure, ...]:
        """Return observer failures that were isolated from agent execution."""
        return self._event_sink.failures

    async def run(
        self,
        prompt: str,
        *,
        run_id: str | None = None,
    ) -> AgentResult:
        """Run model/tool turns until a final assistant message is committed."""
        if not prompt.strip():
            raise ValueError("prompt must contain non-whitespace text")
        if run_id is not None and not run_id.strip():
            raise ValueError("run_id must not be empty")

        async with self._run_lock:
            self._active_run_id = run_id or uuid4().hex
            self._active_turn_id = _turn_id(self._active_run_id, 1)
            self._emit("run_started", "live")
            total_usage: TokenUsage | None = None

            try:
                await self._commit(
                    Message(role="user", content=prompt),
                    SessionEventContext(
                        run_id=self._active_run_id,
                        turn_id=self._active_turn_id,
                        provenance=EventProvenance(
                            source="user",
                            actor_id="user",
                        ),
                    ),
                )
                for request_number in range(1, self._max_model_requests + 1):
                    self._active_turn_id = _turn_id(
                        self._active_run_id,
                        request_number,
                    )
                    turn = await self._request_model(request_number)
                    total_usage = _merge_usage(total_usage, turn.usage)
                    await self._commit(
                        turn.message,
                        SessionEventContext(
                            run_id=self._active_run_id,
                            turn_id=self._active_turn_id,
                            provenance=EventProvenance(
                                source="model",
                                actor_id=type(self._provider).__name__,
                            ),
                        ),
                    )

                    has_tool_calls = bool(turn.message.tool_calls)
                    if has_tool_calls:
                        if turn.finish.reason == "length":
                            await self._fail_truncated_tool_calls(
                                turn.message.tool_calls
                            )
                        else:
                            await self._execute_tool_calls(
                                turn.message.tool_calls,
                                request_number=request_number,
                            )
                    steering = await self._receive_steering(
                        close_if_empty=not has_tool_calls
                    )
                    if steering:
                        next_turn_id = _turn_id(
                            self._active_run_id,
                            request_number + 1,
                        )
                        for instruction in steering:
                            await self._commit(
                                Message(role="user", content=instruction),
                                SessionEventContext(
                                    run_id=self._active_run_id,
                                    turn_id=next_turn_id,
                                    provenance=EventProvenance(
                                        source="user",
                                        actor_id="steer",
                                    ),
                                ),
                            )

                    if has_tool_calls or steering:
                        if request_number < self._max_model_requests:
                            await self._prepare_next_turn(request_number + 1)
                            continue
                        raise AgentLoopLimitError(
                            f"run exceeded {self._max_model_requests} model requests"
                        )

                    self._emit(
                        "run_completed",
                        "live",
                        finish_reason=turn.finish.reason,
                        model_requests=request_number,
                    )
                    return AgentResult(
                        message=turn.message,
                        messages=self.messages,
                        finish_reason=turn.finish.reason,
                        usage=total_usage,
                    )

                raise AgentLoopLimitError(
                    f"run exceeded {self._max_model_requests} model requests"
                )
            except asyncio.CancelledError:
                self._emit("run_cancelled", "live")
                raise
            except AgentError as error:
                self._emit("run_failed", "live", error=type(error).__name__)
                raise
            except Exception as error:
                wrapped = ProviderError("model provider failed")
                self._emit("run_failed", "live", error=type(wrapped).__name__)
                raise wrapped from error
            finally:
                self._emit("run_settled", "live")
                self._active_run_id = None
                self._active_turn_id = None

    async def _receive_steering(
        self,
        *,
        close_if_empty: bool,
    ) -> tuple[str, ...]:
        receiver = self._steering_receiver
        if receiver is None:
            return ()
        return await receiver(close_if_empty)

    async def _request_model(self, request_number: int) -> _ModelTurn:
        context = await self._prepare_context(request_number)
        self._emit(
            "model_request_started",
            "live",
            request_number=request_number,
        )
        partial_text: list[str] = []
        partial_thinking: list[str] = []
        tool_calls: list[ToolCall] = []
        tool_call_ids: set[str] = set()
        usage: TokenUsage | None = None
        finish: Finish | None = None

        async for event in self._provider.stream(
            context.messages,
            self._tool_definitions,
        ):
            if isinstance(event, ThinkingDelta):
                _require_unfinished(finish, "thinking")
                partial_thinking.append(event.text)
                self._emit("thinking_delta", "live", text=event.text)
            elif isinstance(event, TextDelta):
                _require_unfinished(finish, "text")
                partial_text.append(event.text)
                self._emit("text_delta", "live", text=event.text)
            elif isinstance(event, ToolCall):
                _require_unfinished(finish, "tool call")
                if event.id in tool_call_ids:
                    raise ModelProtocolError(f"duplicate tool call id: {event.id}")
                tool_call_ids.add(event.id)
                tool_calls.append(event)
                self._emit(
                    "tool_call_received",
                    "capability",
                    tool_call_id=event.id,
                    tool_name=event.name,
                    arguments=event.arguments,
                )
            elif isinstance(event, TokenUsage):
                usage = event
                self._emit(
                    "model_usage",
                    "live",
                    input_tokens=event.input_tokens,
                    output_tokens=event.output_tokens,
                    total_tokens=event.total_tokens,
                    cached_input_tokens=event.cached_input_tokens,
                )
            elif isinstance(event, Finish):
                if finish is not None:
                    raise ModelProtocolError("provider emitted more than one finish")
                finish = event
            else:
                raise ModelProtocolError(
                    f"unsupported model event: {type(event).__name__}"
                )

        if finish is None:
            raise ModelProtocolError("provider stream ended without finish")
        if tool_calls and finish.reason not in {"tool_calls", "length"}:
            raise ModelProtocolError(
                "provider emitted tool calls without tool_calls finish reason"
            )
        if not tool_calls and finish.reason == "tool_calls":
            raise ModelProtocolError(
                "provider emitted tool_calls finish reason without tool calls"
            )

        return _ModelTurn(
            message=Message(
                role="assistant",
                content="".join(partial_text),
                thinking="".join(partial_thinking),
                tool_calls=tuple(tool_calls),
            ),
            finish=finish,
            usage=usage,
        )

    async def _prepare_context(self, request_number: int) -> ContextPlan:
        history = self.messages
        try:
            context = await self._context_strategy.prepare(history)
        except asyncio.CancelledError:
            raise
        except ContextPreparationError:
            raise
        except Exception as error:
            raise ContextPreparationError("context strategy failed") from error

        if not isinstance(context, ContextPlan):
            raise ContextPreparationError(
                "context strategy must return ContextPlan"
            )
        if context.source_message_count != len(history):
            raise ContextPreparationError(
                "context plan source_message_count does not match history"
            )

        self._emit(
            "context_prepared",
            "capability",
            request_number=request_number,
            source_message_count=context.source_message_count,
            selected_message_count=len(context.messages),
            omitted_message_count=context.omitted_message_count,
            token_budget=context.token_budget,
            estimated_tokens=context.estimated_tokens,
        )
        return context

    async def _prepare_next_turn(self, next_request_number: int) -> None:
        """Let an interceptor refresh canonical history at a safe boundary."""
        hook = self._next_turn_hook
        if hook is None:
            return

        try:
            prepared = tuple(
                await hook.prepare(
                    self.messages,
                    next_request_number=next_request_number,
                )
            )
        except asyncio.CancelledError:
            raise
        except AgentError:
            raise
        except Exception as error:
            raise ContextPreparationError("next-turn hook failed") from error

        if any(not isinstance(message, Message) for message in prepared):
            raise ContextPreparationError(
                "next-turn hook must return only Message values"
            )
        self._messages = list(prepared)

    async def _execute_tool_calls(
        self,
        calls: Sequence[ToolCall],
        *,
        request_number: int,
    ) -> None:
        for call_index, call in enumerate(calls, start=1):
            tool = self._tools.get(call.name)
            if tool is None:
                self._emit(
                    "tool_resolution_failed",
                    "capability",
                    tool_call_id=call.id,
                    tool_name=call.name,
                )
                await self._commit_tool_result(
                    call,
                    ToolOutput(
                        content=f'Tool "{call.name}" is not available.',
                        is_error=True,
                    ),
                )
                continue

            try:
                validate_tool_arguments(tool.definition.input_schema, call.arguments)
            except ToolValidationError as error:
                self._emit(
                    "tool_validation_failed",
                    "capability",
                    tool_call_id=call.id,
                    tool_name=call.name,
                    violations=error.violations,
                )
                await self._commit_tool_result(
                    call,
                    ToolOutput(
                        content=f"Tool arguments are invalid: {error}",
                        is_error=True,
                    ),
                )
                continue

            context = _tool_execution_context(
                call,
                tool.definition,
                request_number=request_number,
                call_index=call_index,
                session_id=self._session_id,
            )
            if tool.definition.requires_approval:
                decision = await self._review_tool_call(context)
                if not decision.approved:
                    await self._commit_tool_result(
                        call,
                        ToolOutput(
                            content="Tool call was denied by the approval policy.",
                            is_error=True,
                        ),
                    )
                    continue
                context = _tool_execution_context(
                    call,
                    tool.definition,
                    request_number=request_number,
                    call_index=call_index,
                    session_id=self._session_id,
                    approval=decision,
                )

            output = await self._run_tool(tool, context)
            await self._commit_tool_result(call, output)

    async def _review_tool_call(
        self,
        context: ToolExecutionContext,
    ) -> ApprovalDecision:
        call = context.call
        definition = context.definition
        self._emit(
            "tool_approval_requested",
            "capability",
            tool_call_id=call.id,
            tool_name=call.name,
            side_effects=definition.side_effects,
            request_number=context.request_number,
            call_index=context.call_index,
            session_id=context.session_id,
        )
        try:
            decision = await self._approval_policy.review(context)
            if not isinstance(decision, ApprovalDecision):
                raise TypeError("approval policy did not return ApprovalDecision")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._emit(
                "tool_approval_decided",
                "capability",
                tool_call_id=call.id,
                tool_name=call.name,
                approved=False,
                policy_error=type(error).__name__,
                request_number=context.request_number,
                call_index=context.call_index,
                session_id=context.session_id,
            )
            return ApprovalDecision(
                approved=False,
                reason="Approval policy failed closed.",
            )

        self._emit(
            "tool_approval_decided",
            "capability",
            tool_call_id=call.id,
            tool_name=call.name,
            approved=decision.approved,
            reason=decision.reason,
            request_number=context.request_number,
            call_index=context.call_index,
            session_id=context.session_id,
        )
        return decision

    async def _run_tool(
        self,
        tool: Tool,
        context: ToolExecutionContext,
    ) -> ToolOutput:
        call = context.call
        self._emit(
            "tool_execution_started",
            "capability",
            tool_call_id=call.id,
            tool_name=call.name,
            request_number=context.request_number,
            call_index=context.call_index,
            session_id=context.session_id,
        )
        outcome = "success"
        try:
            timeout = tool.definition.timeout_seconds
            if timeout is None:
                output = await self._sandbox.execute(
                    tool,
                    call.arguments,
                    context,
                )
            else:
                async with asyncio.timeout(timeout):
                    output = await self._sandbox.execute(
                        tool,
                        call.arguments,
                        context,
                    )
            if not isinstance(output, ToolOutput):
                raise TypeError("tool did not return ToolOutput")
            if output.is_error:
                outcome = "error"
        except asyncio.CancelledError:
            self._emit(
                "tool_execution_finished",
                "capability",
                tool_call_id=call.id,
                tool_name=call.name,
                outcome="cancelled",
                request_number=context.request_number,
                call_index=context.call_index,
                session_id=context.session_id,
            )
            raise
        except TimeoutError:
            outcome = "timeout"
            output = ToolOutput(
                content="Tool execution exceeded its timeout.",
                is_error=True,
            )
        except SandboxViolationError as error:
            outcome = "sandbox_denied"
            self._emit(
                "tool_sandbox_denied",
                "capability",
                tool_call_id=call.id,
                tool_name=call.name,
                side_effects=context.definition.side_effects,
                sandbox=type(self._sandbox).__name__,
                error=type(error).__name__,
                request_number=context.request_number,
                call_index=context.call_index,
                session_id=context.session_id,
            )
            output = ToolOutput(
                content="Tool execution was blocked by the sandbox.",
                is_error=True,
            )
        except SandboxError as error:
            outcome = "sandbox_error"
            output = ToolOutput(
                content=f"Sandbox execution failed ({type(error).__name__}).",
                is_error=True,
            )
        except Exception as error:
            outcome = "error"
            output = ToolOutput(
                content=f"Tool execution failed ({type(error).__name__}).",
                is_error=True,
            )

        self._emit(
            "tool_execution_finished",
            "capability",
            tool_call_id=call.id,
            tool_name=call.name,
            outcome=outcome,
            request_number=context.request_number,
            call_index=context.call_index,
            session_id=context.session_id,
        )
        return output

    async def _fail_truncated_tool_calls(
        self,
        calls: Sequence[ToolCall],
    ) -> None:
        for call in calls:
            await self._commit_tool_result(
                call,
                ToolOutput(
                    content=(
                        "Tool call was not executed because the model response "
                        "reached its output limit."
                    ),
                    is_error=True,
                ),
            )

    async def _commit_tool_result(
        self,
        call: ToolCall,
        output: ToolOutput,
    ) -> None:
        await self._commit(
            Message(
                role="tool",
                content=output.content,
                tool_call_id=call.id,
                tool_name=call.name,
                is_error=output.is_error,
                structured_content=output.structured_content,
            ),
            SessionEventContext(
                run_id=self._active_run_id,
                turn_id=self._active_turn_id,
                provenance=EventProvenance(
                    source="tool",
                    actor_id=call.name,
                ),
            ),
        )

    async def _commit(
        self,
        message: Message,
        event_context: SessionEventContext | None = None,
    ) -> None:
        context = event_context or SessionEventContext(
            run_id=self._active_run_id,
            turn_id=self._active_turn_id,
        )
        if self._message_committer is not None:
            await self._message_committer(message, context)

        self._messages.append(message)
        self._emit(
            "message_committed",
            "durable",
            provenance_source=context.provenance.source,
            **_message_payload(message),
        )

    def _emit(
        self,
        kind: EventKind,
        channel: EventChannel,
        **payload: object,
    ) -> None:
        correlated_payload = dict(payload)
        if self._session_id is not None:
            correlated_payload.setdefault("session_id", self._session_id)
        if self._active_run_id is not None:
            correlated_payload.setdefault("run_id", self._active_run_id)
        if self._active_turn_id is not None:
            correlated_payload.setdefault("turn_id", self._active_turn_id)
        self._event_sink.emit(
            AgentEvent(
                kind=kind,
                channel=channel,
                payload=correlated_payload,
            )
        )


class _SessionWriter:
    """Append one message at a time while carrying the latest version."""

    def __init__(
        self,
        session_store: SessionStore,
        snapshot: SessionSnapshot,
    ) -> None:
        self._session_store = session_store
        self._snapshot = snapshot

    @property
    def snapshot(self) -> SessionSnapshot:
        return self._snapshot

    async def commit(
        self,
        message: Message,
        event_context: SessionEventContext,
    ) -> None:
        self._snapshot = await self._session_store.append(
            self._snapshot.session_id,
            self._snapshot.version,
            (message,),
            event_context=event_context,
        )

    async def compact(
        self,
        compaction: SessionCompaction,
        event_context: SessionEventContext,
    ) -> SessionSnapshot:
        self._snapshot = await self._session_store.compact(
            self._snapshot.session_id,
            self._snapshot.version,
            compaction,
            event_context=event_context,
        )
        return self._snapshot


class _SessionNextTurnHook:
    """Adapts session compaction to the Agent's generic next-turn hook."""

    def __init__(
        self,
        writer: _SessionWriter,
        prepare: Callable[..., Awaitable[None]],
        run_id: str,
    ) -> None:
        self._writer = writer
        self._prepare = prepare
        self._run_id = run_id

    async def prepare(
        self,
        messages: Sequence[Message],
        *,
        next_request_number: int,
    ) -> Sequence[Message]:
        if tuple(messages) != self._writer.snapshot.messages:
            raise CompactionError(
                "agent and session histories diverged before next-turn preparation"
            )
        await self._prepare(
            self._writer,
            event_context=SessionEventContext(
                run_id=self._run_id,
                turn_id=_turn_id(self._run_id, next_request_number),
                provenance=EventProvenance(
                    source="compaction",
                    actor_id="automatic-compaction",
                ),
            ),
        )
        return self._writer.snapshot.messages


@dataclass(slots=True)
class _QueuedSessionRun:
    run_id: str
    prompt: str
    delivery: str
    future: asyncio.Future[AgentResult]


class AgentSession:
    """Loads one session, runs the agent loop, and persists new messages."""

    def __init__(
        self,
        session_id: str,
        session_store: SessionStore,
        provider: ModelProvider,
        *,
        tools: Sequence[Tool] = (),
        approval_policy: ApprovalPolicy | None = None,
        sandbox: Sandbox | None = None,
        compaction_policy: CompactionPolicy | None = None,
        compaction_strategy: CompactionStrategy | None = None,
        context_strategy: ContextStrategy | None = None,
        event_sink: EventSink | None = None,
        system_prompt: str | None = None,
        max_model_requests: int = 8,
    ) -> None:
        if compaction_policy is not None and compaction_strategy is None:
            raise ConfigurationError(
                "compaction_policy requires a compaction_strategy"
            )
        self._session_id = session_id
        self._session_store = session_store
        self._provider = provider
        self._tools = tuple(tools)
        self._approval_policy = approval_policy
        self._sandbox = sandbox
        self._compaction_policy = compaction_policy
        self._compaction_strategy = compaction_strategy
        self._context_strategy = context_strategy
        self._event_sink = ensure_isolated_event_sink(event_sink)
        self._system_prompt = system_prompt
        self._max_model_requests = max_model_requests
        self._run_lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()
        self._queued_runs: deque[_QueuedSessionRun] = deque()
        self._steering: deque[str] = deque()
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._active_task: asyncio.Task[AgentResult] | None = None
        self._active_compaction_task: asyncio.Task[SessionSnapshot] | None = None
        self._active_run: _QueuedSessionRun | None = None
        self._state: SessionState = "idle"
        self._accepting_steer = False
        self._idle_event = asyncio.Event()
        self._idle_event.set()

    @property
    def event_sink_failures(self) -> tuple[EventSinkFailure, ...]:
        """Return observer failures that were isolated from session execution."""
        return self._event_sink.failures

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
        if not prompt.strip():
            raise ValueError("prompt must contain non-whitespace text")

        queued = await self._enqueue_run(prompt, delivery="prompt")
        try:
            return await asyncio.shield(queued.future)
        except asyncio.CancelledError:
            await self._cancel_submission(queued)
            raise

    async def follow_up(self, prompt: str) -> str:
        """Queue a new run after earlier work and return its run identity."""
        if not prompt.strip():
            raise ValueError("prompt must contain non-whitespace text")

        queued = await self._enqueue_run(prompt, delivery="follow_up")
        queued.future.add_done_callback(_consume_background_result)
        return queued.run_id

    async def steer(self, prompt: str) -> None:
        """Insert a user instruction at the active run's next safe boundary."""
        if not prompt.strip():
            raise ValueError("prompt must contain non-whitespace text")

        async with self._control_lock:
            if self._active_run is None or not self._accepting_steer:
                raise SessionControlError(
                    "steer requires an active run that still accepts input"
                )
            self._steering.append(prompt)

    async def abort(self, *, clear_queue: bool = True) -> None:
        """Cancel the active run and, by default, all queued follow-ups."""
        active_tasks: tuple[asyncio.Task[object], ...]
        async with self._control_lock:
            if clear_queue:
                while self._queued_runs:
                    queued = self._queued_runs.popleft()
                    queued.future.cancel()
            self._steering.clear()
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
        if clear_queue:
            await self.wait_until_idle()

    async def wait_until_idle(self) -> None:
        """Wait until no run is active and the follow-up queue is empty."""
        await self._idle_event.wait()

    async def _enqueue_run(
        self,
        prompt: str,
        *,
        delivery: str,
    ) -> _QueuedSessionRun:
        loop = asyncio.get_running_loop()
        queued = _QueuedSessionRun(
            run_id=uuid4().hex,
            prompt=prompt,
            delivery=delivery,
            future=loop.create_future(),
        )
        async with self._control_lock:
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
                self._active_run = queued
                self._accepting_steer = True
                self._state = "running"
                self._active_task = asyncio.create_task(
                    self._run_once(queued.prompt, queued.run_id),
                    name=f"agent-run:{queued.run_id}",
                )
                active_task = self._active_task

            try:
                result = await active_task
            except asyncio.CancelledError:
                if not queued.future.done():
                    queued.future.cancel()
            except BaseException as error:
                if not queued.future.done():
                    queued.future.set_exception(error)
            else:
                if not queued.future.done():
                    queued.future.set_result(result)
            finally:
                async with self._control_lock:
                    if self._active_run is queued:
                        self._active_run = None
                        self._active_task = None
                        self._accepting_steer = False
                        self._steering.clear()
                        self._state = "settled"
                await asyncio.sleep(0)

    def _start_dispatcher_locked(self) -> None:
        self._dispatcher_task = asyncio.create_task(
            self._dispatch_runs(),
            name=f"agent-session:{self._session_id}",
        )

    async def _cancel_submission(self, queued: _QueuedSessionRun) -> None:
        active_task: asyncio.Task[AgentResult] | None = None
        async with self._control_lock:
            if queued in self._queued_runs:
                self._queued_runs.remove(queued)
                queued.future.cancel()
                if self._active_run is None and not self._queued_runs:
                    self._state = "idle"
                    self._idle_event.set()
                return
            if self._active_run is queued:
                self._steering.clear()
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

    async def _receive_steering(
        self,
        close_if_empty: bool,
    ) -> tuple[str, ...]:
        async with self._control_lock:
            steering = tuple(self._steering)
            self._steering.clear()
            if close_if_empty and not steering:
                self._accepting_steer = False
            return steering

    async def _run_once(self, prompt: str, run_id: str) -> AgentResult:
        """Execute one queued run against the latest durable snapshot."""
        async with self._run_lock:
            snapshot = await self._session_store.load(self._session_id)
            writer = _SessionWriter(self._session_store, snapshot)
            await self._recover_unresolved_tool_calls(writer, run_id=run_id)

            if not writer.snapshot.messages and self._system_prompt is not None:
                await writer.commit(
                    Message(role="system", content=self._system_prompt),
                    SessionEventContext(
                        run_id=run_id,
                        provenance=EventProvenance(
                            source="runtime",
                            actor_id="system-prompt",
                        ),
                    ),
                )

            await self._maybe_auto_compact(
                writer,
                pending_messages=(Message(role="user", content=prompt),),
                event_context=SessionEventContext(
                    run_id=run_id,
                    turn_id=_turn_id(run_id, 1),
                    provenance=EventProvenance(
                        source="compaction",
                        actor_id="automatic-compaction",
                    ),
                ),
            )

            agent = Agent(
                self._provider,
                tools=self._tools,
                approval_policy=self._approval_policy,
                sandbox=self._sandbox,
                context_strategy=self._context_strategy,
                event_sink=self._event_sink,
                initial_messages=writer.snapshot.messages,
                message_committer=writer.commit,
                next_turn_hook=(
                    _SessionNextTurnHook(
                        writer,
                        self._maybe_auto_compact,
                        run_id,
                    )
                    if self._compaction_policy is not None
                    else None
                ),
                steering_receiver=self._receive_steering,
                session_id=self._session_id,
                max_model_requests=self._max_model_requests,
            )
            result = await agent.run(prompt, run_id=run_id)

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
        if self._compaction_strategy is None:
            raise ConfigurationError(
                "AgentSession.compact requires a compaction_strategy"
            )

        await self._begin_compaction()
        try:
            return await self._compact_once(
                keep_recent_turns=keep_recent_turns
            )
        finally:
            await self._finish_compaction()

    async def _compact_once(
        self,
        *,
        keep_recent_turns: int,
    ) -> SessionSnapshot:
        async with self._run_lock:
            try:
                snapshot = await self._session_store.load(self._session_id)
                writer = _SessionWriter(self._session_store, snapshot)
                await self._recover_unresolved_tool_calls(writer)
                preparation = prepare_compaction(
                    writer.snapshot.messages,
                    keep_recent_turns=keep_recent_turns,
                )
            except asyncio.CancelledError:
                raise
            except AgentError as error:
                self._emit(
                    "compaction_failed",
                    error=type(error).__name__,
                    automatic=False,
                )
                raise
            except Exception as error:
                wrapped = CompactionError("compaction preparation failed")
                self._emit(
                    "compaction_failed",
                    error=type(wrapped).__name__,
                    automatic=False,
                )
                raise wrapped from error

            return await self._execute_compaction(
                writer,
                preparation,
                automatic=False,
                event_context=SessionEventContext(
                    provenance=EventProvenance(
                        source="compaction",
                        actor_id="manual-compaction",
                    )
                ),
            )

    async def _begin_compaction(self) -> None:
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

    async def _finish_compaction(self) -> None:
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
            await writer.commit(
                recovered,
                SessionEventContext(
                    run_id=run_id,
                    provenance=EventProvenance(
                        source="recovery",
                        actor_id="tool-call-recovery",
                    ),
                ),
            )
            self._event_sink.emit(
                AgentEvent(
                    kind="message_committed",
                    channel="durable",
                    payload={**_message_payload(recovered), "recovered": True},
                )
            )
            self._emit(
                "tool_call_recovered",
                tool_call_id=call.id,
                tool_name=call.name,
                outcome="unknown",
                source_version=source_version,
                recovered_version=writer.snapshot.version,
            )

    async def _maybe_auto_compact(
        self,
        writer: _SessionWriter,
        pending_messages: Sequence[Message] = (),
        *,
        event_context: SessionEventContext | None = None,
    ) -> None:
        if self._compaction_policy is None:
            return

        candidate_messages = (
            *writer.snapshot.messages,
            *pending_messages,
        )
        try:
            decision = self._compaction_policy.evaluate(candidate_messages)
            if not isinstance(decision, CompactionDecision):
                raise CompactionPolicyError(
                    "compaction policy must return CompactionDecision"
                )
        except CompactionError as error:
            self._emit(
                "compaction_failed",
                error=type(error).__name__,
                automatic=True,
                stage="policy",
            )
            raise
        except Exception as error:
            wrapped = CompactionPolicyError("compaction policy failed")
            self._emit(
                "compaction_failed",
                error=type(wrapped).__name__,
                automatic=True,
                stage="policy",
            )
            raise wrapped from error

        self._emit(
            "compaction_evaluated",
            automatic=True,
            should_compact=decision.should_compact,
            estimated_tokens=decision.estimated_tokens,
            token_threshold=decision.token_threshold,
            keep_recent_turns=decision.keep_recent_turns,
            candidate_message_count=len(candidate_messages),
        )
        if not decision.should_compact:
            return

        try:
            preparation = prepare_compaction(
                writer.snapshot.messages,
                keep_recent_turns=decision.keep_recent_turns,
            )
        except CompactionNotNeededError:
            self._emit(
                "compaction_skipped",
                automatic=True,
                reason="no_compactable_history",
                source_version=writer.snapshot.version,
            )
            return
        except AgentError as error:
            self._emit(
                "compaction_failed",
                error=type(error).__name__,
                automatic=True,
                stage="preparation",
            )
            raise
        except Exception as error:
            wrapped = CompactionError("compaction preparation failed")
            self._emit(
                "compaction_failed",
                error=type(wrapped).__name__,
                automatic=True,
                stage="preparation",
            )
            raise wrapped from error

        await self._execute_compaction(
            writer,
            preparation,
            automatic=True,
            event_context=event_context
            or SessionEventContext(
                provenance=EventProvenance(
                    source="compaction",
                    actor_id="automatic-compaction",
                )
            ),
        )

    async def _execute_compaction(
        self,
        writer: _SessionWriter,
        preparation: CompactionPreparation,
        *,
        automatic: bool,
        event_context: SessionEventContext,
    ) -> SessionSnapshot:
        strategy = self._compaction_strategy
        if strategy is None:
            raise ConfigurationError("compaction strategy is not configured")

        source_version = writer.snapshot.version
        self._emit(
            "compaction_started",
            automatic=automatic,
            source_version=source_version,
            source_message_count=preparation.source_message_count,
            summarized_message_count=len(preparation.messages_to_summarize),
            kept_message_count=len(preparation.kept_messages),
        )
        try:
            summary = await strategy.summarize(
                preparation.messages_to_summarize
            )
            if not isinstance(summary, str):
                raise CompactionError(
                    "compaction strategy must return summary text"
                )

            compacted = await writer.compact(
                SessionCompaction(
                    replace_start=preparation.replace_start,
                    replaced_message_count=len(
                        preparation.messages_to_summarize
                    ),
                    summary_message=build_summary_message(summary),
                ),
                event_context,
            )
        except asyncio.CancelledError:
            self._emit("compaction_cancelled", automatic=automatic)
            raise
        except AgentError as error:
            self._emit(
                "compaction_failed",
                error=type(error).__name__,
                automatic=automatic,
                stage="execution",
            )
            raise
        except Exception as error:
            wrapped = CompactionError("compaction strategy failed")
            self._emit(
                "compaction_failed",
                error=type(wrapped).__name__,
                automatic=automatic,
                stage="execution",
            )
            raise wrapped from error

        self._emit(
            "compaction_completed",
            automatic=automatic,
            source_version=source_version,
            compacted_version=compacted.version,
            compacted_message_count=len(compacted.messages),
        )
        return compacted

    def _emit(self, kind: EventKind, **payload: object) -> None:
        self._event_sink.emit(
            AgentEvent(kind=kind, channel="capability", payload=payload)
        )


def _require_unfinished(finish: Finish | None, event_name: str) -> None:
    if finish is not None:
        raise ModelProtocolError(f"{event_name} arrived after finish")


def _turn_id(run_id: str, request_number: int) -> str:
    return f"{run_id}:turn:{request_number}"


def _tool_execution_context(
    call: ToolCall,
    definition: ToolDefinition,
    *,
    request_number: int,
    call_index: int,
    session_id: str | None,
    approval: ApprovalDecision | None = None,
) -> ToolExecutionContext:
    """Snapshot mutable JSON so policy and tool code cannot alter the transcript."""
    return ToolExecutionContext(
        call=deepcopy(call),
        definition=deepcopy(definition),
        request_number=request_number,
        call_index=call_index,
        session_id=session_id,
        approval=approval,
    )


def _message_payload(message: Message) -> dict[str, object]:
    return {
        "role": message.role,
        "content": message.content,
        "thinking": message.thinking,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in message.tool_calls
        ],
        "tool_call_id": message.tool_call_id,
        "tool_name": message.tool_name,
        "is_error": message.is_error,
        "structured_content": message.structured_content,
    }


def _merge_usage(
    current: TokenUsage | None,
    incoming: TokenUsage | None,
) -> TokenUsage | None:
    if incoming is None:
        return current
    if current is None:
        return incoming
    cached_tokens: int | None = None
    if current.cached_input_tokens is not None or incoming.cached_input_tokens is not None:
        cached_tokens = (current.cached_input_tokens or 0) + (
            incoming.cached_input_tokens or 0
        )
    return TokenUsage(
        input_tokens=current.input_tokens + incoming.input_tokens,
        output_tokens=current.output_tokens + incoming.output_tokens,
        total_tokens=current.total_tokens + incoming.total_tokens,
        cached_input_tokens=cached_tokens,
    )


def _duplicates(names: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for name in names:
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    return duplicates


def _consume_background_result(future: asyncio.Future[AgentResult]) -> None:
    """Retrieve unobserved follow-up failures so asyncio does not warn."""
    if future.cancelled():
        return
    future.exception()
