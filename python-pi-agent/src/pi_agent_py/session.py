from __future__ import annotations

from collections.abc import Sequence

from .domain import DurableSessionEvent, Message, SessionSnapshot
from .errors import SessionConflictError, SessionValidationError
from .session_transitions import (
    fork_session_events,
    prepare_session_append,
    replay_session,
    validate_session_id,
)


class InMemorySessionStore:
    def __init__(self) -> None:
        self._events_by_session: dict[str, list[DurableSessionEvent]] = {}

    async def load(self, session_id: str) -> SessionSnapshot:
        validate_session_id(session_id)

        events = self._events_by_session.get(session_id, [])
        return replay_session(session_id, events)

    async def read_events(
        self,
        session_id: str,
    ) -> tuple[DurableSessionEvent, ...]:
        """Return an immutable copy of the durable session log."""
        validate_session_id(session_id)
        return tuple(self._events_by_session.get(session_id, []))

    async def append(
        self,
        session_id: str,
        expected_version: int,
        messages: Sequence[Message],
    ) -> SessionSnapshot:
        current_events = self._events_by_session.get(session_id, [])
        plan = prepare_session_append(
            session_id,
            current_events,
            expected_version,
            messages,
        )
        updated_events = [*current_events, *plan.new_events]
        self._events_by_session[session_id] = updated_events

        return plan.snapshot

    async def fork(
        self,
        source_session_id: str,
        target_session_id: str,
        at_version: int | None = None,
    ) -> SessionSnapshot:
        validate_session_id(source_session_id)
        validate_session_id(target_session_id)
        if source_session_id == target_session_id:
            raise SessionValidationError(
                "target_session_id must differ from the source session id"
            )

        source_events = tuple(
            self._events_by_session.get(source_session_id, [])
        )
        forked_events = fork_session_events(
            source_events,
            target_session_id,
            at_version,
        )
        if target_session_id in self._events_by_session:
            raise SessionConflictError(
                f"target session {target_session_id!r} already exists"
            )
        if not forked_events:
            raise SessionValidationError(
                "a persisted fork must contain at least one event"
            )

        self._events_by_session[target_session_id] = list(forked_events)
        return replay_session(target_session_id, forked_events)
