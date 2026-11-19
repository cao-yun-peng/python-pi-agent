"""Bounded query semantics shared by the memory fixture and isolated helper."""

import fnmatch
import json
import os
import posixpath
import stat
import sys
from collections.abc import Callable, Iterable, Iterator

if __package__:
    from ._file_ops import OperationError, decode, open_directory, read_at

EXCLUDED_DIRECTORIES = frozenset((".git", ".venv", "node_modules", "__pycache__"))


def match_glob(name: str, path: str, pattern: str) -> bool:
    if "/" not in pattern:
        return fnmatch.fnmatchcase(posixpath.basename(name), pattern)
    parts, patterns = posixpath.relpath(name, path).split("/"), pattern.split("/")
    def closure(states: set[int]) -> set[int]:
        result = set(states)
        for index in range(len(patterns)):
            if index in result and patterns[index] == "**":
                result.add(index + 1)
        return result
    states = {0}
    for part in parts:
        following = set()
        for index in closure(states):
            if index < len(patterns):
                if patterns[index] == "**":
                    following.add(index)
                elif fnmatch.fnmatchcase(part, patterns[index]):
                    following.add(index + 1)
        states = following
    return len(patterns) in closure(states)


def run_query(
    operation: str, path: str, pattern: str, items: Iterable[tuple[str, str]],
    read: Callable[[str, int], bytes], limits: dict[str, int], file_budget: int,
) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    matches: list[dict[str, object]] = []
    scanned = skipped = scanned_bytes = output_bytes = 0
    truncated = False
    iterator = iter(items)
    try:
        for name, kind in iterator:
            if kind == "truncated":
                truncated = True
                continue
            if scanned >= limits["max_scan_entries"]:
                truncated = True
                break
            scanned += 1
            rows: list[dict[str, object]] = []
            if operation == "ls":
                rows.append({"path": name, "kind": kind})
            elif kind == "file" and operation == "find":
                if match_glob(name, path, pattern):
                    rows.append({"path": name, "kind": kind})
            elif kind == "file" and operation == "grep":
                remaining = limits["max_scan_bytes"] - scanned_bytes
                if remaining <= 0:
                    truncated = True
                    break
                try:
                    data = read(name, min(file_budget, remaining))
                    scanned_bytes += len(data)
                    text = decode(data)
                except (OSError, OperationError) as error:
                    skipped += 1
                    if isinstance(error, OSError) or error.consumed:
                        # A failed in-flight read may have consumed its full allowance.
                        truncated = True
                        break
                    if isinstance(error, OperationError) and error.code == "too_large" and remaining < file_budget:
                        truncated = True
                        break
                    continue
                # Stop collecting this file as soon as its bounded match quota is reached.
                for number, line in enumerate(text.splitlines(), 1):
                    if pattern in line:
                        rows.append({"path": name, "line": number,
                            "text": line[:limits["max_match_chars"]],
                            "truncated": len(line) > limits["max_match_chars"]})
                        if len(rows) + len(matches) > limits["max_entries"]:
                            break
            destination = matches if operation == "grep" else entries
            limit_hit = False
            for row in rows:
                size = len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 1
                if len(destination) >= limits["max_entries"] or output_bytes + size > limits["max_output_bytes"]:
                    truncated = True
                    limit_hit = True
                    break
                destination.append(row)
                output_bytes += size
            if limit_hit:
                break
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()
    return {"entries": entries, "matches": matches, "truncated": truncated,
            "scanned_entries": scanned, "skipped_files": skipped}


