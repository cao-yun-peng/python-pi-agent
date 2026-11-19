"""Provider-neutral model and tool loop for one Agent."""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from uuid import uuid4
from warnings import warn

from .components import AgentComponents, _resolve_components
from ..domain import (
    AgentResult,
    ApprovalDecision,
    ContextPlan,
    DurableEventIdentity,
    EventChannel,
    EventKind,
    EventProvenance,
    Finish,
    Message,
    ModelStreamEvent,
    NextTurnContext,
    ProviderRequestContext,
    ProviderState,
    SessionEventContext,
    TextDelta,
    ThinkingDelta,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
)
from ..errors import (
    AgentError,
    AgentLoopLimitError,
    ContextPreparationError,
    InternalError,
    ModelProtocolError,
    ProviderError,
    SandboxError,
    SandboxTimeoutError,
    ToolExecutionDeniedError,
    ToolValidationError,
)
from ..observability.events import EventSinkFailure, ensure_isolated_event_sink, make_agent_event
from ..ports import (
    ApprovalPolicy,
    ContextStrategy,
    EventSink,
    ModelProvider,
    NextTurnHook,
    ProviderRequestHook,
    Tool,
    ToolExecutionGate,
)
from ..tools.validation import validate_tool_arguments
from ..observability._session_events import _duration_ms, _message_payload, _run_error_fields, _turn_id


@dataclass(frozen=True, slots=True)
class _ModelTurn:
    message: Message
    finish: Finish
    usage: TokenUsage | None


@dataclass(frozen=True, slots=True)
class _SteeringInstruction:
    prompt: str
    control_id: str
    claim_id: str


_MessageCommitter = Callable[[Message, SessionEventContext], Awaitable[DurableEventIdentity]]
_SteeringCommitter = Callable[
    [Message, SessionEventContext, str, str], Awaitable[DurableEventIdentity]
]
_SteeringReceiver = Callable[[bool], Awaitable[tuple[_SteeringInstruction, ...]]]


@dataclass(frozen=True, slots=True)
class _AgentWiring:
    message_committer: _MessageCommitter | None = None
    steering_committer: _SteeringCommitter | None = None
    provider_request_hook: ProviderRequestHook | None = None
    steering_receiver: _SteeringReceiver | None = None
    dispatch_started: bool = False


