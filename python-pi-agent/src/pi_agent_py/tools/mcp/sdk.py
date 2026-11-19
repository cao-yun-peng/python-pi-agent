"""Pinned optional SDK boundary. Importing this module opens no transport."""

from __future__ import annotations

import json
import math
import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Literal, cast

from ...domain import JSONObject, JSONValue
from ...errors import AgentError, ConfigurationError
from ._logging import sdk_log_privacy
from .connection import OwnedMcpEndpoint
from .contracts import (
    McpCallResult,
    McpConnectionError,
    McpConnectionSpec,
    McpContent,
    McpEndpoint,
    McpLimits,
    McpProtocolError,
    McpRequestNotStartedError,
    McpResultError,
    McpToolDescriptor,
    McpTransportError,
    _json,
)

if TYPE_CHECKING:
    from mcp.client.session import ClientSession, IncomingMessage


@dataclass(frozen=True, slots=True)
class McpSdkOptions:
    mode: Literal["auto", "legacy", "2026-07-28"] = "auto"
    limits: McpLimits = field(default_factory=McpLimits)
    max_discovery_pages: int = 100
    max_pending_requests: int = 64
    open_timeout_seconds: float = 10
    request_timeout_seconds: float = 30
    cleanup_timeout_seconds: float = 8
    allow_insecure_local: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.allow_insecure_local, bool):
            raise ConfigurationError(
                "MCP loopback development permission must be boolean"
            )
        if self.mode not in {"auto", "legacy", "2026-07-28"}:
            raise ConfigurationError("unsupported MCP SDK negotiation mode")
        for value in (self.max_discovery_pages, self.max_pending_requests):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ConfigurationError(
                    "MCP SDK queue/page bounds must be positive integers"
                )
        for deadline in (
            self.open_timeout_seconds,
            self.request_timeout_seconds,
            self.cleanup_timeout_seconds,
        ):
            if (
                isinstance(deadline, bool)
                or not math.isfinite(deadline)
                or deadline < 0
            ):
                raise ConfigurationError(
                    "MCP SDK deadlines must be finite and nonnegative"
                )
        if self.cleanup_timeout_seconds == 0:
            raise ConfigurationError("MCP SDK cleanup deadline must be positive")