def disk_items(root: str, path: str, recursive: bool, limits: dict[str, int], depth: int = 0,
               scan: dict[str, int] | None = None) -> Iterator[tuple[str, str]]:
    if scan is None:
        scan = {"count": 0}
    descriptor = open_directory(root, path)
    try:
        names = []
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                if scan["count"] >= limits["max_scan_entries"]:
                    if depth == 0:
                        raise OperationError("scan_limit")
                    yield path, "truncated"
                    return
                names.append(entry.name)
                scan["count"] += 1
        for name in sorted(names):
            relative = name if path == "." else path + "/" + name
            try:
                usable = bool(relative.strip()) and len(relative.encode("utf-8")) <= 4096 and not relative.startswith("~") and ":" not in relative and "\\" not in relative
            except UnicodeError:
                usable = False
            if not usable:
                yield "", "truncated"
                continue
            try:
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            kind = "symlink" if stat.S_ISLNK(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "other"
            if kind == "other":
                continue
            yield relative, kind
            if recursive and kind == "directory" and name not in EXCLUDED_DIRECTORIES:
                if depth + 1 >= limits["max_depth"]:
                    yield relative, "truncated"
                else:
                    try:
                        yield from disk_items(root, relative, True, limits, depth + 1, scan)
                    except (OSError, OperationError):
                        yield relative, "truncated"
    finally:
        os.close(descriptor)


def memory_items(names: Iterable[str], path: str, recursive: bool, max_depth: int, max_scan_entries: int,
                 scan: dict[str, int]) -> Iterator[tuple[str, str]]:
    tree: dict[str, str] = {}
    for name in names:
        tree[name] = "file"
        parts = name.split("/")
        for index in range(1, len(parts)):
            tree["/".join(parts[:index])] = "directory"
    if path != "." and path not in tree:
        raise OperationError("not_found")
    if tree.get(path) == "file":
        scan["count"] += 1
        yield path, "file"
        return
    def walk(directory: str, depth: int) -> Iterator[tuple[str, str]]:
        prefix = "" if directory == "." else directory + "/"
        children = sorted(n for n in tree if n.startswith(prefix) and "/" not in n[len(prefix):])
        if scan["count"] + len(children) > max_scan_entries:
            if depth == 0:
                raise OperationError("scan_limit")
            yield directory, "truncated"
            return
        scan["count"] += len(children)
        for name in children:
            kind = tree[name]
            yield name, kind
            if recursive and kind == "directory" and posixpath.basename(name) not in EXCLUDED_DIRECTORIES:
                if depth + 1 >= max_depth:
                    yield name, "truncated"
                else:
                    yield from walk(name, depth + 1)
    yield from walk(path, 0)


def main() -> None:
    root, file_budget = sys.argv[1], int(sys.argv[2])
    try:
        payload = sys.stdin.buffer.read(65_536)
        if len(payload) >= 65_536:
            raise OperationError("protocol_error")
        request = json.loads(payload)
        operation, path = request["operation"], request["path"]
        limits = request["limits"]
        scan = {"count": 0}
        def read(name: str, budget: int) -> bytes:
            parent, _, filename = name.rpartition("/")
            descriptor = open_directory(root, parent or ".")
            try:
                return read_at(descriptor, filename, budget)[0]
            finally:
                os.close(descriptor)
        def items() -> Iterator[tuple[str, str]]:
            try:
                yield from disk_items(root, path, operation != "ls", limits, scan=scan)
            except NotADirectoryError:
                if operation != "grep":
                    raise OperationError("not_file")
                # Probe kind without reading bytes outside the total scan budget.
                parent, _, filename = path.rpartition("/")
                descriptor = open_directory(root, parent or ".")
                try:
                    read_at(descriptor, filename, file_budget, check_only=True)
                finally:
                    os.close(descriptor)
                scan["count"] += 1
                yield path, "file"
        result = run_query(operation, path, request.get("pattern") or "", items(), read, limits, file_budget)
        result["scanned_entries"] = scan["count"]
    except OperationError as error:
        result = {"error": error.code}
    except OSError as error:
        import errno
        result = {"error": {errno.ENOENT: "not_found", errno.ELOOP: "symlink_denied", errno.ENOTDIR: "not_file",
                            errno.EACCES: "permission_denied", errno.EPERM: "permission_denied"}.get(error.errno or 0, "unavailable")}
    except Exception:
        result = {"error": "unavailable"}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
