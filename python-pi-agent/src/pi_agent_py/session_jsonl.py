from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from .domain import (
    DurableSessionEvent,
    JSONObject,
    JSONValue,
    Message,
    Role,
    SessionCompaction,
    SessionSnapshot,
    ToolCall,
)
from .errors import (
    SessionConflictError,
    SessionReplayError,
    SessionStorageError,
    SessionValidationError,
)
from .session_transitions import (
    fork_session_events,
    prepare_session_append,
    prepare_session_compaction,
    replay_session,
    validate_session_id,
)

_SCHEMA_VERSION = 3
_SUPPORTED_SCHEMA_VERSIONS = {1, 2, 3}
_LEGACY_EVENT_KEYS = {
    "schema_version",
    "session_id",
    "sequence",
    "kind",
    "message",
}
_EVENT_KEYS_V3 = {*_LEGACY_EVENT_KEYS, "compaction"}
_MESSAGE_KEYS_V1 = {
    "role",
    "content",
    "tool_calls",
    "tool_call_id",
    "tool_name",
    "is_error",
    "structured_content",
}
_MESSAGE_KEYS_V2 = {*_MESSAGE_KEYS_V1, "thinking"}
_TOOL_CALL_KEYS = {"id", "name", "arguments"}
_COMPACTION_KEYS = {
    "replace_start",
    "replaced_message_count",
    "summary_message",
}
_ROLES = {"system", "user", "assistant", "tool"}


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
            )
            encoded_lines = tuple(
                _encode_event(event) for event in plan.new_events
            )
            await self._append_lines(encoded_lines)

        return plan.snapshot

    async def compact(
        self,
        session_id: str,
        expected_version: int,
        compaction: SessionCompaction,
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
                event
                for event in all_events
                if event.session_id == source_session_id
            )
            forked_events = fork_session_events(
                source_events,
                target_session_id,
                at_version,
            )
            if any(
                event.session_id == target_session_id
                for event in all_events
            ):
                raise SessionConflictError(
                    f"target session {target_session_id!r} already exists"
                )
            if not forked_events:
                raise SessionValidationError(
                    "a persisted fork must contain at least one event"
                )

            encoded_lines = tuple(
                _encode_event(event) for event in forked_events
            )
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


def _encode_event(event: DurableSessionEvent) -> str:
    message = _encode_message(event.message) if event.message is not None else None
    compaction = None
    if event.compaction is not None:
        compaction = {
            "replace_start": event.compaction.replace_start,
            "replaced_message_count": event.compaction.replaced_message_count,
            "summary_message": _encode_message(
                event.compaction.summary_message
            ),
        }
    record = {
        "schema_version": _SCHEMA_VERSION,
        "session_id": event.session_id,
        "sequence": event.sequence,
        "kind": event.kind,
        "message": message,
        "compaction": compaction,
    }
    try:
        return json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise SessionValidationError(
            "session event contains a value that is not JSON serializable"
        ) from error


def _decode_event(line: str, line_number: int) -> DurableSessionEvent:
    if not line.strip():
        raise SessionReplayError(f"empty JSONL record at line {line_number}")

    try:
        raw: object = json.loads(line)
    except json.JSONDecodeError as error:
        raise SessionReplayError(
            f"invalid JSONL record at line {line_number}"
        ) from error

    try:
        record = _require_object(raw, "event")
        schema_version = _require_int(record["schema_version"], "schema_version")
        if schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported schema_version: {schema_version}")

        expected_event_keys = (
            _EVENT_KEYS_V3 if schema_version == 3 else _LEGACY_EVENT_KEYS
        )
        _require_exact_keys(record, expected_event_keys, "event")

        session_id = _require_string(record["session_id"], "session_id")
        sequence = _require_int(record["sequence"], "sequence")
        kind = _require_string(record["kind"], "kind")
        if kind == "message_committed":
            if schema_version == 3 and record["compaction"] is not None:
                raise ValueError(
                    "message_committed event compaction must be null"
                )
            message_record = _require_object(record["message"], "message")
            message = _decode_message(message_record, schema_version)
            return DurableSessionEvent(
                session_id=session_id,
                sequence=sequence,
                kind="message_committed",
                message=message,
            )
        if kind == "context_compacted" and schema_version == 3:
            if record["message"] is not None:
                raise ValueError("context_compacted event message must be null")
            compaction_record = _require_object(
                record["compaction"],
                "compaction",
            )
            return DurableSessionEvent(
                session_id=session_id,
                sequence=sequence,
                kind="context_compacted",
                compaction=_decode_compaction(compaction_record),
            )
        raise ValueError(f"unsupported event kind: {kind}")
    except (KeyError, TypeError, ValueError) as error:
        raise SessionReplayError(
            f"invalid session event at line {line_number}: {error}"
        ) from error


