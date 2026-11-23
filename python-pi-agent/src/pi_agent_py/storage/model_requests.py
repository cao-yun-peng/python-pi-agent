"""Versioned, offline-readable normalized requests with immutable file publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import tempfile
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import NoReturn, ParamSpec, TypeVar, cast

from ..domain import (
    JSONValue, ModelRequestTarget, ProviderRequestContext, ProviderRequestPurpose,
    RecordedModelRequest, ToolDefinition, ToolSideEffects,
)
from ..errors import (
    SessionConflictError, SessionReplayError, SessionStorageError, SessionValidationError,
)
from ._codec import (
    _decode_message, _encode_message, _require_array, _require_bool,
    _require_exact_keys, _require_int, _require_object, _require_optional_string,
    _require_string,
)


_P = ParamSpec("_P")
_T = TypeVar("_T")
_REQUEST_KEYS = {"schema_version", "message_schema_version", "context", "target", "messages", "tools"}
_CONTEXT_KEYS = {
    "request_id", "attempt_id", "purpose", "request_number", "attempt", "session_id",
    "run_id", "turn_id", "parent_request_id", "cause_event_id",
}
_TOOL_KEYS = {
    "name", "description", "input_schema", "side_effects", "requires_approval",
    "retry_safe", "replay_safe", "timeout_seconds",
}


def _key(request: RecordedModelRequest) -> tuple[str, str]:
    return request.context.request_id, request.context.attempt_id


class InMemoryModelRequestStore:
    """Offline request-store substitute with detached read and write values."""

    def __init__(self) -> None:
        self._requests: dict[tuple[str, str], RecordedModelRequest] = {}

    async def record(self, request: RecordedModelRequest) -> None:
        """Save once per request/attempt pair; reject duplicate identities."""
        key = _key(request)
        if key in self._requests:
            raise SessionConflictError("model request identity already exists")
        self._requests[key] = deepcopy(request)

    async def read_requests(
        self, session_id: str | None = None,
    ) -> tuple[RecordedModelRequest, ...]:
        """Return detached records in insertion order, optionally by Session."""
        return deepcopy(tuple(
            request for request in self._requests.values()
            if session_id is None or request.context.session_id == session_id
        ))


class FileModelRequestStore:
    """Store one versioned JSON artifact per request/attempt under an explicit root.

    Files are fsynced before no-overwrite publication. Concurrent publishers of
    the same identity have one winner. The filesystem must support hard links.
    Pending files are not read as records; finalized corruption is rejected.
    There are no workers to close. Cancelled I/O is joined before returning.
    Records contain full model input and require application-owned access and
    retention policy. Directory-entry survival after power loss is not promised.
    """

    def __init__(
        self, root: str | Path, *, max_record_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if type(max_record_bytes) is not int or max_record_bytes <= 0:
            raise ValueError("max_record_bytes must be a positive integer")
        self._root = Path(root)
        self._max_record_bytes = max_record_bytes

    async def record(self, request: RecordedModelRequest) -> None:
        """Persist before dispatch; duplicate, encoding and I/O failures reject."""
        encoded = _encode(request)
        if len(encoded) > self._max_record_bytes:
            raise SessionValidationError("model request exceeds the record byte limit")
        filename = _filename(*_key(request))
        try:
            await _joined_io(_publish, self._root, filename, encoded)
        except FileExistsError as error:
            raise SessionConflictError("model request identity already exists") from error
        except OSError as error:
            raise SessionStorageError("failed to persist model request") from error

    async def read_requests(
        self, session_id: str | None = None,
    ) -> tuple[RecordedModelRequest, ...]:
        """Read finalized records without a provider; filename order is not chronology."""
        try:
            return await _joined_io(
                _read_requests, self._root, self._max_record_bytes, session_id,
            )
        except OSError as error:
            raise SessionStorageError("failed to read model requests") from error


async def _joined_io(
    operation: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs,
) -> _T:
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A cancelled waiter does not stop filesystem work in its worker thread.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                # Cancellation remains the caller's outcome; retrieve worker errors.
                break
        if not task.cancelled():
            task.exception()
        raise


def _filename(request_id: str, attempt_id: str) -> str:
    identity = json.dumps([request_id, attempt_id], ensure_ascii=True).encode("ascii")
    return hashlib.sha256(identity).hexdigest() + ".request.json"


def _publish(root: Path, filename: str, encoded: bytes) -> None:
    root.mkdir(parents=True, exist_ok=True)
    descriptor, pending = tempfile.mkstemp(prefix=".pending-", dir=root)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(pending, root / filename)
    finally:
        Path(pending).unlink(missing_ok=True)


def _encode(request: RecordedModelRequest) -> bytes:
    record = {
        "schema_version": 2,
        # Reuse the Session codec's explicit v6 message fields, including opaque state.
        "message_schema_version": 6,
        "context": asdict(request.context),
        "target": {
            "provider": request.target.provider, "model": request.target.model,
            "parameters": dict(request.target.parameters),
        },
        "messages": [_encode_message(message) for message in request.messages],
        "tools": [
            {
                "name": tool.name, "description": tool.description,
                "input_schema": dict(tool.input_schema), "side_effects": tool.side_effects,
                "requires_approval": tool.requires_approval, "retry_safe": tool.retry_safe,
                "replay_safe": tool.replay_safe, "timeout_seconds": tool.timeout_seconds,
                "prompt_guidelines": list(tool.prompt_guidelines),
            }
            for tool in request.tools
        ],
    }
    try:
        return (json.dumps(
            record, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"),
        ) + "\n").encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise SessionValidationError("model request must contain finite JSON values") from error


def _object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _constant(value: str) -> NoReturn:
    raise ValueError("non-finite JSON constant")


def _read_requests(
    root: Path, max_record_bytes: int, session_id: str | None,
) -> tuple[RecordedModelRequest, ...]:
    if not root.exists():
        return ()
    if not root.is_dir():
        raise NotADirectoryError("model request root must be a directory")
    requests: list[RecordedModelRequest] = []
    for path in sorted(root.glob("*.request.json")):
        with path.open("rb") as handle:
            encoded = handle.read(max_record_bytes + 1)
        if len(encoded) > max_record_bytes:
            raise SessionReplayError("model request exceeds the record byte limit")
        request = _decode(encoded)
        if path.name != _filename(*_key(request)):
            raise SessionReplayError("model request filename does not match its identity")
        if session_id is None or request.context.session_id == session_id:
            requests.append(request)
    return tuple(requests)


def _decode(encoded: bytes) -> RecordedModelRequest:
    try:
        raw: object = json.loads(encoded, object_pairs_hook=_object, parse_constant=_constant)
        json.dumps(raw, allow_nan=False)
        record = _require_object(raw, "request")
        _require_exact_keys(record, _REQUEST_KEYS, "request")
        version = _require_int(record["schema_version"], "schema_version")
        if version not in (1, 2):
            raise ValueError("unsupported request schema version")
        if _require_int(record["message_schema_version"], "message_schema_version") != 6:
            raise ValueError("unsupported request message version")
        raw_context = _require_object(record["context"], "context")
        _require_exact_keys(raw_context, _CONTEXT_KEYS, "context")
        purpose = _require_string(raw_context["purpose"], "purpose")
        context = ProviderRequestContext(
            request_id=_require_string(raw_context["request_id"], "request_id"),
            attempt_id=_require_string(raw_context["attempt_id"], "attempt_id"),
            purpose=cast(ProviderRequestPurpose, purpose),
            request_number=_require_int(raw_context["request_number"], "request_number"),
            attempt=_require_int(raw_context["attempt"], "attempt"),
            session_id=_require_optional_string(raw_context["session_id"], "session_id"),
            run_id=_require_optional_string(raw_context["run_id"], "run_id"),
            turn_id=_require_optional_string(raw_context["turn_id"], "turn_id"),
            parent_request_id=_require_optional_string(raw_context["parent_request_id"], "parent_request_id"),
            cause_event_id=_require_optional_string(raw_context["cause_event_id"], "cause_event_id"),
        )
        target = _require_object(record["target"], "target")
        _require_exact_keys(target, {"provider", "model", "parameters"}, "target")
        return RecordedModelRequest(
            context=context,
            target=ModelRequestTarget(
                _require_string(target["provider"], "provider"),
                _require_string(target["model"], "model"),
                cast(dict[str, JSONValue], _require_object(target["parameters"], "parameters")),
            ),
            messages=tuple(
                _decode_message(_require_object(message, "message"))
                for message in _require_array(record["messages"], "messages")
            ),
            tools=tuple(
                _decode_tool(_require_object(tool, "tool"), version=version)
                for tool in _require_array(record["tools"], "tools")
            ),
        )
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        raise SessionReplayError("invalid persisted model request") from error


def _decode_tool(record: dict[str, object], *, version: int) -> ToolDefinition:
    _require_exact_keys(record, _TOOL_KEYS | ({"prompt_guidelines"} if version == 2 else set()), "tool")
    effects = _require_string(record["side_effects"], "side_effects")
    if effects not in {"none", "read", "write", "external"}:
        raise ValueError("unsupported tool side effects")
    timeout = record["timeout_seconds"]
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
    ):
        raise TypeError("tool timeout must be a number or null")
    return ToolDefinition(
        name=_require_string(record["name"], "name"),
        description=_require_string(record["description"], "description"),
        input_schema=cast(dict[str, JSONValue], _require_object(record["input_schema"], "input_schema")),
        side_effects=cast(ToolSideEffects, effects),
        requires_approval=_require_bool(record["requires_approval"], "requires_approval"),
        retry_safe=_require_bool(record["retry_safe"], "retry_safe"),
        replay_safe=_require_bool(record["replay_safe"], "replay_safe"),
        timeout_seconds=timeout,
        prompt_guidelines=tuple(
            _require_string(rule, "prompt guideline")
            for rule in _require_array(record["prompt_guidelines"], "prompt_guidelines")
        ) if version == 2 else (),
    )
