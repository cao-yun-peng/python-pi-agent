"""One task owns a transport context; callers only enqueue cancellable work."""

from __future__ import annotations

import asyncio
import contextvars
import math
from collections import deque
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Literal, cast

from ...domain import JSONObject
from ...errors import ConfigurationError
from .contracts import (
    McpCallResult,
    McpConnectionError,
    McpEndpoint,
    McpProtocolError,
    McpRequestNotStartedError,
    McpToolDescriptor,
    McpTransportError,
)

ConnectionState = Literal["new", "opening", "ready", "closing", "closed", "broken"]
EndpointFactory = Callable[[], AbstractAsyncContextManager[McpEndpoint]]
Response = McpCallResult | tuple[McpToolDescriptor, ...]


@dataclass(eq=False, slots=True)
class _Request:
    name: str | None
    arguments: JSONObject
    future: asyncio.Future[Response]
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


class OwnedMcpEndpoint:
    """Serial SDK work, bounded pending queue, task-local cancellation and cleanup.

    The endpoint context enters and exits inside _run. No caller task exits an
    SDK cancel scope, including when build/run/close originate in different tasks.
    """

    def __init__(
        self,
        factory: EndpointFactory,
        *,
        max_pending_requests: int,
        request_timeout_seconds: float,
        cleanup_timeout_seconds: float,
    ) -> None:
        if (
            isinstance(max_pending_requests, bool)
            or max_pending_requests <= 0
            or any(
                not math.isfinite(v) or v < 0
                for v in (request_timeout_seconds, cleanup_timeout_seconds)
            )
            or cleanup_timeout_seconds == 0
        ):
            raise ConfigurationError("invalid MCP owner limits")
        self._factory = factory
        self._max_pending = max_pending_requests
        self._request_timeout = request_timeout_seconds
        self._cleanup_timeout = cleanup_timeout_seconds
        self._pending: deque[_Request] = deque()
        self._wake = asyncio.Event()
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._active: _Request | None = None
        self._state: ConnectionState = "new"
        self._protocol_version = ""
        self._owner: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None
        self._close_failure: BaseException | None = None

    @classmethod
    async def open(
        cls,
        factory: EndpointFactory,
        *,
        max_pending_requests: int = 64,
        open_timeout_seconds: float = 10,
        request_timeout_seconds: float = 30,
        cleanup_timeout_seconds: float = 8,
    ) -> OwnedMcpEndpoint:
        if not math.isfinite(open_timeout_seconds) or open_timeout_seconds < 0:
            raise ConfigurationError("invalid MCP startup deadline")
        endpoint = cls(
            factory,
            max_pending_requests=max_pending_requests,
            request_timeout_seconds=request_timeout_seconds,
            cleanup_timeout_seconds=cleanup_timeout_seconds,
        )
        endpoint._state = "opening"
        # Do not inherit whichever Session ContextVar happened to initiate build.
        endpoint._owner = asyncio.create_task(
            endpoint._run(), name="mcp-owner", context=contextvars.Context()
        )
        try:
            async with asyncio.timeout(open_timeout_seconds):
                await asyncio.shield(endpoint._ready)
            return endpoint
        except BaseException:
            endpoint._ready.cancel()
            endpoint._owner.cancel()
            await endpoint.aclose()
            raise

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def protocol_version(self) -> str:
        return self._protocol_version

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def _invoke(self, backend: McpEndpoint, request: _Request) -> None:
        try:
            if request.name is None:
                response: Response = await backend.list_tools()
            else:
                response = await backend.call_tool(request.name, request.arguments)
            if not request.future.done():
                request.future.set_result(response)
        except asyncio.CancelledError:
            request.future.cancel()
        except Exception as error:
            if isinstance(
                error, (McpConnectionError, McpProtocolError, McpTransportError)
            ):
                self._state = "broken"
                self._failure = error
            if not request.future.done():
                request.future.set_exception(error)
        finally:
            request.done.set()

    async def _run(self) -> None:
        try:
            async with self._factory() as backend:
                self._protocol_version = backend.protocol_version
                self._state = "ready"
                self._ready.set_result(None)
                async with asyncio.TaskGroup() as group:
                    while self._state == "ready":
                        await self._wake.wait()
                        if self._state != "ready":
                            break
                        if not self._pending:
                            self._wake.clear()
                            continue
                        request = self._pending.popleft()
                        self._active = request
                        request.task = group.create_task(self._invoke(backend, request))
                        try:
                            await request.done.wait()
                        finally:
                            if not request.done.is_set():
                                request.task.cancel()
                            self._active = None
        except BaseException as error:
            if self._ready.done() and not self._ready.cancelled():
                self._close_failure = error
            self._failure = error
            if self._state not in {"closing", "closed"}:
                self._state = "broken"
        finally:
            if not self._ready.done():
                startup_error = McpConnectionError("MCP owner could not start")
                startup_error.__cause__ = self._failure
                self._ready.set_exception(startup_error)
            while self._pending:
                request = self._pending.popleft()
                if not request.future.done():
                    request.future.set_exception(
                        McpRequestNotStartedError("MCP owner is unavailable")
                    )
                request.done.set()
            if self._state != "broken":
                self._state = "closed"

    async def _submit(self, name: str | None, arguments: JSONObject) -> Response:
        if self._state != "ready" or len(self._pending) >= self._max_pending:
            raise McpRequestNotStartedError(
                "MCP owner is unavailable or its pending queue is full"
            )
        request = _Request(name, arguments, asyncio.get_running_loop().create_future())
        self._pending.append(request)
        self._wake.set()
        try:
            async with asyncio.timeout(self._request_timeout):
                return await asyncio.shield(request.future)
        except (asyncio.CancelledError, TimeoutError):
            if request.done.is_set():
                pass
            elif request.task is None:
                self._pending.remove(request)
                request.done.set()
            else:
                request.task.cancel()
            request.future.cancel()
            try:
                async with asyncio.timeout(self._cleanup_timeout):
                    await asyncio.shield(request.done.wait())
            except TimeoutError:
                self._state = "broken"
                self._wake.set()
            raise
        finally:
            if request.future.done() and not request.future.cancelled():
                request.future.exception()  # Observe a result racing with caller cancellation.

    async def list_tools(self) -> tuple[McpToolDescriptor, ...]:
        return cast(tuple[McpToolDescriptor, ...], await self._submit(None, {}))

    async def call_tool(self, name: str, arguments: JSONObject) -> McpCallResult:
        from copy import deepcopy

        return cast(McpCallResult, await self._submit(name, deepcopy(arguments)))

    async def aclose(self) -> None:
        owner = self._owner
        if owner is None or self._state == "closed":
            return
        self._state = "closing"
        self._wake.set()
        if self._active is not None and self._active.task is not None:
            self._active.task.cancel()
        try:
            async with asyncio.timeout(self._cleanup_timeout):
                await asyncio.shield(owner)
        except TimeoutError as error:
            owner.cancel()
            async with asyncio.timeout(self._cleanup_timeout):
                await asyncio.shield(owner)
            raise McpConnectionError(
                "MCP owner cleanup exceeded its deadline"
            ) from error
        self._state = "closed"
        if self._close_failure is not None:
            raise McpConnectionError(
                "MCP owner resource cleanup failed"
            ) from self._close_failure
