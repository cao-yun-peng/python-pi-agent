"""Real trusted local stdio server + FakeProvider; install pi-agent-py[mcp]."""

import asyncio
import sys
from pathlib import Path

from pi_agent_py.api import (
    AgentEvent,
    AgentRuntimeBuilder,
    CallbackEventSink,
    DeclaredSideEffectGate,
    Finish,
    JsonSchemaValidator,
    StaticApprovalPolicy,
    TextDelta,
    ToolCall,
)
from pi_agent_py.testing import FakeProvider
from pi_agent_py.tools.mcp.contracts import (
    AllowedMcpConnections,
    McpConnectionSpec,
    McpServerSettings,
)
from pi_agent_py.tools.mcp.plugin import mcp_plugin
from pi_agent_py.tools.mcp.sdk import SdkMcpConnector
from pi_agent_py.tools.mcp.tool import mcp_tool_name


async def main() -> None:
    server = Path(__file__).with_name("_mcp_demo_server.py").resolve()
    spec = McpConnectionSpec(
        "local", "stdio", command=sys.executable, args=("-B", str(server))
    )
    name = mcp_tool_name("local", "echo")
    validator = JsonSchemaValidator()
    provider = FakeProvider(
        [
            [
                ToolCall(
                    "echo-once", name, {"value": "Hello from the local MCP process"}
                ),
                Finish("tool_calls"),
            ],
            [TextDelta("The local tool result is committed."), Finish()],
        ]
    )

    def trace(event: AgentEvent) -> None:
        if event.kind.startswith("mcp_"):
            print(event.kind, event.envelope.run_id)

    runtime = await (
        AgentRuntimeBuilder()
        .with_provider(provider)
        .with_schema_validator(validator)
        .with_approval_policy(StaticApprovalPolicy(True))
        .with_execution_gate(
            DeclaredSideEffectGate(
                allowed_side_effects=("external",), allowed_tools=(name,)
            )
        )
        .with_event_sink(CallbackEventSink(trace))
        .with_plugin(
            "mcp",
            mcp_plugin(
                (McpServerSettings(spec, ("echo",)),),
                connector=SdkMcpConnector(),
                schema_validator=validator,
                connection_policy=AllowedMcpConnections((spec,)),
            ),
        )
        .build()
    )
    async with runtime:
        result = await runtime.run("stdio-demo", "Call the trusted local fixture")
        print(result.messages[2].content)
        print(result.message.content)


if __name__ == "__main__":
    asyncio.run(main())
