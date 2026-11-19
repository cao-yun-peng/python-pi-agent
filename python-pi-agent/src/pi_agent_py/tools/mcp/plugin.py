"""Transactional discovery: borrow observation, own endpoints, stage Tools."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from uuid import uuid4

from ...domain import EventKind
from ...errors import ConfigurationError
from ...observability.events import make_agent_event
from ...plugins import PluginAPI, PluginFactory
from ...ports import SchemaValidator
from .contracts import (
    AllowedMcpConnections,
    McpConnectionError,
    McpConnectionPolicy,
    McpConnector,
    McpEndpoint,
    McpProtocolError,
    McpServerSettings,
    _json,
)
from .tool import McpTool


def mcp_plugin(
    servers: Sequence[McpServerSettings],
    *,
    connector: McpConnector,
    schema_validator: SchemaValidator,
    connection_policy: McpConnectionPolicy | None = None,
) -> PluginFactory:
    """All selected servers are required. Host rollback closes in reverse order."""
    settings = tuple(servers)
    if (
        not settings
        or len(settings) > 16
        or len({s.spec.server_id for s in settings}) != len(settings)
    ):
        raise ConfigurationError("MCP requires 1..16 uniquely named servers")
    policy = (
        connection_policy if connection_policy is not None else AllowedMcpConnections()
    )

    async def setup(api: PluginAPI) -> None:
        # Validate the entire connection policy before the first connection.
        for server in settings:
            policy.check(server.spec)
        names: set[str] = set()
        for server in settings:
            span = uuid4().hex
            started = time.monotonic()

            def emit(
                kind: EventKind,
                *,
                outcome: str | None = None,
                error: BaseException | None = None,
                count: int | None = None,
            ) -> None:
                api.event_sink.emit(
                    make_agent_event(
                        kind,
                        "capability",
                        source="mcp",
                        span_id=span,
                        duration_ms=None
                        if outcome is None
                        else (time.monotonic() - started) * 1000,
                        payload={
                            "server_id": server.spec.server_id,
                            "outcome": outcome,
                            "error_type": None
                            if error is None
                            else type(error).__name__,
                            "tool_count": count,
                        },
                    )
                )

            emit("mcp_connection_started")
            try:
                async with asyncio.timeout(server.connection_timeout_seconds):
                    endpoint = await connector.open(server.spec)

                    # Bind loop variables now: rollback may run after another server opens.
                    async def close(
                        endpoint: McpEndpoint = endpoint,
                        server_id: str = server.spec.server_id,
                        grace: float = server.cleanup_grace_seconds,
                    ) -> None:
                        async with asyncio.timeout(grace):
                            await endpoint.aclose()
                        api.event_sink.emit(
                            make_agent_event(
                                "mcp_connection_closed",
                                "capability",
                                source="mcp",
                                payload={"server_id": server_id},
                            )
                        )

                    api.register_cleanup(close)
                    descriptors = await endpoint.list_tools()
                    if len(descriptors) > server.limits.max_discovered_tools:
                        raise McpProtocolError(
                            "MCP discovery exceeds tool count budget"
                        )
                    try:
                        _json(
                            [
                                {
                                    "name": d.remote_name,
                                    "description": d.description,
                                    "input": dict(d.input_schema),
                                    "output": None
                                    if d.output_schema is None
                                    else dict(d.output_schema),
                                    "annotations": dict(d.annotations),
                                }
                                for d in descriptors
                            ],
                            max_bytes=server.limits.max_catalog_bytes,
                            max_depth=server.limits.max_depth + 2,
                        )
                    except (ValueError, TypeError, RecursionError) as error:
                        raise McpProtocolError(
                            "MCP discovery exceeds JSON budgets"
                        ) from error
                    discovered = {d.remote_name: d for d in descriptors}
                    if len(discovered) != len(descriptors):
                        raise McpProtocolError("duplicate MCP remote tool name")
                    if not set(server.allow_tools) <= discovered.keys():
                        raise McpProtocolError("selected MCP tool was not discovered")
                    for remote_name in server.allow_tools:
                        tool = McpTool(
                            server,
                            discovered[remote_name],
                            endpoint,
                            schema_validator,
                            event_sink=api.event_sink,
                        )
                        if tool.definition.name in names:
                            raise McpProtocolError("MCP local tool name conflict")
                        names.add(tool.definition.name)
                        api.register_tool(tool)
                    emit("mcp_tools_discovered", count=len(descriptors))
            except asyncio.CancelledError:
                emit("mcp_connection_cancelled", outcome="cancelled")
                raise
            except Exception as error:
                emit("mcp_connection_failed", outcome="error", error=error)
                raise McpConnectionError("MCP initialization failed") from error
            emit("mcp_connection_ready", outcome="success")

    return setup
