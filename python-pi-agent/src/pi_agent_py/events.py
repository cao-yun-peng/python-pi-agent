"""Composable, failure-isolated event sinks for apps, logs, and tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Mapping

from .domain import AgentEvent, EventKind
from .ports import EventSink


_QUEUE_STOP = object()
_SAFE_TOKEN_KEYS = frozenset(
    {
        "cached_input_tokens",
        "estimated_tokens",
        "input_tokens",
        "output_tokens",
        "token_budget",
        "token_threshold",
        "total_tokens",
    }
)
_SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth_token",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "refresh_token",
        "secret",
        "set_cookie",
        "token",
    }
)


@dataclass(frozen=True, slots=True)
class EventSinkFailure:
    """A bounded, non-sensitive diagnostic for an observer failure."""

    event_kind: EventKind
    stage: Literal["emit", "enqueue", "deliver"]
    error_type: str
    dropped: bool


@dataclass(frozen=True, slots=True)
class EventRedactionPolicy:
    """Controls which model-visible event fields may reach a log sink."""

    replacement: str = "<redacted>"
    redact_message_content: bool = True
    redact_thinking: bool = True
    redact_tool_arguments: bool = True
    redact_structured_content: bool = True
    redact_sensitive_keys: bool = True


class NullEventSink:
    """Drops events when an application does not need observation."""

    def emit(self, event: AgentEvent) -> None:
        del event


class InMemoryEventSink:
    """Captures events in emission order for deterministic assertions."""

    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)


class CallbackEventSink:
    """Adapts a fast synchronous callback to the EventSink port."""

    def __init__(self, callback: Callable[[AgentEvent], None]) -> None:
        self._callback = callback

    def emit(self, event: AgentEvent) -> None:
        self._callback(event)


class IsolatedEventSink:
    """Prevents an observational sink from changing runtime control flow."""

    def __init__(self, sink: EventSink, *, max_failures: int = 100) -> None:
        if max_failures <= 0:
            raise ValueError("max_failures must be greater than zero")
        self._sink = sink
        self._max_failures = max_failures
        self._failures: list[EventSinkFailure] = []

    @property
    def failures(self) -> tuple[EventSinkFailure, ...]:
        """Return bounded diagnostics without downstream exception messages."""
        return tuple(self._failures)

    def emit(self, event: AgentEvent) -> None:
        try:
            self._sink.emit(event)
        except Exception as error:
            self._record_failure(
                EventSinkFailure(
                    event_kind=event.kind,
                    stage="emit",
                    error_type=type(error).__name__,
                    dropped=True,
                )
            )

    def _record_failure(self, failure: EventSinkFailure) -> None:
        self._failures.append(failure)
        if len(self._failures) > self._max_failures:
            del self._failures[0]


class RedactingEventSink:
    """Copies and redacts an event before forwarding it to a logging sink."""

    def __init__(
        self,
        sink: EventSink,
        *,
        policy: EventRedactionPolicy | None = None,
    ) -> None:
        self._sink = sink
        self._policy = policy or EventRedactionPolicy()

    def emit(self, event: AgentEvent) -> None:
        self._sink.emit(redact_event(event, self._policy))


class BufferedEventSink:
    """Delivers events in order on a bounded, explicitly closed worker."""

    def __init__(
        self,
        sink: EventSink,
        *,
        max_queue_size: int = 256,
        max_failures: int = 100,
    ) -> None:
        if max_queue_size <= 0:
            raise ValueError("max_queue_size must be greater than zero")
        if max_failures <= 0:
            raise ValueError("max_failures must be greater than zero")
        self._sink = sink
        self._queue: asyncio.Queue[AgentEvent | object] = asyncio.Queue(
            max_queue_size
        )
        self._max_failures = max_failures
        self._failures: list[EventSinkFailure] = []
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def failures(self) -> tuple[EventSinkFailure, ...]:
        """Return bounded queue and downstream delivery diagnostics."""
        return tuple(self._failures)

    def emit(self, event: AgentEvent) -> None:
        if self._closed:
            self._record_failure(event, "enqueue", RuntimeError, dropped=True)
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._record_failure(event, "enqueue", asyncio.QueueFull, dropped=True)
            return
        self._ensure_worker()

    async def flush(self) -> None:
        """Wait until every currently queued event has been delivered or failed."""
        if not self._queue.empty():
            self._ensure_worker()
        await self._queue.join()

    async def aclose(self) -> None:
        """Drain pending events and stop the worker; safe to call repeatedly."""
        if self._closed:
            if self._worker is not None:
                await asyncio.shield(self._worker)
            return
        await self.flush()
        self._closed = True
        if self._worker is None:
            return
        self._queue.put_nowait(_QUEUE_STOP)
        await asyncio.shield(self._worker)
        self._worker = None

    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._worker = loop.create_task(self._deliver())

    async def _deliver(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _QUEUE_STOP:
                    return
                if not isinstance(item, AgentEvent):
                    continue
                event = item
                try:
                    await asyncio.to_thread(self._sink.emit, event)
                except Exception as error:
                    self._record_failure(
                        event,
                        "deliver",
                        type(error),
                        dropped=True,
                    )
            finally:
                self._queue.task_done()

    def _record_failure(
        self,
        event: AgentEvent,
        stage: Literal["enqueue", "deliver"],
        error_type: type[BaseException],
        *,
        dropped: bool,
    ) -> None:
        self._failures.append(
            EventSinkFailure(
                event_kind=event.kind,
                stage=stage,
                error_type=error_type.__name__,
                dropped=dropped,
            )
        )
        if len(self._failures) > self._max_failures:
            del self._failures[0]


def ensure_isolated_event_sink(sink: EventSink | None) -> IsolatedEventSink:
    """Return one failure boundary, avoiding nested isolation wrappers."""
    if isinstance(sink, IsolatedEventSink):
        return sink
    return IsolatedEventSink(sink or NullEventSink())


def redact_event(
    event: AgentEvent,
    policy: EventRedactionPolicy | None = None,
) -> AgentEvent:
    """Create a logging-safe event without mutating the runtime event."""
    selected = policy or EventRedactionPolicy()
    payload = _redact_sensitive_values(event.payload, selected)

    if event.kind in {"text_delta", "thinking_delta"}:
        should_redact = (
            event.kind == "text_delta" and selected.redact_message_content
        ) or (event.kind == "thinking_delta" and selected.redact_thinking)
        if should_redact and "text" in payload:
            payload["text"] = selected.replacement

    if event.kind == "tool_call_received" and selected.redact_tool_arguments:
        if "arguments" in payload:
            payload["arguments"] = selected.replacement

    if event.kind == "message_committed":
        if selected.redact_message_content and "content" in payload:
            payload["content"] = selected.replacement
        if selected.redact_thinking and payload.get("thinking"):
            payload["thinking"] = selected.replacement
        if selected.redact_structured_content and payload.get(
            "structured_content"
        ) is not None:
            payload["structured_content"] = selected.replacement
        if selected.redact_tool_arguments:
            payload["tool_calls"] = _redact_tool_calls(
                payload.get("tool_calls"),
                selected.replacement,
            )

    return AgentEvent(kind=event.kind, channel=event.channel, payload=payload)


def _redact_tool_calls(value: object, replacement: str) -> object:
    if not isinstance(value, list):
        return value
    redacted: list[object] = []
    for item in value:
        if not isinstance(item, Mapping):
            redacted.append(item)
            continue
        copied = dict(item)
        if "arguments" in copied:
            copied["arguments"] = replacement
        redacted.append(copied)
    return redacted


def _redact_sensitive_values(
    value: Mapping[str, object],
    policy: EventRedactionPolicy,
) -> dict[str, object]:
    return {
        key: (
            policy.replacement
            if policy.redact_sensitive_keys and _is_secret_key(key)
            else _copy_and_redact(item, policy)
        )
        for key, item in value.items()
    }


def _copy_and_redact(value: object, policy: EventRedactionPolicy) -> object:
    if isinstance(value, Mapping):
        return _redact_sensitive_values(value, policy)
    if isinstance(value, list):
        return [_copy_and_redact(item, policy) for item in value]
    if isinstance(value, tuple):
        return tuple(_copy_and_redact(item, policy) for item in value)
    return value


def _is_secret_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in _SAFE_TOKEN_KEYS:
        return False
    return (
        normalized in _SECRET_KEYS
        or normalized.endswith(("_api_key", "_password", "_secret", "_token"))
    )
