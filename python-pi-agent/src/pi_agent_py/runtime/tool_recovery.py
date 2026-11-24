"""Shared, pure unknown-outcome compensation; never retry a tool here."""

from collections.abc import Sequence

from ..domain import (
    Message,
    ToolCall,
    DurableSessionEvent,
    ControlDelivery,
    SessionSnapshot,
)
from ..errors import SessionReplayError
from ..execution_domain import RunRecord
from ..storage.transitions import find_unresolved_tool_calls, replay_session


def interrupted_tool_result(call: ToolCall) -> Message:
    return Message(
        role="tool",
        content=(
            "Tool execution did not produce a durable result because the previous "
            "agent run was interrupted. The external side-effect outcome is unknown. "
            "Do not assume the call failed or retry it automatically."
        ),
        tool_call_id=call.id,
        tool_name=call.name,
        is_error=True,
        structured_content={
            "status": "interrupted",
            "outcome": "unknown",
            "recovered": True,
        },
    )


def validate_checkpoint_history(
    record: RunRecord, events: Sequence[DurableSessionEvent]
) -> SessionSnapshot:
    """Check immutable references and unresolved calls before any recovery I/O."""
    sid, rid = record.submission.session_id, record.submission.run_id
    snapshot = replay_session(sid, events)
    cp = record.checkpoint
    indexed = {event.sequence: event for event in events}

    def referenced(reference: object) -> tuple[DurableSessionEvent, Message]:
        from ..domain import DurableEventIdentity

        if not isinstance(reference, DurableEventIdentity):
            raise SessionReplayError("checkpoint reference is missing")
        event = indexed.get(reference.sequence)
        if (
            event is None
            or event.event_id != reference.event_id
            or event.session_id != sid
            or event.run_id != rid
        ):
            raise SessionReplayError(
                "checkpoint reference differs from durable history"
            )
        message = (
            event.control.message
            if isinstance(event.control, ControlDelivery)
            else event.message
        )
        if message is None:
            raise SessionReplayError("checkpoint reference is not a visible message")
        return event, message

    if cp.prompt_event is not None:
        event, message = referenced(cp.prompt_event)
        if message.role != "user" or message.content != record.submission.prompt:
            raise SessionReplayError("checkpoint prompt differs from accepted input")
        control_id = record.submission.control_id
        if control_id is None and event.kind != "message_committed":
            raise SessionReplayError("ordinary prompt is not a message commit")
        if control_id is not None and (
            not isinstance(event.control, ControlDelivery)
            or event.control.control_id != control_id
        ):
            raise SessionReplayError("follow-up checkpoint has no matching delivery")
    elif any(
        event.run_id == rid
        and event.message is not None
        and event.message.role in ("user", "assistant")
        for event in events
    ):
        raise SessionReplayError("before_prompt already has visible Run input/output")
    if cp.assistant_event is not None:
        _, assistant = referenced(cp.assistant_event)
        if assistant.role != "assistant":
            raise SessionReplayError(
                "assistant checkpoint does not reference an assistant"
            )
        if cp.phase in (
            "awaiting_tools",
            "awaiting_boundary",
        ) and cp.requires_continuation != bool(assistant.tool_calls):
            raise SessionReplayError(
                "checkpoint continuation differs from assistant tools"
            )
    unresolved = find_unresolved_tool_calls(snapshot.messages)
    if (
        cp.phase == "awaiting_tools"
        and tuple(call.id for call in unresolved) != cp.unresolved_tool_ids
    ):
        raise SessionReplayError("checkpoint unresolved calls differ from history")
    if cp.phase in ("awaiting_model", "awaiting_boundary") and unresolved:
        raise SessionReplayError("checkpoint bypasses unresolved calls")
    return snapshot
