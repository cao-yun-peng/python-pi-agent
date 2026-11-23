from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from pathlib import Path

from ..domain import (
    ControlTransition,
    DurableSessionEvent,
    Message,
    SessionCompaction,
    SessionEventContext,
    SessionEventKind,
    SessionSnapshot,
)
from ..errors import (
    SessionConflictError,
    SessionReplayError,
    SessionStorageError,
    SessionValidationError,
)
from ._codec import _encode_event, _decode_event
from .transitions import (
    fork_session_events,
    prepare_session_append,
    prepare_session_compaction,
    prepare_session_control,
    replay_session,
    validate_session_id,
)


class JsonlSessionStore:
    """Persist durable session events as one versioned JSON object per line."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()

    async def load(self, session_id: str) -> SessionSnapshot:
        events = await self.read_events(session_id)
        return replay_session(session_id, events)

    async def read_events(
        self,
        session_id: str,
    ) -> tuple[DurableSessionEvent, ...]:
        validate_session_id(session_id)

        async with self._lock:
            events = await self._read_all_events()

        return tuple(event for event in events if event.session_id == session_id)

    async def append(
        self,
        session_id: str,
        expected_version: int,
        messages: Sequence[Message],
        *,
        event_context: SessionEventContext | None = None,
    ) -> SessionSnapshot:
        async with self._lock:
            all_events = await self._read_all_events()
            session_events = tuple(
                event for event in all_events if event.session_id == session_id
            )
            plan = prepare_session_append(
                session_id,
                session_events,
                expected_version,
                messages,
                event_context=event_context,
            )
            encoded_lines = tuple(_encode_event(event) for event in plan.new_events)
            await self._append_lines(encoded_lines)

        return plan.snapshot

    async def compact(
        self,
        session_id: str,
        expected_version: int,
        compaction: SessionCompaction,
        *,
        event_context: SessionEventContext | None = None,
    ) -> SessionSnapshot:
        async with self._lock:
            all_events = await self._read_all_events()
            session_events = tuple(
                event for event in all_events if event.session_id == session_id
            )
            plan = prepare_session_compaction(
                session_id,
                session_events,
                expected_version,
                compaction,
                event_context=event_context,
            )
            await self._append_lines((_encode_event(plan.event),))

        return plan.snapshot

    async def record_control(
        self,
        session_id: str,
        expected_version: int,
        kind: SessionEventKind,
        control: ControlTransition,
        *,
        event_context: SessionEventContext | None = None,
    ) -> SessionSnapshot:
        async with self._lock:
            all_events = await self._read_all_events()
            session_events = tuple(
                event for event in all_events if event.session_id == session_id
            )
            plan = prepare_session_control(
                session_id,
                session_events,
                expected_version,
                kind,
                control,
                event_context=event_context,
            )
            await self._append_lines((_encode_event(plan.event),))
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

        async with self._lock:
            all_events = await self._read_all_events()
            source_events = tuple(
                event for event in all_events if event.session_id == source_session_id
            )
            forked_events = fork_session_events(
                source_events,
                target_session_id,
                at_version,
            )
            if any(event.session_id == target_session_id for event in all_events):
                raise SessionConflictError(
                    f"target session {target_session_id!r} already exists"
                )
            if not forked_events:
                raise SessionValidationError(
                    "a persisted fork must contain at least one event"
                )

            encoded_lines = tuple(_encode_event(event) for event in forked_events)
            await self._append_lines(encoded_lines)

        return replay_session(target_session_id, forked_events)

    async def _read_all_events(self) -> tuple[DurableSessionEvent, ...]:
        try:
            return await asyncio.to_thread(_read_event_file, self._path)
        except SessionReplayError:
            raise
        except OSError as error:
            raise SessionStorageError(
                f"failed to read session log: {self._path}"
            ) from error

    async def _append_lines(self, lines: Sequence[str]) -> None:
        try:
            await asyncio.to_thread(_append_event_lines, self._path, lines)
        except OSError as error:
            raise SessionStorageError(
                f"failed to append session log: {self._path}"
            ) from error


def _read_event_file(path: Path) -> tuple[DurableSessionEvent, ...]:
    if not path.exists():
        return ()

    events: list[DurableSessionEvent] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                events.append(_decode_event(line, line_number))
    except UnicodeDecodeError as error:
        raise SessionReplayError("session log is not valid UTF-8") from error
    return tuple(events)


def _append_event_lines(path: Path, lines: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(f"{line}\n" for line in lines))
        handle.flush()
        os.fsync(handle.fileno())
