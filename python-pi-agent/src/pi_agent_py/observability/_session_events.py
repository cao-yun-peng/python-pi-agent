"""Shared runtime event metadata and Session event emission."""

from __future__ import annotations

import time
from typing import TypedDict

from ..domain import (
    EventChannel,
    EventKind,
    JSONObject,
    JSONValue,
    Message,
    Role,
    SessionEventContext,
)
from ..errors import AgentError, _error_category
from .events import make_agent_event
from ..ports import EventSink


class _SessionEvents:
    """Correlate control, recovery, and compaction events with a Session."""

    def __init__(self, session_id: str, event_sink: EventSink) -> None:
        self._session_id = session_id
        self._event_sink = event_sink

    def emit(
        self,
        kind: EventKind,
        *,
        channel: EventChannel = "capability",
        event_context: SessionEventContext | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        request_id: str | None = None,
        attempt_id: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        duration_ms: float | None = None,
        durable_event_id: str | None = None,
        **payload: object,
    ) -> None:
        context = event_context or SessionEventContext(
            run_id=run_id,
            turn_id=turn_id,
        )
        self._event_sink.emit(
            make_agent_event(
                kind,
                channel,
                source="agent_session",
                payload=payload,
                session_id=self._session_id,
                run_id=context.run_id,
                turn_id=context.turn_id,
                request_id=request_id,
                attempt_id=attempt_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                duration_ms=duration_ms,
                durable_event_id=durable_event_id,
            )
        )


class _RunErrorFields(TypedDict):
    error: str
    error_category: str


def _run_error_fields(error: AgentError) -> _RunErrorFields:
    return {"error": type(error).__name__, "error_category": _error_category(error)}


def _turn_id(run_id: str, request_number: int) -> str:
    return f"{run_id}:turn:{request_number}"


def _duration_ms(started_at: float) -> float:
    """Return a non-negative monotonic duration for observer metadata."""
    return max(0.0, (time.perf_counter() - started_at) * 1000)


class _MessagePayload(TypedDict):
    role: Role
    content: str
    thinking: str
    tool_calls: list[dict[str, str | JSONObject]]
    tool_call_id: str | None
    tool_name: str | None
    is_error: bool
    structured_content: JSONValue


def _message_payload(message: Message) -> _MessagePayload:
    # Provider-owned continuation state is durable but deliberately excluded
    # from best-effort observer events because it may contain encrypted state.
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
