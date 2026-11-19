"""One-command loopback HTTP demo: official SDK server + production client."""

import asyncio
import socket
from contextlib import asynccontextmanager

import uvicorn
from _mcp_demo_server import server as fixture

from pi_agent_py.api import (
    AgentRuntimeBuilder,
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
from pi_agent_py.tools.mcp.sdk import McpSdkOptions, SdkMcpConnector
from pi_agent_py.tools.mcp.tool import mcp_tool_name


@asynccontextmanager
async def local_server():
    sdk_app = fixture.streamable_http_app(json_response=False)

    async def app(scope, receive, send):
        if (
            scope["type"] == "http"
            and dict(scope["headers"]).get(b"authorization") != b"fixture-only"
        ):
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        await sdk_app(scope, receive, send)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        server = uvicorn.Server(
            uvicorn.Config(
                app, log_config=None, access_log=False, timeout_graceful_shutdown=1
            )
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("local demo server did not start")
                    await asyncio.sleep(0)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
        finally:
            server.should_exit = True
            await task


async def main() -> None:
    async with local_server() as url:
        spec = McpConnectionSpec(
            "local",
            "streamable_http",
            url=url,
            headers={"Authorization": "fixture-only"},
        )
        validator = JsonSchemaValidator()
        provider = FakeProvider(
            [
                [
                    ToolCall(
                        "c",
                        mcp_tool_name("local", "echo"),
                        {"value": "Local SSE result"},
                    ),
                    Finish("tool_calls"),
                ],
                [TextDelta("The HTTP tool result is committed."), Finish()],
            ]
        )
        async with await AgentRuntimeBuilder().with_provider(
            provider
        ).with_schema_validator(validator).with_approval_policy(
            StaticApprovalPolicy(True)
        ).with_execution_gate(
            DeclaredSideEffectGate(allowed_side_effects=("external",))
        ).with_plugin(
            "mcp",
            mcp_plugin(
                (McpServerSettings(spec, ("echo",)),),
                connector=SdkMcpConnector(McpSdkOptions(allow_insecure_local=True)),
                schema_validator=validator,
                connection_policy=AllowedMcpConnections((spec,)),
            ),
        ).build() as runtime:
            result = await runtime.run("http-demo", "Call the local fixture")
            print(result.messages[2].content)
            print(result.message.content)


if __name__ == "__main__":
    asyncio.run(main())
