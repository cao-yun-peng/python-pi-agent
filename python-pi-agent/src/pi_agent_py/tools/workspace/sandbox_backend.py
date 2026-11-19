"""Workspace text projection and a read-only ProcessSandbox adapter."""

from __future__ import annotations

import base64
import asyncio
import binascii
import hashlib
import json
from math import isfinite
from dataclasses import asdict
from importlib.resources import files
from typing import cast
from uuid import uuid4

from ...domain import (
    SandboxProcessRequest, WorkspaceReadLimits, WorkspaceReadRequest, WorkspaceReadResult,
    WorkspaceWriteRequest, WorkspaceEditRequest, WorkspaceMutationResult,
    WorkspaceQueryRequest, WorkspaceQueryResult, WorkspaceQueryLimits, WorkspaceEntry, WorkspaceMatch,
)
from ...errors import WorkspaceFileError, SandboxError
from ...ports import ProcessSandbox


def project_workspace_text(
    request: WorkspaceReadRequest, data: bytes, limits: WorkspaceReadLimits,
) -> WorkspaceReadResult:
    """Project whole lines, with no partial UTF-8 sequence or lost continuation."""
    if len(data) > limits.max_file_bytes:
        raise WorkspaceFileError("too_large")
    try:
        text = data.decode("utf-8-sig")
        if "\x00" in text:
            raise ValueError("NUL is not supported in workspace text")
    except (UnicodeError, ValueError) as error:
        raise WorkspaceFileError("not_utf8") from error
    lines = text.splitlines(keepends=True)
    start = request.offset - 1
    if start > len(lines):
        raise WorkspaceFileError("invalid_offset")
    page: list[str] = []
    byte_count = 0
    for line in lines[start:start + min(request.limit, limits.max_lines)]:
        size = len(line.encode("utf-8"))
        if byte_count + size > limits.max_output_bytes:
            if not page:
                raise WorkspaceFileError("line_too_long")
            break
        page.append(line)
        byte_count += size
    end = start + len(page)
    return WorkspaceReadResult(
        path=request.path, text="".join(page), offset=request.offset,
        returned_lines=len(page), total_lines=len(lines), total_bytes=len(data),
        version="sha256:" + hashlib.sha256(data).hexdigest(),
        next_offset=end + 1 if end < len(lines) else None,
    )


# Linux container helper. No shell, path interpolation, imports from the host,
# symlink following, or unbounded read. Each component is opened relative to an
# already-open directory descriptor, so a rename/symlink swap cannot escape.
_READ_PROGRAM = r'''
import base64, errno, json, os, stat, sys

def read():
    root, path, budget = sys.argv[1], sys.argv[2], int(sys.argv[3])
    parts = path.split('/')
    if any(p in ('', '.', '..') for p in parts) or path.startswith('/'):
        return {'error': 'permission_denied'}
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            except OSError as error:
                if error.errno == errno.ENOTDIR:
                    info = os.stat(part, dir_fd=directory, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        return {'error': 'symlink_denied'}
                raise
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            os.close(descriptor)
            return {'error': 'not_file'}
        with os.fdopen(descriptor, 'rb') as stream:
            if before.st_size > budget:
                return {'error': 'too_large'}
            data = stream.read(budget + 1)
            after = os.fstat(stream.fileno())
            if len(data) > budget:
                return {'error': 'too_large'}
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                return {'error': 'changed'}
            return {'data': base64.b64encode(data).decode('ascii')}
    finally:
        os.close(directory)

try:
    result = read()
except OSError as error:
    result = {'error': {errno.ENOENT: 'not_found', errno.ENOTDIR: 'not_file', errno.ELOOP: 'symlink_denied', errno.EACCES: 'permission_denied', errno.EPERM: 'permission_denied'}.get(error.errno, 'unavailable')}
except Exception:
    result = {'error': 'unavailable'}
print(json.dumps(result, separators=(',', ':')))
'''


