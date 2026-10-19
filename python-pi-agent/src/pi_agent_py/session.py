from __future__ import annotations

from collections.abc import Sequence

from .domain import DurableSessionEvent, Message, SessionSnapshot
from .errors import (
    SessionConflictError,
    SessionReplayError,
    SessionValidationError,
)


def replay_session(
    session_id: str,
    events: Sequence[DurableSessionEvent],
) -> SessionSnapshot:
    """Rebuild one session snapshot from its ordered durable facts."""
    _validate_session_id(session_id)

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


class InMemorySessionStore:
    def __init__(self) -> None:
        self._events_by_session: dict[str, list[DurableSessionEvent]] = {}

    async def load(self, session_id: str) -> SessionSnapshot:
        _validate_session_id(session_id)

        events = self._events_by_session.get(session_id, [])
        return replay_session(session_id, events)

    async def read_events(
        self,
        session_id: str,
    ) -> tuple[DurableSessionEvent, ...]:
        """Return an immutable copy of the durable session log."""
        _validate_session_id(session_id)
        return tuple(self._events_by_session.get(session_id, []))

    async def append(
        self,
        session_id: str,
        expected_version: int,
        messages: Sequence[Message],
    ) -> SessionSnapshot:
        _validate_session_id(session_id)
        new_messages = _validate_append_input(expected_version, messages)

        current_events = self._events_by_session.get(session_id, [])
        current_snapshot = replay_session(session_id, current_events)

        if current_snapshot.version != expected_version:
            raise SessionConflictError(
                f"expected version {expected_version}, "
                f"but current version is {current_snapshot.version}"
            )

        new_events = [
            DurableSessionEvent(
                session_id=session_id,
                sequence=current_snapshot.version + offset,
                kind="message_committed",
                message=message,
            )
            for offset, message in enumerate(new_messages, start=1)
        ]
        updated_events = [*current_events, *new_events]
        self._events_by_session[session_id] = updated_events

        return replay_session(session_id, updated_events)


def _validate_session_id(session_id: str) -> None:
    if not session_id.strip():
        raise SessionValidationError("session_id must not be empty")


def _validate_append_input(
    expected_version: int,
    messages: Sequence[Message],
) -> tuple[Message, ...]:
    if expected_version < 0:
        raise SessionValidationError("expected_version must not be negative")

    normalized_messages = tuple(messages)
    if not normalized_messages:
        raise SessionValidationError("messages must not be empty")

    return normalized_messages
