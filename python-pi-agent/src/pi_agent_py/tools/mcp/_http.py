"""Optional HTTP transport guards, applied before SDK JSON/SSE decoding."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

import httpx2

from ...errors import AgentError
from .contracts import (
    McpAuthenticationError,
    McpProtocolError,
    McpResultError,
    McpTransportError,
    _validate_http_target as validate_http_target,
)


__all__ = ["BoundedHttpTransport", "validate_http_target"]


class _BoundedStream(httpx2.AsyncByteStream):
    def __init__(
        self, stream: httpx2.AsyncByteStream, owner: BoundedHttpTransport
    ) -> None:
        self._stream = stream
        self._owner = owner

    async def __aiter__(self) -> AsyncIterator[bytes]:
        size = 0
        try:
            async for chunk in self._stream:
                size += len(chunk)
                if size > self._owner.max_bytes:
                    error = McpResultError(
                        "MCP HTTP response exceeded its receive budget"
                    )
                    self._owner.failure = error
                    raise httpx2.StreamError(
                        "MCP HTTP receive budget exceeded"
                    ) from error
                yield chunk
        except Exception as error:
            await self.aclose()
            if isinstance(error, httpx2.HTTPError) and not isinstance(
                error, httpx2.StreamError
            ):
                failure = McpTransportError("MCP HTTP response stream was interrupted")
                failure.__cause__ = error
                self._owner.failure = failure
                raise httpx2.StreamError(
                    "MCP HTTP response stream was interrupted"
                ) from error
            raise

    async def aclose(self) -> None:
        await self._stream.aclose()


class BoundedHttpTransport(httpx2.AsyncBaseTransport):
    """Owned transport: identity encoding, no redirects/resumption, bounded streams."""

    def __init__(self, transport: httpx2.AsyncBaseTransport, *, max_bytes: int) -> None:
        self._transport = transport
        self.max_bytes = max_bytes
        self.failure: AgentError | None = None

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if "last-event-id" in request.headers:
            error = McpTransportError("MCP HTTP stream resumption is disabled")
            return self._reject(request, error)
        request.headers["accept-encoding"] = "identity"
        try:
            response = await self._transport.handle_async_request(request)
        except httpx2.HTTPError as error:
            failure = McpTransportError("MCP HTTP request lost its transport")
            failure.__cause__ = error
            return self._reject(request, failure)
        try:
            if 300 <= response.status_code < 400:
                raise McpTransportError(
                    "MCP HTTP redirects require an explicitly reviewed target"
                )
            if response.status_code in {401, 403}:
                raise McpAuthenticationError(
                    "MCP HTTP authentication or authorization was rejected"
                )
            if response.headers.get("content-encoding", "identity").lower() not in {
                "",
                "identity",
            }:
                raise McpProtocolError("compressed MCP HTTP responses are not accepted")
            length = response.headers.get("content-length")
            if length is not None:
                try:
                    declared = int(length)
                except ValueError as error:
                    raise McpProtocolError("invalid MCP HTTP content length") from error
                if declared < 0 or declared > self.max_bytes:
                    raise McpResultError(
                        "MCP HTTP declared response exceeds receive budget"
                    )
        except AgentError as error:
            await response.aclose()
            return self._reject(request, error)
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            extensions=response.extensions,
            request=request,
            stream=_BoundedStream(cast(httpx2.AsyncByteStream, response.stream), self),
        )

    def _reject(self, request: httpx2.Request, error: AgentError) -> httpx2.Response:
        self.failure = error
        # SDK 2.3's POST tasks do not catch transport exceptions. Deliver a fixed
        # JSON-RPC error at HTTP 400; SDK binds a null error id to the original
        # request. No peer body, headers or diagnostic text enter this response.
        return httpx2.Response(
            400,
            request=request,
            json={
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32603,
                    "message": "MCP HTTP request rejected locally",
                },
            },
        )

    async def aclose(self) -> None:
        await self._transport.aclose()
