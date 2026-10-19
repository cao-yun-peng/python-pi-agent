from __future__ import annotations

from collections.abc import Sequence

from .domain import Message, SessionSnapshot
from .errors import SessionConflictError, SessionValidationError


class InMemorySessionStore:
    def __init__(self) -> None:
        self._messages_by_session: dict[str, list[Message]] = {}

    async def load(self, session_id: str) -> SessionSnapshot:
        _validate_session_id(session_id)

        messages = self._messages_by_session.get(session_id, [])
        return SessionSnapshot(
            session_id=session_id,
            version=len(messages),
            messages=tuple(messages),
        )

    async def append(
        self,
        session_id: str,
        expected_version: int,
        messages: Sequence[Message],
    ) -> SessionSnapshot:
        _validate_session_id(session_id)
        new_messages = _validate_append_input(expected_version, messages)

        current_messages = self._messages_by_session.get(session_id, [])
        current_version = len(current_messages)

        if current_version != expected_version:
            raise SessionConflictError(
                f"expected version {expected_version}, "
                f"but current version is {current_version}"
            )
        updated_messages = [*current_messages, *new_messages]
        self._messages_by_session[session_id] = updated_messages

        return SessionSnapshot(
            session_id=session_id,
            version=len(updated_messages),
            messages=tuple(updated_messages),
        )


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
