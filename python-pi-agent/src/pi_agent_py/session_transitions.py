from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass

from .domain import DurableSessionEvent, Message, SessionSnapshot
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

        messages.append(event.message)
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
        )
        for event in normalized_events[:fork_version]
    )

    replay_session(target_session_id, forked_events)
    return forked_events


def validate_session_id(session_id: str) -> None:
    if not session_id.strip():
        raise SessionValidationError("session_id must not be empty")


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
