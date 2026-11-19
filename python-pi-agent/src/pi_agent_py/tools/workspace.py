"""Workspace text projection and a read-only ProcessSandbox adapter."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from math import isfinite

from ..domain import (
    SandboxProcessRequest, WorkspaceReadLimits, WorkspaceReadRequest, WorkspaceReadResult,
)
from ..errors import WorkspaceFileError
from ..ports import ProcessSandbox


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
    """Borrow an existing sandbox; the image must contain Linux Python 3.

    Each read uses /workspace mounted read-only. The sandbox owns process
    cleanup on timeout/cancellation. Its output cap must fit the bounded
    base64 response (roughly 4/3 max_file_bytes plus framing).
    """

    def __init__(
        self, sandbox: ProcessSandbox, *, limits: WorkspaceReadLimits | None = None,
        python_executable: str = "python3", timeout_seconds: float = 30.0,
    ) -> None:
        if not python_executable or "\x00" in python_executable:
            raise ValueError("python_executable must be nonempty without NUL")
        if isinstance(timeout_seconds, bool) or not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        self._sandbox, self._limits = sandbox, limits or WorkspaceReadLimits()
        self._python, self._timeout = python_executable, timeout_seconds

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
