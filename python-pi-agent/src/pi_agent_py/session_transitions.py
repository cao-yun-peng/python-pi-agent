from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass

from .domain import (
    DurableSessionEvent,
    Message,
    SessionCompaction,
    SessionSnapshot,
)
from .errors import (
    SessionConflictError,
    SessionReplayError,
    SessionValidationError,
)


@dataclass(frozen=True, slots=True)
class SessionAppendPlan:
    """The immutable result of calculating one valid session append."""

    new_events: tuple[DurableSessionEvent, ...]
    snapshot: SessionSnapshot


@dataclass(frozen=True, slots=True)
class SessionCompactionPlan:
    """The immutable result of calculating one valid compaction append."""

    event: DurableSessionEvent
    snapshot: SessionSnapshot


def replay_session(
    session_id: str,
    events: Sequence[DurableSessionEvent],
) -> SessionSnapshot:
    """Rebuild one session snapshot from its ordered durable facts."""
    validate_session_id(session_id)

    messages: list[Message] = []
    expected_sequence = 1

    for event in events:
        if event.session_id != session_id:
            raise SessionReplayError(
                f"event belongs to session {event.session_id!r}, "
                f"not {session_id!r}"
            )
        if event.sequence != expected_sequence:
            raise SessionReplayError(
                f"expected event sequence {expected_sequence}, "
                f"but received {event.sequence}"
            )

        if event.kind == "message_committed":
            if event.message is None:
                raise SessionReplayError(
                    "message_committed event is missing its message payload"
                )
            messages.append(event.message)
        elif event.kind == "context_compacted":
            if event.compaction is None:
                raise SessionReplayError(
                    "context_compacted event is missing its compaction payload"
                )
            _apply_replayed_compaction(messages, event.compaction)
        else:
            raise SessionReplayError(f"unsupported event kind: {event.kind}")
        expected_sequence += 1

    return SessionSnapshot(
        session_id=session_id,
        version=expected_sequence - 1,
        messages=tuple(messages),
    )


def prepare_session_append(
    session_id: str,
    current_events: Sequence[DurableSessionEvent],
    expected_version: int,
    messages: Sequence[Message],
) -> SessionAppendPlan:
    """Calculate new events and state without performing storage I/O."""
    validate_session_id(session_id)
    new_messages = _normalize_append_input(expected_version, messages)
    current_snapshot = replay_session(session_id, current_events)

    if current_snapshot.version != expected_version:
        raise SessionConflictError(
            f"expected version {expected_version}, "
            f"but current version is {current_snapshot.version}"
        )

    new_events = tuple(
        DurableSessionEvent(
            session_id=session_id,
            sequence=current_snapshot.version + offset,
            kind="message_committed",
            message=message,
        )
        for offset, message in enumerate(new_messages, start=1)
    )
    snapshot = replay_session(
        session_id,
        (*current_events, *new_events),
    )
    return SessionAppendPlan(new_events=new_events, snapshot=snapshot)


def prepare_session_compaction(
    session_id: str,
    current_events: Sequence[DurableSessionEvent],
    expected_version: int,
    compaction: SessionCompaction,
) -> SessionCompactionPlan:
    """Calculate a durable compaction event without performing storage I/O."""
    validate_session_id(session_id)
    current_snapshot = replay_session(session_id, current_events)
    if current_snapshot.version != expected_version:
        raise SessionConflictError(
            f"expected version {expected_version}, "
            f"but current version is {current_snapshot.version}"
        )

    _validate_compaction_range(current_snapshot.messages, compaction)
    event = DurableSessionEvent(
        session_id=session_id,
        sequence=current_snapshot.version + 1,
        kind="context_compacted",
        compaction=compaction,
    )
    snapshot = replay_session(session_id, (*current_events, event))
    return SessionCompactionPlan(event=event, snapshot=snapshot)


def fork_session_events(
    source_events: Sequence[DurableSessionEvent],
    target_session_id: str,
    at_version: int | None = None,
) -> tuple[DurableSessionEvent, ...]:
    """Copy a validated source history into an isolated session event stream."""
    validate_session_id(target_session_id)
    normalized_events = tuple(source_events)

    if not normalized_events:
        _validate_fork_version(at_version, source_version=0)
        return ()

    source_session_id = normalized_events[0].session_id
    source_snapshot = replay_session(source_session_id, normalized_events)

    if source_session_id == target_session_id:
        raise SessionValidationError(
            "target_session_id must differ from the source session id"
        )

    fork_version = _validate_fork_version(
        at_version,
        source_version=source_snapshot.version,
    )
    forked_events = tuple(
        DurableSessionEvent(
            session_id=target_session_id,
            sequence=event.sequence,
            kind=event.kind,
            message=deepcopy(event.message),
            compaction=deepcopy(event.compaction),
        )
        for event in normalized_events[:fork_version]
    )

    replay_session(target_session_id, forked_events)
    return forked_events


def validate_session_id(session_id: str) -> None:
    if not session_id.strip():
        raise SessionValidationError("session_id must not be empty")


def _apply_replayed_compaction(
    messages: list[Message],
    compaction: SessionCompaction,
) -> None:
    try:
        _validate_compaction_range(messages, compaction)
    except SessionValidationError as error:
        raise SessionReplayError(f"invalid compaction event: {error}") from error
    end = compaction.replace_start + compaction.replaced_message_count
    messages[compaction.replace_start:end] = [compaction.summary_message]


def _validate_compaction_range(
    messages: Sequence[Message],
    compaction: SessionCompaction,
) -> None:
    leading_system_count = 0
    while (
        leading_system_count < len(messages)
        and messages[leading_system_count].role == "system"
    ):
        leading_system_count += 1
    if compaction.replace_start != leading_system_count:
        raise SessionValidationError(
            "compaction must replace the conversation prefix after leading "
            "system messages"
        )
    end = compaction.replace_start + compaction.replaced_message_count
    if end > len(messages):
        raise SessionValidationError(
            "compaction replacement exceeds the current message history"
        )
    if any(
        message.role == "system"
        for message in messages[compaction.replace_start:end]
    ):
        raise SessionValidationError("compaction cannot replace system messages")


def _normalize_append_input(
    expected_version: int,
    messages: Sequence[Message],
) -> tuple[Message, ...]:
    if expected_version < 0:
        raise SessionValidationError("expected_version must not be negative")

    normalized_messages = tuple(messages)
    if not normalized_messages:
        raise SessionValidationError("messages must not be empty")

    return normalized_messages


def _validate_fork_version(
    at_version: int | None,
    *,
    source_version: int,
) -> int:
    fork_version = source_version if at_version is None else at_version
    if fork_version < 0:
        raise SessionValidationError("at_version must not be negative")
    if fork_version > source_version:
        raise SessionValidationError(
            f"at_version {fork_version} exceeds source version {source_version}"
        )
    return fork_version