def _decode_message(
    record: Mapping[str, object],
    schema_version: int,
) -> Message:
    expected_keys = (
        _MESSAGE_KEYS_V2 if schema_version >= 2 else _MESSAGE_KEYS_V1
    )
    _require_exact_keys(record, expected_keys, "message")
    role_text = _require_string(record["role"], "message.role")
    if role_text not in _ROLES:
        raise ValueError(f"unsupported message role: {role_text}")

    raw_tool_calls = record["tool_calls"]
    if not isinstance(raw_tool_calls, list):
        raise TypeError("message.tool_calls must be an array")

    tool_calls = tuple(
        _decode_tool_call(_require_object(raw_call, "tool_call"))
        for raw_call in raw_tool_calls
    )
    return Message(
        role=cast(Role, role_text),
        content=_require_string(record["content"], "message.content"),
        thinking=(
            _require_string(record["thinking"], "message.thinking")
            if schema_version >= 2
            else ""
        ),
        tool_calls=tool_calls,
        tool_call_id=_require_optional_string(
            record["tool_call_id"],
            "message.tool_call_id",
        ),
        tool_name=_require_optional_string(
            record["tool_name"],
            "message.tool_name",
        ),
        is_error=_require_bool(record["is_error"], "message.is_error"),
        structured_content=cast(JSONValue, record["structured_content"]),
    )


def _encode_message(message: Message) -> dict[str, object]:
    return {
        "role": message.role,
        "content": message.content,
        "thinking": message.thinking,
        "tool_calls": [
            {
                "id": call.id,
                "name": call.name,
                "arguments": call.arguments,
            }
            for call in message.tool_calls
        ],
        "tool_call_id": message.tool_call_id,
        "tool_name": message.tool_name,
        "is_error": message.is_error,
        "structured_content": message.structured_content,
    }


def _decode_compaction(record: Mapping[str, object]) -> SessionCompaction:
    _require_exact_keys(record, _COMPACTION_KEYS, "compaction")
    summary_record = _require_object(
        record["summary_message"],
        "compaction.summary_message",
    )
    return SessionCompaction(
        replace_start=_require_int(
            record["replace_start"],
            "compaction.replace_start",
        ),
        replaced_message_count=_require_int(
            record["replaced_message_count"],
            "compaction.replaced_message_count",
        ),
        summary_message=_decode_message(summary_record, 3),
    )


def _decode_tool_call(record: Mapping[str, object]) -> ToolCall:
    _require_exact_keys(record, _TOOL_CALL_KEYS, "tool_call")
    arguments = _require_object(record["arguments"], "tool_call.arguments")
    return ToolCall(
        id=_require_string(record["id"], "tool_call.id"),
        name=_require_string(record["name"], "tool_call.name"),
        arguments=cast(JSONObject, arguments),
    )


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


def _require_object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise TypeError(f"{name} must be an object")
    return cast(dict[str, object], value)


def _require_exact_keys(
    record: Mapping[str, object],
    expected: set[str],
    name: str,
) -> None:
    actual = set(record)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"{name} keys mismatch; missing={missing}, extra={extra}")


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _require_optional_string(value: object, name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{name} must be a string or null")
    return value


def _require_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be a boolean")
    return value