class _SdkEndpoint:
    def __init__(
        self,
        session: ClientSession,
        options: McpSdkOptions,
        *,
        transport_failure: Callable[[], AgentError | None] | None = None,
    ) -> None:
        self._session = session
        self._options = options
        self._invalid = False
        self._transport_failure = transport_failure
        self.protocol_version = session.protocol_version or ""

    async def on_message(self, message: IncomingMessage) -> None:
        from mcp_types import ToolListChangedNotification

        if isinstance(message, (ToolListChangedNotification, Exception)):
            self._invalid = True

    def _check(self) -> None:
        if self._invalid or (
            self._transport_failure is not None
            and self._transport_failure() is not None
        ):
            raise McpRequestNotStartedError("MCP connection requires rebuilding")

    def _raise_transport_failure(self, cause: Exception | None = None) -> None:
        if self._transport_failure is not None:
            failure = self._transport_failure()
            if failure is not None:
                self._invalid = True
                if failure is cause:
                    raise failure
                raise failure from cause

    async def list_tools(self) -> tuple[McpToolDescriptor, ...]:
        from mcp_types import ListToolsRequest, ListToolsResult, PaginatedRequestParams

        self._check()
        cursor: str | None = None
        cursors: set[str] = set()
        names: set[str] = set()
        tools: list[McpToolDescriptor] = []
        remaining = self._options.limits.max_catalog_bytes
        try:
            for _ in range(self._options.max_discovery_pages):
                # send_request avoids SDK listing mutation/header maps and output-schema caches.
                page = await self._session.send_request(
                    ListToolsRequest(
                        params=None
                        if cursor is None
                        else PaginatedRequestParams(cursor=cursor)
                    ),
                    ListToolsResult,
                )
                encoded = _json(
                    page.model_dump(mode="json", by_alias=True, exclude_none=True),
                    max_bytes=remaining,
                    max_depth=self._options.limits.max_depth + 3,
                )
                remaining -= len(encoded.encode("utf-8"))
                if (
                    len(tools) + len(page.tools)
                    > self._options.limits.max_discovered_tools
                ):
                    raise McpProtocolError("MCP discovered tool count exceeded")
                for item in page.tools:
                    if item.name in names:
                        raise McpProtocolError(
                            "duplicate MCP tool across discovery pages"
                        )
                    names.add(item.name)
                    for schema in (item.input_schema, item.output_schema):
                        if schema is not None:
                            _json(
                                schema,
                                max_bytes=self._options.limits.max_schema_bytes,
                                max_depth=self._options.limits.max_depth,
                            )
                    tools.append(
                        McpToolDescriptor(
                            item.name,
                            item.description or "",
                            item.input_schema,
                            item.output_schema,
                            {}
                            if item.annotations is None
                            else item.annotations.model_dump(
                                mode="json", by_alias=True, exclude_none=True
                            ),
                        )
                    )
                cursor = page.next_cursor
                if cursor is None:
                    if self._invalid:
                        raise McpProtocolError("MCP catalog changed during discovery")
                    return tuple(tools)
                if not cursor or cursor in cursors:
                    raise McpProtocolError("MCP discovery cursor repeated or empty")
                cursors.add(cursor)
            raise McpProtocolError("MCP discovery page limit exceeded")
        except (McpProtocolError, McpRequestNotStartedError):
            self._invalid = True
            raise
        except Exception as error:
            self._raise_transport_failure(error)
            self._invalid = True
            raise McpProtocolError("MCP discovery failed") from error

    async def call_tool(self, name: str, arguments: JSONObject) -> McpCallResult:
        from mcp.shared.exceptions import MCPError
        from mcp_types import (
            CallToolRequest,
            CallToolRequestParams,
            CallToolResult,
            InputRequiredResult,
        )
        from pydantic import TypeAdapter, ValidationError

        self._check()
        try:
            result = await self._session.send_request(
                CallToolRequest(
                    params=CallToolRequestParams(name=name, arguments=arguments)
                ),
                TypeAdapter(CallToolResult | InputRequiredResult),
            )
            self._raise_transport_failure()
            if self._invalid:
                raise McpProtocolError(
                    "MCP catalog or transport changed during invocation"
                )
            if (
                isinstance(result, InputRequiredResult)
                or result.result_type == "input_required"
            ):
                return McpCallResult(result_type="input_required")
            _json(
                result.model_dump(mode="json", by_alias=True, exclude_none=True),
                max_bytes=self._options.limits.max_result_bytes,
                max_depth=self._options.limits.max_depth + 3,
            )
            # Presence is read before exclude_none normalization; explicit null remains distinct.
            present = "structured_content" in result.model_fields_set
            content: list[McpContent] = []
            for block in result.content:
                if block.type == "text":
                    content.append(McpContent("text", block.text))
                else:
                    kind = (
                        block.type
                        if block.type in {"image", "audio", "resource", "resource_link"}
                        else "unsupported"
                    )
                    content.append(McpContent(kind))
            value = cast(
                JSONValue,
                json.loads(
                    _json(
                        result.structured_content,
                        max_bytes=self._options.limits.max_structured_bytes,
                        max_depth=self._options.limits.max_depth,
                    )
                ),
            )
            return McpCallResult(
                tuple(content),
                is_error=result.is_error,
                structured_content=value,
                has_structured_content=present,
            )
        except McpProtocolError:
            self._invalid = True
            raise
        except MCPError as error:
            self._raise_transport_failure(error)
            self._invalid = True
            raise McpProtocolError(
                "MCP request failed at the protocol boundary"
            ) from error
        except (ValidationError, ValueError, TypeError, RecursionError) as error:
            raise McpResultError("MCP result could not be normalized") from error
        except Exception as error:
            self._raise_transport_failure(error)
            self._invalid = True
            raise McpTransportError("MCP transport failed during invocation") from error

    async def aclose(self) -> None:
        self._invalid = True  # The enclosing SDK context owns resource release.


