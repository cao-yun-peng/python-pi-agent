"""The minimal agent loop.

The loop knows only normalized model events, committed messages, and observable
runtime events. Vendor SDKs, persistence, user interfaces, and tools belong in
adapters added around this core.
"""

from __future__ import annotations

import asyncio

from .domain import (
    AgentEvent,
    AgentResult,
    EventChannel,
    EventKind,
    Finish,
    Message,
    TextDelta,
)
from .errors import AgentError, ModelProtocolError, ProviderError
from .events import NullEventSink
from .ports import EventSink, ModelProvider


class Agent:
    """A stateful, single-session agent with a serialized run loop."""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        event_sink: EventSink | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self._provider = provider
        self._event_sink = event_sink or NullEventSink()
        self._messages: list[Message] = []
        if system_prompt is not None:
            self._messages.append(Message(role="system", content=system_prompt))
        self._run_lock = asyncio.Lock()

    @property
    def messages(self) -> tuple[Message, ...]:
        """Return an immutable snapshot of committed model-visible history."""
        return tuple(self._messages)

    async def run(self, prompt: str) -> AgentResult:
        """Run one turn and commit the assistant message only after `Finish`."""
        if not prompt.strip():
            raise ValueError("prompt must contain non-whitespace text")

        async with self._run_lock:
            self._emit("run_started", "live")
            user_message = Message(role="user", content=prompt)
            self._commit(user_message)
            partial_text: list[str] = []

            try:
                self._emit("model_request_started", "live")
                finish: Finish | None = None
                async for event in self._provider.stream(self.messages):
                    if isinstance(event, TextDelta):
                        if finish is not None:
                            raise ModelProtocolError("text arrived after finish")
                        partial_text.append(event.text)
                        self._emit("text_delta", "live", text=event.text)
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

                assistant_message = Message(
                    role="assistant",
                    content="".join(partial_text),
                )
                self._commit(assistant_message)
                self._emit(
                    "run_completed",
                    "live",
                    finish_reason=finish.reason,
                )
                return AgentResult(
                    message=assistant_message,
                    messages=self.messages,
                    finish_reason=finish.reason,
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

    def _commit(self, message: Message) -> None:
        self._messages.append(message)
        self._emit(
            "message_committed",
            "durable",
            role=message.role,
            content=message.content,
        )

    def _emit(
        self,
        kind: EventKind,
        channel: EventChannel,
        **payload: object,
    ) -> None:
        self._event_sink.emit(AgentEvent(kind=kind, channel=channel, payload=payload))
