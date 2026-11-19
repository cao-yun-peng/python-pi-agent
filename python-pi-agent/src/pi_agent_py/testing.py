"""Deterministic test doubles for learning and contract tests."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import TypeAlias, Literal

from .domain import (
    Message,
    JSONObject,
    ModelStreamEvent,
    ProviderRequestContext,
    ToolDefinition,
)
from .errors import CompactionError, ProviderError
from .domain import WorkspaceReadLimits, WorkspaceReadRequest, WorkspaceReadResult
from .errors import WorkspaceFileError
from .tools.workspace.sandbox_backend import project_workspace_text
from .domain import WorkspaceWriteRequest, WorkspaceEditRequest, WorkspaceMutationResult
from .tools.workspace._file_ops import prepare_mutation, OperationError
from .tools.workspace._query_ops import memory_items, run_query
from .domain import WorkspaceQueryRequest, WorkspaceQueryLimits, WorkspaceQueryResult, WorkspaceEntry, WorkspaceMatch
from .tools.mcp.contracts import (
    McpCallResult, McpConnectionError, McpConnectionSpec, McpToolDescriptor,
    McpTransportError,
)


@dataclass(frozen=True, slots=True)
class WaitUntilCancelled:
    """A fake script step that blocks until its owning task is cancelled."""


FakeStep: TypeAlias = ModelStreamEvent | Exception | WaitUntilCancelled
FakeCompactionStep: TypeAlias = str | Exception | WaitUntilCancelled


class InMemoryWorkspaceFiles:
    """A per-instance byte store detached from the initial fixture catalog."""

    def __init__(self, files: Mapping[str, bytes], *, limits: WorkspaceReadLimits | None = None,
                 allow_write: bool = False, query_limits: WorkspaceQueryLimits | None = None) -> None:
        self._files = {WorkspaceReadRequest(path).path: bytes(data) for path, data in files.items()}
        self._limits = limits or WorkspaceReadLimits()
        self._mutation_lock = asyncio.Lock()
        self._allow_write = allow_write
        self._query_limits = query_limits or WorkspaceQueryLimits()

    async def query(self, request: WorkspaceQueryRequest) -> WorkspaceQueryResult:
        from dataclasses import asdict
        from typing import cast
        catalog = dict(self._files)
        if request.operation != "grep" and request.path in catalog:
            raise WorkspaceFileError("not_file")
        def read(name: str, budget: int) -> bytes:
            data = catalog[name]
            if len(data) > budget:
                raise OperationError("too_large")
            return data
        try:
            scan = {"count": 0}
            result = run_query(request.operation, request.path, request.pattern or "",
                memory_items(catalog, request.path, request.operation != "ls", self._query_limits.max_depth, self._query_limits.max_scan_entries, scan),
                read, asdict(self._query_limits), self._limits.max_file_bytes)
            result["scanned_entries"] = scan["count"]
        except OperationError as error:
            raise WorkspaceFileError(error.code) from error
        entries = tuple(WorkspaceEntry(str(row["path"]), cast("Literal['file', 'directory', 'symlink']", row["kind"])) for row in cast(list[dict[str, object]], result["entries"]))
        matches = tuple(WorkspaceMatch(str(row["path"]), cast(int, row["line"]), str(row["text"]), bool(row["truncated"])) for row in cast(list[dict[str, object]], result["matches"]))
        return WorkspaceQueryResult(entries, matches, bool(result["truncated"]), cast(int, result["scanned_entries"]), cast(int, result["skipped_files"]))

    async def write_text(self, request: WorkspaceWriteRequest) -> WorkspaceMutationResult:
        return await self._mutate("write", request.path, request.content, "", request.expected_version)

    async def edit_text(self, request: WorkspaceEditRequest) -> WorkspaceMutationResult:
        return await self._mutate("edit", request.path, request.new_text, request.old_text, request.expected_version)

    async def _mutate(self, operation: str, path: str, content: str, old: str, expected: str | None) -> WorkspaceMutationResult:
        if not self._allow_write:
            raise WorkspaceFileError("permission_denied")
        async with self._mutation_lock:
            if any(path.startswith(name + "/") or name.startswith(path + "/") for name in self._files):
                raise WorkspaceFileError("not_file")
            if max(len(content.encode("utf-8")), len(old.encode("utf-8"))) > self._limits.max_file_bytes:
                raise WorkspaceFileError("too_large")
            try:
                data, result = prepare_mutation(operation, path, self._files.get(path), content, old,
                    expected, self._limits.max_file_bytes, min(self._limits.max_output_bytes, 8192))
            except OperationError as error:
                raise WorkspaceFileError(error.code) from error
            self._files[path] = data
            return WorkspaceMutationResult(path, str(result["version"]), int(result["bytes_written"]),
                bool(result["created"]), str(result["diff"]), bool(result["diff_truncated"]))

    async def read_text(self, request: WorkspaceReadRequest) -> WorkspaceReadResult:
        try:
            data = self._files[request.path]
        except KeyError as error:
            raise WorkspaceFileError("not_found") from error
        return project_workspace_text(request, data, self._limits)


class FakeProvider:
    """Replays one deterministic script for each model request."""

    def __init__(self, responses: Iterable[Iterable[FakeStep]]) -> None:
        self._responses = deque(tuple(response) for response in responses)
        self.requests: list[tuple[Message, ...]] = []
        self.requested_tools: list[tuple[ToolDefinition, ...]] = []
        self.request_contexts: list[ProviderRequestContext] = []
        self.blocked = asyncio.Event()

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
        *,
        context: ProviderRequestContext,
    ) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(tuple(messages))
        self.requested_tools.append(tuple(tools))
        self.request_contexts.append(context)
        if not self._responses:
            raise ProviderError("fake provider has no scripted response left")

        for step in self._responses.popleft():
            if isinstance(step, WaitUntilCancelled):
                self.blocked.set()
                await asyncio.Event().wait()
                continue
            if isinstance(step, Exception):
                raise step
            yield step


class FakeCompactionStrategy:
    """Returns scripted summaries while recording the selected history."""

    def __init__(self, responses: Iterable[FakeCompactionStep]) -> None:
        self._responses = deque(responses)
        self.requests: list[tuple[Message, ...]] = []
        self.request_contexts: list[ProviderRequestContext] = []
        self.blocked = asyncio.Event()

    async def summarize(
        self,
        messages: Sequence[Message],
        *,
        context: ProviderRequestContext,
    ) -> str:
        self.requests.append(tuple(messages))
        self.request_contexts.append(context)
        if not self._responses:
            raise CompactionError(
                "fake compaction strategy has no scripted response left"
            )

        response = self._responses.popleft()
        if isinstance(response, WaitUntilCancelled):
            self.blocked.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable after cancellation")
        if isinstance(response, Exception):
            raise response
        return response


class FakeMcpEndpoint:
    """Normalized endpoint script, with no SDK, subprocess or network activity.

    Blocking scripts use Events; one cancelled call releases the local lock.
    Transport owner tasks, bounded queues and wire parsing belong to SDK tests.
    """

    protocol_version = "2025-11-25"

    def __init__(self, tools: Iterable[McpToolDescriptor],
                 responses: Iterable[McpCallResult | Exception | WaitUntilCancelled] = (), *,
                 list_error: Exception | WaitUntilCancelled | None = None) -> None:
        self._tools = tuple(tools)
        self._responses = deque(responses)
        self._list_error = list_error
        self._lock = asyncio.Lock()
        self.blocked = asyncio.Event()
        self.discovery_blocked = asyncio.Event()
        self.calls: list[tuple[str, JSONObject]] = []
        self.closed = False
        self.close_count = 0

    async def list_tools(self) -> tuple[McpToolDescriptor, ...]:
        if self.closed:
            raise McpConnectionError("fake MCP endpoint is closed")
        if isinstance(self._list_error, WaitUntilCancelled):
            self.discovery_blocked.set()
            await asyncio.Event().wait()
        if isinstance(self._list_error, Exception):
            raise self._list_error
        return self._tools

    async def call_tool(self, name: str, arguments: JSONObject) -> McpCallResult:
        async with self._lock:
            if self.closed:
                raise McpConnectionError("fake MCP endpoint is closed")
            self.calls.append((name, deepcopy(arguments)))
            if not self._responses:
                raise McpTransportError("fake MCP endpoint has no scripted result left")
            response = self._responses.popleft()
            if isinstance(response, WaitUntilCancelled):
                self.blocked.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable after cancellation")
            if isinstance(response, Exception):
                raise response
            return response

    async def aclose(self) -> None:
        if not self.closed:
            self.closed = True
            self.close_count += 1


class FakeMcpConnector:
    """Explicit per-server endpoints; no process-level state or automatic retries."""

    def __init__(self, endpoints: Mapping[str, FakeMcpEndpoint | Exception]) -> None:
        self._endpoints = dict(endpoints)
        self.opens: list[McpConnectionSpec] = []

    async def open(self, spec: McpConnectionSpec) -> FakeMcpEndpoint:
        self.opens.append(spec)
        endpoint = self._endpoints.get(spec.server_id)
        if endpoint is None:
            raise McpConnectionError("fake MCP server is not configured")
        if isinstance(endpoint, Exception):
            raise endpoint
        if endpoint.closed:
            raise McpConnectionError("fake MCP connector cannot reopen a closed endpoint")
        return endpoint