@asynccontextmanager
async def _open_sdk(
    spec: McpConnectionSpec, options: McpSdkOptions
) -> AsyncIterator[McpEndpoint]:
    try:
        if version("mcp") != "2.3.0":
            raise ConfigurationError(
                "MCP adapter requires the verified SDK version 2.3.0"
            )
        from mcp import Client
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except (ImportError, PackageNotFoundError) as error:
        raise ConfigurationError(
            "install pi-agent-py[mcp] to use the SDK connector"
        ) from error
    transport_failure: Callable[[], AgentError | None] | None = None
    try:
        with sdk_log_privacy(), open(os.devnull, "w", encoding="utf-8") as stderr:
            async with AsyncExitStack() as stack:
                if spec.transport == "stdio":
                    transport = stdio_client(
                        StdioServerParameters(
                            command=spec.command or "",
                            args=list(spec.args),
                            cwd=spec.cwd,
                            env=dict(spec.env),
                        ),
                        errlog=stderr,
                    )
                else:
                    import httpx2
                    from mcp.client.streamable_http import streamable_http_client
                    from ._http import BoundedHttpTransport, validate_http_target

                    validate_http_target(
                        spec, allow_insecure_local=options.allow_insecure_local
                    )
                    guarded = BoundedHttpTransport(
                        httpx2.AsyncHTTPTransport(
                            verify=True, trust_env=False, retries=0
                        ),
                        max_bytes=options.limits.max_result_bytes,
                    )

                    def transport_failure() -> AgentError | None:
                        return guarded.failure

                    client_http = await stack.enter_async_context(
                        httpx2.AsyncClient(
                            transport=guarded,
                            headers=dict(spec.headers),
                            trust_env=False,
                            follow_redirects=False,
                            timeout=httpx2.Timeout(options.request_timeout_seconds),
                        )
                    )
                    transport = streamable_http_client(
                        spec.url or "",
                        http_client=client_http,
                        max_sse_event_size=options.limits.max_result_bytes,
                    )
                endpoint: _SdkEndpoint | None = None

                async def message_handler(message: IncomingMessage) -> None:
                    if endpoint is not None:
                        await endpoint.on_message(message)

                client = await stack.enter_async_context(
                    Client(
                        transport,
                        mode="legacy" if options.mode == "legacy" else "auto",
                        cache=None,
                        message_handler=message_handler,
                    )
                )
                capabilities = client.session.server_capabilities
                if (
                    options.mode == "2026-07-28"
                    and client.session.protocol_version != options.mode
                ):
                    raise McpProtocolError(
                        "MCP server did not negotiate the required protocol version"
                    )
                if capabilities is None or capabilities.tools is None:
                    raise McpProtocolError(
                        "MCP server does not advertise tools capability"
                    )
                endpoint = _SdkEndpoint(
                    client.session, options, transport_failure=transport_failure
                )
                try:
                    yield endpoint
                finally:
                    await endpoint.aclose()
    except (ConfigurationError, McpProtocolError, McpTransportError):
        raise
    except Exception as error:
        failure = None if transport_failure is None else transport_failure()
        if failure is not None and failure is not error:
            raise failure from error
        raise McpConnectionError("MCP SDK connection lifecycle failed") from error


class SdkMcpConnector:
    """One new owned SDK context per open; no automatic reconnect or replay."""

    def __init__(
        self,
        options: McpSdkOptions | None = None,
        *,
        per_server: Mapping[str, McpSdkOptions] | None = None,
    ) -> None:
        self._options = options if options is not None else McpSdkOptions()
        self._per_server = dict(per_server or {})

    async def open(self, spec: McpConnectionSpec) -> OwnedMcpEndpoint:
        options = self._per_server.get(spec.server_id, self._options)
        return await OwnedMcpEndpoint.open(
            lambda: _open_sdk(spec, options),
            max_pending_requests=options.max_pending_requests,
            open_timeout_seconds=options.open_timeout_seconds,
            request_timeout_seconds=options.request_timeout_seconds,
            cleanup_timeout_seconds=options.cleanup_timeout_seconds,
        )
