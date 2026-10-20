"""The provider-neutral agent and tool-call loop."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass

from .context import FullHistoryContextStrategy
from .domain import (
    AgentEvent,
    AgentResult,
    ContextPlan,
    EventChannel,
    EventKind,
    Finish,
    Message,
    SessionSnapshot,
    TextDelta,
    ThinkingDelta,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolOutput,
)
from .errors import (
    AgentError,
    AgentLoopLimitError,
    ContextPreparationError,
    ModelProtocolError,
    ProviderError,
    ToolValidationError,
)
from .events import NullEventSink
from .ports import (
    ApprovalPolicy,
    ContextStrategy,
    EventSink,
    ModelProvider,
    SessionStore,
    Tool,
)
from .tools import DenyByDefaultApprovalPolicy, validate_tool_arguments


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
        context_strategy: ContextStrategy | None = None,
        event_sink: EventSink | None = None,
        initial_messages: Sequence[Message] = (),
        message_committer: Callable[[Message], Awaitable[None]] | None = None,
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

        self._provider = provider
        self._tools = {tool.definition.name: tool for tool in tools}
        self._tool_definitions = tuple(definitions)
        self._approval_policy = approval_policy or DenyByDefaultApprovalPolicy()
        self._context_strategy = context_strategy or FullHistoryContextStrategy()
        self._event_sink = event_sink or NullEventSink()
        self._max_model_requests = max_model_requests
        self._message_committer = message_committer
        self._messages = list(initial_messages)
        if system_prompt is not None:
            self._messages.append(Message(role="system", content=system_prompt))
        self._run_lock = asyncio.Lock()

    @property
    def messages(self) -> tuple[Message, ...]:
        """Return an immutable snapshot of committed model-visible history."""
        return tuple(self._messages)

    async def run(self, prompt: str) -> AgentResult:
        """Run model/tool turns until a final assistant message is committed."""
        if not prompt.strip():
            raise ValueError("prompt must contain non-whitespace text")

        async with self._run_lock:
            self._emit("run_started", "live")
            total_usage: TokenUsage | None = None

            try:
                await self._commit(Message(role="user", content=prompt))
                for request_number in range(1, self._max_model_requests + 1):
                    turn = await self._request_model(request_number)
                    total_usage = _merge_usage(total_usage, turn.usage)
                    await self._commit(turn.message)

                    if turn.message.tool_calls:
                        if turn.finish.reason == "length":
                            await self._fail_truncated_tool_calls(
                                turn.message.tool_calls
                            )
                        else:
                            await self._execute_tool_calls(turn.message.tool_calls)
                        continue

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

    async def _execute_tool_calls(self, calls: Sequence[ToolCall]) -> None:
        for call in calls:
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

            if tool.definition.requires_approval:
                approved = await self._review_tool_call(call, tool.definition)
                if not approved:
                    await self._commit_tool_result(
                        call,
                        ToolOutput(
                            content="Tool call was denied by the approval policy.",
                            is_error=True,
                        ),
                    )
                    continue

            output = await self._run_tool(tool, call)
            await self._commit_tool_result(call, output)

    async def _review_tool_call(
        self,
        call: ToolCall,
        definition: ToolDefinition,
    ) -> bool:
        self._emit(
            "tool_approval_requested",
            "capability",
            tool_call_id=call.id,
            tool_name=call.name,
            side_effects=definition.side_effects,
        )
        try:
            decision = await self._approval_policy.review(call, definition)
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
            )
            return False

        self._emit(
            "tool_approval_decided",
            "capability",
            tool_call_id=call.id,
            tool_name=call.name,
            approved=decision.approved,
            reason=decision.reason,
        )
        return decision.approved

    async def _run_tool(self, tool: Tool, call: ToolCall) -> ToolOutput:
        self._emit(
            "tool_execution_started",
            "capability",
            tool_call_id=call.id,
            tool_name=call.name,
        )
        outcome = "success"
        try:
            timeout = tool.definition.timeout_seconds
            if timeout is None:
                output = await tool.execute(call.arguments)
            else:
                async with asyncio.timeout(timeout):
                    output = await tool.execute(call.arguments)
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
            )
            raise
        except TimeoutError:
            outcome = "timeout"
            output = ToolOutput(
                content="Tool execution exceeded its timeout.",
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
            )
        )

    async def _commit(self, message: Message) -> None:
        if self._message_committer is not None:
            await self._message_committer(message)

        self._messages.append(message)
        self._emit("message_committed", "durable", **_message_payload(message))

    def _emit(
        self,
        kind: EventKind,
        channel: EventChannel,
        **payload: object,
    ) -> None:
        self._event_sink.emit(AgentEvent(kind=kind, channel=channel, payload=payload))


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

    async def commit(self, message: Message) -> None:
        self._snapshot = await self._session_store.append(
            self._snapshot.session_id,
            self._snapshot.version,
            (message,),
        )


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
        context_strategy: ContextStrategy | None = None,
        event_sink: EventSink | None = None,
        system_prompt: str | None = None,
        max_model_requests: int = 8,
    ) -> None:
        self._session_id = session_id
        self._session_store = session_store
        self._provider = provider
        self._tools = tuple(tools)
        self._approval_policy = approval_policy
        self._context_strategy = context_strategy
        self._event_sink = event_sink
        self._system_prompt = system_prompt
        self._max_model_requests = max_model_requests
        self._run_lock = asyncio.Lock()

    async def run(self, prompt: str) -> AgentResult:
        """Run against the latest stored history and persist the new messages."""
        if not prompt.strip():
            raise ValueError("prompt must contain non-whitespace text")

        async with self._run_lock:
            snapshot = await self._session_store.load(self._session_id)
            writer = _SessionWriter(self._session_store, snapshot)

            if not snapshot.messages and self._system_prompt is not None:
                await writer.commit(
                    Message(role="system", content=self._system_prompt)
                )

            agent = Agent(
                self._provider,
                tools=self._tools,
                approval_policy=self._approval_policy,
                context_strategy=self._context_strategy,
                event_sink=self._event_sink,
                initial_messages=writer.snapshot.messages,
                message_committer=writer.commit,
                max_model_requests=self._max_model_requests,
            )
            result = await agent.run(prompt)

            return AgentResult(
                message=result.message,
                messages=writer.snapshot.messages,
                finish_reason=result.finish_reason,
                usage=result.usage,
            )


def _require_unfinished(finish: Finish | None, event_name: str) -> None:
    if finish is not None:
        raise ModelProtocolError(f"{event_name} arrived after finish")


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
