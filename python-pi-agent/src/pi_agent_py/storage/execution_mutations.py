"""Pure agreement checks between execution progress and actual session facts."""

from collections.abc import Sequence

from ..domain import (
    ControlTransition,
    ControlDelivery,
    Message,
    SessionEventKind,
    DurableSessionEvent,
    SessionSnapshot,
)
from ..errors import SessionValidationError
from ..execution_domain import (
    AppendExecutionMessages,
    CompactExecutionHistory,
    DeliverExecutionControl,
    RecordExecutionControl,
    ExecutionGrant,
    ExecutionMutation,
    ProgressCommand,
    CommitPrompt,
    CommitAssistant,
    CommitToolResults,
    ContinueRun,
    DispatchModel,
    RunRecord,
)
from .transitions import (
    find_unresolved_tool_calls,
    prepare_session_append,
    prepare_session_compaction,
    prepare_session_control,
    replay_session,
)


def plan_execution_mutations(
    events: Sequence[DurableSessionEvent],
    grant: ExecutionGrant,
    record: RunRecord | None,
    command: ProgressCommand | None,
    mutations: tuple[ExecutionMutation, ...],
) -> tuple[tuple[DurableSessionEvent, ...], SessionSnapshot]:
    control: ControlTransition
    kind: SessionEventKind
    history = tuple(events)
    before = replay_session(grant.session_id, history)
    snapshot = before
    new: list[DurableSessionEvent] = []
    if command is None and not mutations:
        raise SessionValidationError("empty execution step")
    if isinstance(command, DispatchModel) and mutations:
        raise SessionValidationError("dispatch cannot mutate session history")
    for mutation in mutations:
        if mutation.context.run_id != grant.run_id:
            raise SessionValidationError(
                "mutation context must identify the granted run"
            )
        if grant.purpose == "maintenance" and not isinstance(
            mutation, CompactExecutionHistory
        ):
            raise SessionValidationError("maintenance only authorizes compaction")
        if command is None and not isinstance(
            mutation, (CompactExecutionHistory, RecordExecutionControl)
        ):
            raise SessionValidationError("message changes require a progress command")
        if isinstance(mutation, AppendExecutionMessages):
            if not isinstance(
                command, (CommitPrompt, CommitAssistant, CommitToolResults)
            ):
                raise SessionValidationError("append does not match execution command")
            plan = prepare_session_append(
                grant.session_id,
                history,
                snapshot.version,
                mutation.messages,
                event_context=mutation.context,
            )
            added, snapshot = plan.new_events, plan.snapshot
        elif isinstance(mutation, CompactExecutionHistory):
            if command is not None or find_unresolved_tool_calls(snapshot.messages):
                raise SessionValidationError(
                    "compaction requires a separate closed-tail step"
                )
            compacted = prepare_session_compaction(
                grant.session_id,
                history,
                snapshot.version,
                mutation.compaction,
                event_context=mutation.context,
            )
            added, snapshot = (compacted.event,), compacted.snapshot
        elif isinstance(mutation, (DeliverExecutionControl, RecordExecutionControl)):
            pending = {p.control.control_id: p for p in snapshot.pending_controls}
            if isinstance(mutation, DeliverExecutionControl):
                control = mutation.delivery
                item = pending.get(control.control_id)
                if (
                    item is None
                    or item.control.run_id != grant.run_id
                    or item.claim is None
                    or item.claim.owner_id != grant.owner_id
                ):
                    raise SessionValidationError(
                        "delivery is not owned by this execution"
                    )
                if not isinstance(command, (CommitPrompt, ContinueRun)):
                    raise SessionValidationError(
                        "delivery requires prompt or continuation"
                    )
                kind = "control_delivered"
            else:
                control = mutation.control
                kind = mutation.kind
                from ..domain import QueuedControl, ControlClaim

                if isinstance(control, QueuedControl):
                    if (
                        control.delivery != "steer"
                        or control.run_id != grant.run_id
                        or control.target_run_id != grant.run_id
                    ):
                        raise SessionValidationError(
                            "owner can enqueue only steering for this run"
                        )
                else:
                    item = pending.get(control.control_id)
                    if item is None or item.control.run_id != grant.run_id:
                        raise SessionValidationError(
                            "control does not belong to this execution"
                        )
                    if (
                        isinstance(control, ControlClaim)
                        and control.owner_id != grant.owner_id
                    ):
                        raise SessionValidationError(
                            "control claim owner differs from grant"
                        )
            controlled = prepare_session_control(
                grant.session_id,
                history,
                snapshot.version,
                kind,
                control,
                event_context=mutation.context,
            )
            added, snapshot = (controlled.event,), controlled.snapshot
        else:
            raise SessionValidationError("unsupported execution mutation")
        new.extend(added)
        history += added
        find_unresolved_tool_calls(snapshot.messages)
    if record is None:
        if grant.purpose != "maintenance" or command is not None:
            raise SessionValidationError("run progress requires a run record")
        return tuple(new), snapshot
    visible = [e for e in new if e.message is not None or e.kind == "control_delivered"]
    messages: list[Message] = []
    for event in visible:
        if event.message is not None:
            messages.append(event.message)
        elif isinstance(event.control, ControlDelivery):
            messages.append(event.control.message)
        else:
            raise SessionValidationError("invalid visible execution event")
    if isinstance(command, CommitPrompt):
        users = [(e, m) for e, m in zip(visible, messages) if m.role == "user"]
        if len(users) != 1 or users[0][1].content != record.submission.prompt:
            raise SessionValidationError("prompt differs from accepted submission")
        event, _ = users[0]
        if (event.event_id, event.sequence, event.session_id) != (
            command.event.event_id,
            command.event.sequence,
            command.event.session_id,
        ):
            raise SessionValidationError("prompt reference is not the committed event")
        if record.submission.control_id is not None:
            if (
                not isinstance(event.control, ControlDelivery)
                or event.control.control_id != record.submission.control_id
            ):
                raise SessionValidationError(
                    "follow-up must atomically deliver its control"
                )
        elif event.kind != "message_committed":
            raise SessionValidationError("ordinary prompt must be a message commit")
        if (
            any(m.role not in ("system", "user") for m in messages)
            or any(m.role == "system" for m in messages)
            and before.messages
        ):
            raise SessionValidationError("invalid prompt preparation messages")
    elif isinstance(command, CommitAssistant):
        if (
            len(visible) != 1
            or messages[0].role != "assistant"
            or visible[0].event_id != command.event.event_id
            or visible[0].sequence != command.event.sequence
            or command.event.session_id != grant.session_id
        ):
            raise SessionValidationError(
                "assistant reference does not match committed message"
            )
        if tuple(c.id for c in messages[0].tool_calls) != command.tool_call_ids:
            raise SessionValidationError("checkpoint tool calls differ from assistant")
    elif isinstance(command, CommitToolResults):
        if (
            any(m.role != "tool" for m in messages)
            or tuple(m.tool_call_id for m in messages) != command.tool_call_ids
        ):
            raise SessionValidationError("tool result facts differ from checkpoint")
    elif isinstance(command, ContinueRun):
        if any(e.kind != "control_delivered" for e in visible) or tuple(
            e.event_id for e in visible
        ) != tuple(e.event_id for e in command.steering_events):
            raise SessionValidationError(
                "steering references differ from delivered controls"
            )
        for reference, event in zip(command.steering_events, visible):
            if (
                reference.session_id != event.session_id
                or reference.sequence != event.sequence
            ):
                raise SessionValidationError("invalid steering reference")
        if any(
            p.control.delivery == "steer" and p.control.run_id == grant.run_id
            for p in snapshot.pending_controls
        ):
            raise SessionValidationError(
                "accepted steering must be handled at boundary"
            )
    elif isinstance(command, DispatchModel):
        if find_unresolved_tool_calls(snapshot.messages):
            raise SessionValidationError("dispatch cannot bypass unresolved tools")
    elif command is not None:
        raise SessionValidationError("unsupported progress command")
    return tuple(new), snapshot