class Agent:
    """A stateful, single-session agent with a serialized run loop."""

    def __init__(
        self,
        provider: AgentComponents | ModelProvider | None = None,
        *,
        components: AgentComponents | None = None,
        tools: Sequence[Tool] | None = None,
        approval_policy: ApprovalPolicy | None = None,
        execution_gate: ToolExecutionGate | None = None,
        context_strategy: ContextStrategy | None = None,
        event_sink: EventSink | None = None,
        initial_messages: Sequence[Message] = (),
        message_committer: _MessageCommitter | None = None,
        steering_committer: _SteeringCommitter | None = None,
        next_turn_hook: NextTurnHook | None = None,
        provider_request_hook: ProviderRequestHook | None = None,
        steering_receiver: _SteeringReceiver | None = None,
        session_id: str | None = None,
        system_prompt: str | None = None,
        max_model_requests: int | None = None,
    ) -> None:
        if initial_messages and system_prompt is not None:
            raise ValueError(
                "initial_messages and system_prompt cannot be supplied together"
            )
        if session_id is not None and not session_id.strip():
            raise ValueError("session_id must not be empty")

        self._components = _resolve_components(
            provider,
            components,
            tools=tools,
            approval_policy=approval_policy,
            execution_gate=execution_gate,
            context_strategy=context_strategy,
            next_turn_hook=next_turn_hook,
            system_prompt=system_prompt,
            max_model_requests=max_model_requests,
        )
        tool_entries = tuple(
            (tool.definition, tool) for tool in self._components.tools
        )
        self._tools = {definition.name: tool for definition, tool in tool_entries}
        self._tool_definitions = tuple(definition for definition, _ in tool_entries)
        self._event_sink = ensure_isolated_event_sink(event_sink)
        if any(
            callback is not None
            for callback in (
                message_committer, steering_committer, provider_request_hook, steering_receiver
            )
        ):
            warn("Agent wiring keywords are deprecated", DeprecationWarning, stacklevel=2)
        self._wiring = _AgentWiring(
            message_committer, steering_committer, provider_request_hook, steering_receiver
        )
        self._next_turn_hook = self._components.next_turn_hook
        self._session_id = session_id
        self._active_run_id: str | None = None
        self._active_turn_id: str | None = None
        self._messages = list(initial_messages)
        initial_prompt = self._components.system_prompt
        if not self._messages and initial_prompt is not None:
            self._messages.append(Message(role="system", content=initial_prompt))
        self._run_lock = asyncio.Lock()

    @classmethod
    def _for_session(
        cls,
        components: AgentComponents,
        *,
        wiring: _AgentWiring,
        event_sink: EventSink,
        initial_messages: Sequence[Message],
        session_id: str,
        next_turn_hook: NextTurnHook | None,
    ) -> Agent:
        agent = cls(
            components,
            event_sink=event_sink,
            initial_messages=initial_messages,
            session_id=session_id,
        )
        agent._wiring = wiring
        agent._next_turn_hook = next_turn_hook
        return agent

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
        prompt_already_committed: bool = False,
    ) -> AgentResult:
        """Run model/tool turns until a final assistant message is committed."""
        if not prompt.strip():
            raise ValueError("prompt must contain non-whitespace text")
        if run_id is not None and not run_id.strip():
            raise ValueError("run_id must not be empty")

        async with self._run_lock:
            self._active_run_id = run_id or uuid4().hex
            self._active_turn_id = _turn_id(self._active_run_id, 1)
            if not self._wiring.dispatch_started:
                self._emit("run_started", "live")
            total_usage: TokenUsage | None = None

            try:
                if not prompt_already_committed:
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
                max_requests = self._components.max_model_requests
                for request_number in range(1, max_requests + 1):
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
                                actor_id=type(self._components.provider).__name__,
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
                            await self._commit_steering(
                                Message(role="user", content=instruction.prompt),
                                SessionEventContext(
                                    run_id=self._active_run_id,
                                    turn_id=next_turn_id,
                                    provenance=EventProvenance(
                                        source="user",
                                        actor_id="steer",
                                    ),
                                ),
                                instruction.control_id,
                                instruction.claim_id,
                            )

                    if has_tool_calls or steering:
                        if request_number < max_requests:
                            await self._prepare_next_turn(request_number + 1)
                            continue
                        raise AgentLoopLimitError(
                            f"run exceeded {max_requests} model requests"
                        )

                    self._emit(
                        "run_completed",
                        "live",
                        finish_reason=turn.finish.reason,
                        model_requests=request_number,
                        usage=asdict(total_usage) if total_usage is not None else None,
                    )
                    return AgentResult(
                        message=turn.message,
                        messages=self.messages,
                        finish_reason=turn.finish.reason,
                        usage=total_usage,
                    )

                raise AgentLoopLimitError(
                    f"run exceeded {max_requests} model requests"
                )
            except asyncio.CancelledError:
                self._emit("run_cancelled", "live")
                raise
            except AgentError as error:
                self._emit("run_failed", "live", **_run_error_fields(error))
                raise
            except Exception as error:
                wrapped = InternalError("agent run failed unexpectedly")
                self._emit("run_failed", "live", **_run_error_fields(wrapped))
                raise wrapped from error
            finally:
                self._emit("run_settled", "live")
                self._active_run_id = None
                self._active_turn_id = None

    async def _receive_steering(
        self,
        *,
        close_if_empty: bool,
    ) -> tuple[_SteeringInstruction, ...]:
        receiver = self._wiring.steering_receiver
        if receiver is None:
            return ()
        return await receiver(close_if_empty)

    async def _commit_steering(
        self,
        message: Message,
        context: SessionEventContext,
        control_id: str,
        claim_id: str,
    ) -> None:
        committer = self._wiring.steering_committer
        if committer is None:
            await self._commit(message, context)
            return
        durable_identity = await committer(
            message,
            context,
            control_id,
            claim_id,
        )
        self._messages.append(message)
        self._emit(
            "message_committed",
            "durable",
            durable_event_id=durable_identity.event_id,
            provenance_source=context.provenance.source,
            **_message_payload(message),
        )

    async def _request_model(self, request_number: int) -> _ModelTurn:
        if self._active_run_id is None or self._active_turn_id is None:
            raise RuntimeError("provider request requires an active run and turn")
        request_context = ProviderRequestContext(
            request_id=uuid4().hex,
            attempt_id=uuid4().hex,
            purpose="agent_turn",
            request_number=request_number,
            session_id=self._session_id,
            run_id=self._active_run_id,
            turn_id=self._active_turn_id,
        )
        context = await self._prepare_context(
            request_number,
            request_context,
        )
        # This span represents the logical request. A resilient model runtime
        # emits child attempt spans with distinct attempt identities.
        span_id = request_context.request_id
        started_at = time.perf_counter()
        self._emit(
            "model_request_started",
            "live",
            request_number=request_number,
            request_id=request_context.request_id,
            span_id=span_id,
            purpose=request_context.purpose,
        )
        partial_text: list[str] = []
        partial_thinking: list[str] = []
        tool_calls: list[ToolCall] = []
        tool_call_ids: set[str] = set()
        provider_state: list[ProviderState] = []
        usage: TokenUsage | None = None
        finish: Finish | None = None

        try:
            async for event in self._stream_provider(context.messages, request_context):
                if isinstance(event, ThinkingDelta):
                    _require_unfinished(finish, "thinking")
                    partial_thinking.append(event.text)
                    self._emit(
                        "thinking_delta",
                        "live",
                        request_id=request_context.request_id,
                        parent_span_id=span_id,
                        text=event.text,
                    )
                elif isinstance(event, TextDelta):
                    _require_unfinished(finish, "text")
                    partial_text.append(event.text)
                    self._emit(
                        "text_delta",
                        "live",
                        request_id=request_context.request_id,
                        parent_span_id=span_id,
                        text=event.text,
                    )
                elif isinstance(event, ToolCall):
                    _require_unfinished(finish, "tool call")
                    if event.id in tool_call_ids:
                        raise ModelProtocolError(f"duplicate tool call id: {event.id}")
                    tool_call_ids.add(event.id)
                    tool_calls.append(event)
                    self._emit(
                        "tool_call_received",
                        "capability",
                        request_id=request_context.request_id,
                        parent_span_id=span_id,
                        tool_call_id=event.id,
                        tool_name=event.name,
                        arguments=event.arguments,
                    )
                elif isinstance(event, ProviderState):
                    _require_unfinished(finish, "provider state")
                    provider_state.append(deepcopy(event))
                elif isinstance(event, TokenUsage):
                    usage = event
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
        except asyncio.CancelledError:
            self._emit(
                "model_request_failed",
                "live",
                request_id=request_context.request_id,
                span_id=span_id,
                duration_ms=_duration_ms(started_at),
                request_number=request_number,
                outcome="cancelled",
                error="CancelledError",
            )
            raise
        except Exception as error:
            self._emit(
                "model_request_failed",
                "live",
                request_id=request_context.request_id,
                span_id=span_id,
                duration_ms=_duration_ms(started_at),
                request_number=request_number,
                outcome="error",
                error=type(error).__name__,
            )
            raise
        finally:
            if usage is not None:
                self._emit(
                    "model_usage",
                    "live",
                    request_id=request_context.request_id,
                    parent_span_id=span_id,
                    **asdict(usage),
                )

        self._emit(
            "model_request_finished",
            "live",
            request_id=request_context.request_id,
            span_id=span_id,
            duration_ms=_duration_ms(started_at),
            request_number=request_number,
            outcome="success",
            finish_reason=finish.reason,
        )

        return _ModelTurn(
            message=Message(
                role="assistant",
                content="".join(partial_text),
                thinking="".join(partial_thinking),
                tool_calls=tuple(tool_calls),
                provider_state=tuple(provider_state),
            ),
            finish=finish,
            usage=usage,
        )

    async def _stream_provider(
        self,
        messages: Sequence[Message],
        context: ProviderRequestContext,
    ) -> AsyncIterator[ModelStreamEvent]:
        try:
            async for event in self._components.provider.stream(
                messages, self._tool_definitions, context=context
            ):
                yield event
        except AgentError:
            raise
        except Exception as error:
            raise ProviderError("model provider failed") from error

    async def _prepare_context(
        self,
        request_number: int,
        request_context: ProviderRequestContext,
    ) -> ContextPlan:
        history = self.messages
        try:
            context = await self._components.context_strategy.prepare(history)
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
        hook = self._wiring.provider_request_hook
        if hook is not None:
            try:
                context = await hook.prepare(request_context, context)
            except asyncio.CancelledError:
                raise
            except AgentError:
                raise
            except Exception as error:
                raise ContextPreparationError(
                    "provider request hook failed"
                ) from error
            if not isinstance(context, ContextPlan):
                raise ContextPreparationError(
                    "provider request hook must return ContextPlan"
                )
            if context.source_message_count != len(history):
                raise ContextPreparationError(
                    "provider request hook cannot change source_message_count"
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
        if self._active_run_id is None:
            raise RuntimeError("next-turn preparation requires an active run")
        context = NextTurnContext(
            session_id=self._session_id,
            run_id=self._active_run_id,
            turn_id=_turn_id(self._active_run_id, next_request_number),
            next_request_number=next_request_number,
        )

        try:
            prepared = tuple(
                await hook.prepare(
                    self.messages,
                    context=context,
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
            decision = await self._components.approval_policy.review(context)
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
        span_id = uuid4().hex
        started_at = time.perf_counter()
        self._emit(
            "tool_execution_started",
            "capability",
            span_id=span_id,
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
                output = await self._components.execution_gate.execute(
                    tool,
                    call.arguments,
                    context,
                )
            else:
                async with asyncio.timeout(timeout):
                    output = await self._components.execution_gate.execute(
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
                span_id=span_id,
                duration_ms=_duration_ms(started_at),
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
        except ToolExecutionDeniedError as error:
            outcome = "execution_denied"
            self._emit(
                "tool_execution_denied",
                "capability",
                parent_span_id=span_id,
                tool_call_id=call.id,
                tool_name=call.name,
                side_effects=context.definition.side_effects,
                gate=type(self._components.execution_gate).__name__,
                error=type(error).__name__,
                request_number=context.request_number,
                call_index=context.call_index,
                session_id=context.session_id,
            )
            output = ToolOutput(
                content="Tool execution was blocked by the execution policy.",
                is_error=True,
            )
        except SandboxTimeoutError as error:
            outcome = "sandbox_timeout"
            output = ToolOutput(
                content=f"Sandbox execution timed out ({type(error).__name__}).",
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
            span_id=span_id,
            duration_ms=_duration_ms(started_at),
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
        durable_identity: DurableEventIdentity | None = None
        committer = self._wiring.message_committer
        if committer is not None:
            durable_identity = await committer(message, context)

        self._messages.append(message)
        self._emit(
            "message_committed",
            "durable",
            durable_event_id=(
                durable_identity.event_id
                if durable_identity is not None
                else None
            ),
            provenance_source=context.provenance.source,
            **_message_payload(message),
        )

    def _emit(
        self,
        kind: EventKind,
        channel: EventChannel,
        *,
        session_id: str | None = None,
        request_id: str | None = None,
        attempt_id: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        duration_ms: float | None = None,
        durable_event_id: str | None = None,
        **payload: object,
    ) -> None:
        self._event_sink.emit(
            make_agent_event(
                kind,
                channel,
                source="agent",
                payload=payload,
                session_id=session_id or self._session_id,
                run_id=self._active_run_id,
                turn_id=self._active_turn_id,
                request_id=request_id,
                attempt_id=attempt_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                duration_ms=duration_ms,
                durable_event_id=durable_event_id,
            )
        )


def _require_unfinished(finish: Finish | None, event_name: str) -> None:
    if finish is not None:
        raise ModelProtocolError(f"{event_name} arrived after finish")


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
