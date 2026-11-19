"""Deterministic test doubles for learning and contract tests."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import TypeAlias

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
    """A per-instance immutable bytes catalog using production page semantics."""

    def __init__(self, files: Mapping[str, bytes], *, limits: WorkspaceReadLimits | None = None) -> None:
        self._files = {WorkspaceReadRequest(path).path: bytes(data) for path, data in files.items()}
        self._limits = limits or WorkspaceReadLimits()

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
