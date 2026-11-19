"""Standalone standard-library operations, reused in memory and in an isolated helper.

Importing performs no I/O. The sandbox executes this module's source with -I;
all request data arrives on bounded stdin, never interpolated into source.
"""

from __future__ import annotations

import difflib
import errno
import hashlib
import json
import os
import stat
import sys


class OperationError(Exception):
    def __init__(self, code: str, consumed: int = 0) -> None:
        self.code = code
        self.consumed = consumed
        super().__init__(code)


def version(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def decode(data: bytes) -> str:
    try:
        text = data.decode("utf-8-sig")
        if "\x00" in text:
            raise ValueError("NUL")
        return text
    except (UnicodeError, ValueError) as error:
        raise OperationError("not_utf8") from error


def prepare_mutation(
    operation: str, path: str, before: bytes | None, content: str,
    old_text: str, expected: str | None, budget: int, diff_budget: int,
) -> tuple[bytes, dict[str, object]]:
    if before is not None and len(before) > budget:
        raise OperationError("too_large")
    if expected is not None and (before is None or version(before) != expected):
        raise OperationError("conflict")
    if operation == "write":
        if before is not None and expected is None:
            raise OperationError("conflict")
        if before is not None:
            decode(before)
        after = content.encode("utf-8")
    else:
        if before is None:
            raise OperationError("not_found")
        text = decode(before)
        # Normalize CRLF for matching, restore the file's newline convention.
        normalized = text.replace("\r\n", "\n")
        old = old_text.replace("\r\n", "\n")
        new = content.replace("\r\n", "\n")
        first = normalized.find(old)
        if not old or first < 0:
            raise OperationError("no_match")
        if normalized.find(old, first + 1) >= 0:
            raise OperationError("ambiguous_match")
        positions = []
        index = 0
        while index < len(text):
            positions.append(index)
            index += 2 if text[index:index + 2] == "\r\n" else 1
        positions.append(len(text))
        newline = "\r\n" if text.find("\n") > 0 and text[text.find("\n") - 1] == "\r" else "\n"
        changed = text[:positions[first]] + new.replace("\n", newline) + text[positions[first + len(old)]:]
        after = (b"\xef\xbb\xbf" if before.startswith(b"\xef\xbb\xbf") else b"") + changed.encode("utf-8")
    if len(after) > budget or b"\x00" in after:
        raise OperationError("too_large" if len(after) > budget else "not_utf8")
    old_preview, new_preview = decode(before or b"")[:4096], decode(after)[:4096]
    preview = "".join(difflib.unified_diff(old_preview.splitlines(True), new_preview.splitlines(True),
                                        fromfile="before", tofile="after"))
    encoded = preview.encode("utf-8")
    truncated = len(decode(before or b"")) > 4096 or len(decode(after)) > 4096 or len(encoded) > diff_budget
    return after, {"path": path, "version": version(after), "bytes_written": len(after),
                   "created": before is None, "diff": encoded[:diff_budget].decode("utf-8", errors="ignore"),
                   "diff_truncated": truncated}


def open_directory(root: str, path: str, create: bool = False) -> int:
    if sys.platform == "win32":
        raise OperationError("unavailable")
    parts = [] if path == "." else path.split("/")
    if any(p in ("", ".", "..") for p in parts) or path.startswith("/") or "\\" in path:
        raise OperationError("permission_denied")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, dir_fd=descriptor)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except OSError as error:
                if error.errno == errno.ENOTDIR and stat.S_ISLNK(os.stat(part, dir_fd=descriptor, follow_symlinks=False).st_mode):
                    raise OperationError("symlink_denied") from error
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_at(directory: int, name: str, budget: int, check_only: bool = False) -> tuple[bytes, int]:
    if sys.platform == "win32":
        raise OperationError("unavailable")
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OperationError("not_file")
        if check_only:
            return b"", stat.S_IMODE(before.st_mode) & 0o777
        if before.st_size > budget:
            raise OperationError("too_large")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(budget + 1)
        after = os.fstat(descriptor)
        if len(data) > budget:
            raise OperationError("too_large", len(data))
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise OperationError("changed", len(data))
        return data, stat.S_IMODE(before.st_mode) & 0o777
    finally:
        os.close(descriptor)


def mutate(root: str, request: dict[str, object], budget: int, diff_budget: int) -> dict[str, object]:
    if sys.platform == "win32":
        raise OperationError("unavailable")
    operation, path = str(request["operation"]), str(request["path"])
    parent, _, name = path.rpartition("/")
    directory = open_directory(root, parent or ".", create=operation == "write")
    temporary = str(request["temporary"])
    if len(temporary) != 46 or not temporary.startswith(".pi-agent-") or not temporary.endswith(".tmp") or any(c not in "0123456789abcdef" for c in temporary[10:-4]):
        os.close(directory)
        raise OperationError("protocol_error")
    try:
        try:
            before, mode = read_at(directory, name, budget)
        except FileNotFoundError:
            before, mode = None, 0o600
        expected = request.get("expected_version")
        if expected is not None and not isinstance(expected, str):
            raise OperationError("protocol_error")
        data, result = prepare_mutation(operation, path, before, str(request.get("content", "")),
                                        str(request.get("old_text", "")), expected, budget, diff_budget)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if before is None:
            # Publish a complete new file without overwriting a concurrent creator.
            try:
                os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError as error:
                raise OperationError("conflict") from error
        else:
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        return result
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def main() -> None:
    root, budget, diff_budget = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    result: dict[str, object]
    try:
        payload = sys.stdin.buffer.read(16 * budget + 32_768)
        if len(payload) >= 16 * budget + 32_768:
            raise OperationError("too_large")
        request = json.loads(payload)
        if request.get("operation") == "cleanup":
            temporary = request["temporary"]
            if len(temporary) != 46 or not temporary.startswith(".pi-agent-") or not temporary.endswith(".tmp") or any(c not in "0123456789abcdef" for c in temporary[10:-4]):
                raise OperationError("protocol_error")
            parent, _, _ = request["path"].rpartition("/")
            try:
                descriptor = open_directory(root, parent or ".")
            except FileNotFoundError:
                descriptor = None
            if descriptor is not None:
                try:
                    try:
                        os.unlink(temporary, dir_fd=descriptor)
                    except FileNotFoundError:
                        pass
                finally:
                    os.close(descriptor)
            result = {"cleaned": True}
        else:
            result = mutate(root, request, budget, diff_budget)
    except OperationError as error:
        result = {"error": error.code}
    except OSError as error:
        result = {"error": {errno.ENOENT: "not_found", errno.ENOTDIR: "not_file", errno.ELOOP: "symlink_denied",
                            errno.EACCES: "permission_denied", errno.EPERM: "permission_denied"}.get(error.errno or 0, "outcome_unknown")}
    except Exception:
        result = {"error": "outcome_unknown"}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__" and "--mutation" in sys.argv:
    main()