class SandboxedWorkspaceFiles:
    """Borrow a Linux Python sandbox for bounded file access.

    Reads and queries mount /workspace read-only; mutations require allow_write.
    Write/edit share an instance-owned lock, held through failure cleanup.
    The sandbox owns process
    cleanup on timeout/cancellation. Its output cap must fit the bounded
    base64 response (roughly 4/3 max_file_bytes plus framing).
    """

    def __init__(
        self, sandbox: ProcessSandbox, *, limits: WorkspaceReadLimits | None = None,
        python_executable: str = "python3", timeout_seconds: float = 30.0,
        allow_write: bool = False, query_limits: WorkspaceQueryLimits | None = None,
    ) -> None:
        if not python_executable or "\x00" in python_executable:
            raise ValueError("python_executable must be nonempty without NUL")
        if isinstance(timeout_seconds, bool) or not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        self._sandbox, self._limits = sandbox, limits or WorkspaceReadLimits()
        self._python, self._timeout = python_executable, timeout_seconds
        self._mutation_lock = asyncio.Lock()
        self._allow_write = allow_write
        self._query_limits = query_limits or WorkspaceQueryLimits()

    async def query(self, request: WorkspaceQueryRequest) -> WorkspaceQueryResult:
        source = files(__package__).joinpath("_file_ops.py").read_text(encoding="utf-8") + "\n" + files(__package__).joinpath("_query_ops.py").read_text(encoding="utf-8")
        payload = {**asdict(request), "limits": asdict(self._query_limits)}
        response = await self._sandbox.run(SandboxProcessRequest(
            (self._python, "-I", "-c", source, "/workspace", str(self._limits.max_file_bytes)),
            mode="read-only", timeout_seconds=self._timeout,
            stdin=json.dumps(payload, ensure_ascii=False).encode("utf-8")))
        if response.enforcement != "full" or response.exit_code:
            raise WorkspaceFileError("unavailable")
        if response.output_truncated or len(response.stdout.encode("utf-8")) > self._query_limits.max_output_bytes + 32_768:
            raise WorkspaceFileError("protocol_error")
        try:
            value = json.loads(response.stdout)
            if not isinstance(value, dict):
                raise ValueError("invalid query response")
            if set(value) == {"error"}:
                raise WorkspaceFileError(value["error"])
            if set(value) != {"entries", "matches", "truncated", "scanned_entries", "skipped_files"}:
                raise ValueError("invalid query fields")
            if (not isinstance(value["entries"], list) or not isinstance(value["matches"], list)
                or len(value["entries"]) + len(value["matches"]) > self._query_limits.max_entries
                or type(value["truncated"]) is not bool
                or type(value["scanned_entries"]) is not int or not 0 <= value["scanned_entries"] <= self._query_limits.max_scan_entries
                or type(value["skipped_files"]) is not int or not 0 <= value["skipped_files"] <= value["scanned_entries"]):
                raise ValueError("invalid query metadata")
            entries, matches = [], []
            for row in value["entries"] + value["matches"]:
                if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                    raise ValueError("invalid query path")
                WorkspaceReadRequest(row["path"])
                if request.path != "." and row["path"] != request.path and not row["path"].startswith(request.path + "/"):
                    raise ValueError("query path outside request")
            for row in value["entries"]:
                if set(row) != {"path", "kind"} or row["kind"] not in ("file", "directory", "symlink"):
                    raise ValueError("invalid entry")
                entries.append(WorkspaceEntry(row["path"], row["kind"]))
            for row in value["matches"]:
                if (set(row) != {"path", "line", "text", "truncated"} or type(row["line"]) is not int or row["line"] <= 0
                    or not isinstance(row["text"], str) or len(row["text"]) > self._query_limits.max_match_chars or type(row["truncated"]) is not bool):
                    raise ValueError("invalid match")
                matches.append(WorkspaceMatch(row["path"], row["line"], row["text"], row["truncated"]))
            return WorkspaceQueryResult(tuple(entries), tuple(matches), value["truncated"], value["scanned_entries"], value["skipped_files"])
        except (ValueError, TypeError, KeyError) as error:
            raise WorkspaceFileError("protocol_error") from error

    async def write_text(self, request: WorkspaceWriteRequest) -> WorkspaceMutationResult:
        return await self._mutate("write", request)

    async def edit_text(self, request: WorkspaceEditRequest) -> WorkspaceMutationResult:
        return await self._mutate("edit", request)

    def _mutation_request(self, source: str, payload: dict[str, object]) -> SandboxProcessRequest:
        return SandboxProcessRequest(
            (self._python, "-I", "-c", source, "/workspace", str(self._limits.max_file_bytes), str(min(self._limits.max_output_bytes, 8192)), "--mutation"),
            mode="workspace-write", timeout_seconds=self._timeout,
            stdin=json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    async def _cleanup_after_failure(self, source: str, payload: dict[str, object], error: BaseException) -> None:
        try:
            response = await self._sandbox.run(self._mutation_request(source, {
                "operation": "cleanup", "path": payload["path"], "temporary": payload["temporary"],
            }))
            if response.enforcement != "full" or response.exit_code or response.output_truncated or response.stdout.strip() != '{"cleaned":true}':
                raise WorkspaceFileError("outcome_unknown")
        except Exception as cleanup_error:
            error.add_note(f"Temporary-file cleanup unconfirmed ({type(cleanup_error).__name__}).")

    async def _mutate(self, operation: str, request: WorkspaceWriteRequest | WorkspaceEditRequest) -> WorkspaceMutationResult:
        if not self._allow_write:
            raise WorkspaceFileError("permission_denied")
        payload = asdict(request)
        if operation == "edit":
            payload["content"] = payload.pop("new_text")
        payload["operation"] = operation
        payload["temporary"] = ".pi-agent-" + uuid4().hex + ".tmp"
        for name in ("content", "old_text"):
            if len(str(payload.get(name, "")).encode("utf-8")) > self._limits.max_file_bytes:
                raise WorkspaceFileError("too_large")
        source = files(__package__).joinpath("_file_ops.py").read_text(encoding="utf-8")
        async with self._mutation_lock:
            try:
                response = await self._sandbox.run(self._mutation_request(source, payload))
                if response.enforcement != "full" or response.exit_code or response.output_truncated:
                    raise WorkspaceFileError("outcome_unknown")
                if len(response.stdout.encode("utf-8")) > 131_072:
                    raise WorkspaceFileError("outcome_unknown")
                value = json.loads(response.stdout)
                if not isinstance(value, dict):
                    raise ValueError("invalid mutation response")
                if set(value) == {"error"}:
                    try:
                        raise WorkspaceFileError(value["error"])
                    except (TypeError, ValueError) as error:
                        raise WorkspaceFileError("outcome_unknown") from error
                if set(value) != {"path", "version", "bytes_written", "created", "diff", "diff_truncated"}:
                    raise ValueError("invalid mutation response shape")
                if (value["path"] != request.path or not isinstance(value["version"], str)
                    or type(value["bytes_written"]) is not int or not 0 <= value["bytes_written"] <= self._limits.max_file_bytes
                    or type(value["created"]) is not bool or not isinstance(value["diff"], str)
                    or type(value["diff_truncated"]) is not bool):
                    raise ValueError("invalid mutation response values")
                WorkspaceWriteRequest(request.path, "", value["version"])
                if len(value["diff"].encode("utf-8")) > min(self._limits.max_output_bytes, 8192):
                    raise ValueError("mutation preview exceeds budget")
                return WorkspaceMutationResult(cast(str, value["path"]), value["version"], value["bytes_written"], value["created"], value["diff"], value["diff_truncated"])
            except asyncio.CancelledError as error:
                await self._cleanup_after_failure(source, payload, error)
                raise
            except WorkspaceFileError as error:
                if error.code in ("outcome_unknown", "unavailable", "protocol_error"):
                    await self._cleanup_after_failure(source, payload, error)
                raise
            except (SandboxError, ValueError, TypeError) as error:
                unknown = WorkspaceFileError("outcome_unknown")
                await self._cleanup_after_failure(source, payload, unknown)
                raise unknown from error

    async def read_text(self, request: WorkspaceReadRequest) -> WorkspaceReadResult:
        result = await self._sandbox.run(SandboxProcessRequest(
            (self._python, "-I", "-c", _READ_PROGRAM, "/workspace", request.path,
             str(self._limits.max_file_bytes)),
            mode="read-only", timeout_seconds=self._timeout,
        ))
        if result.enforcement != "full":
            raise WorkspaceFileError("unavailable")
        if result.exit_code != 0:
            raise WorkspaceFileError("unavailable")
        if result.output_truncated or len(result.stdout.encode("utf-8")) > 2 * self._limits.max_file_bytes + 4096:
            raise WorkspaceFileError("protocol_error")
        try:
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ValueError("expected result object")
            if set(value) == {"error"}:
                code = value["error"]
                if code not in {"not_found", "not_file", "symlink_denied", "permission_denied", "too_large", "changed", "unavailable"}:
                    raise ValueError("unknown helper error")
                raise WorkspaceFileError(code)
            if set(value) != {"data"} or not isinstance(value["data"], str):
                raise ValueError("expected data")
            data = base64.b64decode(value["data"], validate=True)
        except (ValueError, TypeError, binascii.Error) as error:
            raise WorkspaceFileError("protocol_error") from error
        return project_workspace_text(request, data, self._limits)
