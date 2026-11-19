"""Offline discovery -> policy -> MCP call -> transform -> durable continuation.

Run with pi-agent-py[jsonschema] installed. No MCP SDK or server is used.
"""

import asyncio

from pi_agent_py.api import (
    AgentEvent,
    AgentRuntimeBuilder,
    CallbackEventSink,
    DeclaredSideEffectGate,
    Finish,
    InMemorySessionStore,
    JsonSchemaValidator,
    Middleware,
    StaticApprovalPolicy,
    TextDelta,
    ToolCall,
    TruncateToolResults,
)
from pi_agent_py.testing import FakeMcpConnector, FakeMcpEndpoint, FakeProvider
from pi_agent_py.tools.mcp.contracts import (
    AllowedMcpConnections,
    McpCallResult,
    McpConnectionSpec,
    McpContent,
    McpServerSettings,
    McpToolDescriptor,
    McpTransportError,
)
from pi_agent_py.tools.mcp.plugin import mcp_plugin
from pi_agent_py.tools.mcp.tool import mcp_tool_name


async def main() -> None:
    spec = McpConnectionSpec("notes", "stdio", command="unused-fake-server")
    remote_name = "notes/search"
    endpoint = FakeMcpEndpoint(
        (
            McpToolDescriptor(
                remote_name,
                "Search the notes",
                {
                    "type": "object",
                    "properties": {"query": {"type": "string", "minLength": 1}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
            ),
        ),
        (
            McpCallResult(
                (McpContent("text", "Found matching notes. " * 12),),
                structured_content={"count": 2},
                has_structured_content=True,
            ),
            McpTransportError("This diagnostic is never copied into model output"),
        ),
    )
    name = mcp_tool_name(spec.server_id, remote_name)
    provider = FakeProvider(
        [
            [ToolCall("first", name, {"query": "harness"}), Finish("tool_calls")],
            [TextDelta("Found two notes."), Finish()],
            [ToolCall("second", name, {"query": "failure path"}), Finish("tool_calls")],
            [TextDelta("The outcome is unknown; ask before trying again."), Finish()],
        ]
    )
    validator, store = JsonSchemaValidator(), InMemorySessionStore()

    def trace(event: AgentEvent) -> None:
        if event.kind.startswith("mcp_") or event.kind in {
            "tool_approval_decided",
            "tool_result_transformed",
            "message_committed",
        }:
            print(event.kind, event.envelope.run_id, event.envelope.turn_id)

    runtime = await (
        AgentRuntimeBuilder()
        .with_provider(provider)
        .with_session_store(store)
        .with_schema_validator(validator)
        .with_approval_policy(StaticApprovalPolicy(True))
        .with_execution_gate(
            DeclaredSideEffectGate(
                allowed_side_effects=("external",), allowed_tools=(name,)
            )
        )
        .with_middleware((Middleware("truncate", tool_result=TruncateToolResults(96)),))
        .with_event_sink(CallbackEventSink(trace))
        .with_plugin(
            "mcp",
            mcp_plugin(
                (McpServerSettings(spec, (remote_name,)),),
                connector=FakeMcpConnector({spec.server_id: endpoint}),
                schema_validator=validator,
                connection_policy=AllowedMcpConnections((spec,)),
            ),
        )
        .build()
    )
    async with runtime:
        for prompt in ("Search the notes", "Exercise a transport failure"):
            result = await runtime.run("demo", prompt)
            print(result.message.content)
            print(
                "Committed tool result:",
                next(m.content for m in reversed(result.messages) if m.role == "tool"),
            )
    fork = await store.fork("demo", "offline-fork")
    print(
        "Remote calls / closes / fork messages:",
        len(endpoint.calls),
        endpoint.close_count,
        len(fork.messages),
    )


if __name__ == "__main__":
    asyncio.run(main())
