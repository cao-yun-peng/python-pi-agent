"""Explicit loopback HTTP/SSE integration, never public network."""

import importlib.util
import asyncio
import os
import threading
import unittest
from unittest.mock import patch

from fixtures.mcp_http import local_peer
from pi_agent_py.api import (
    AgentRuntimeBuilder,
    DeclaredSideEffectGate,
    Finish,
    JsonSchemaValidator,
    RuntimeBuildError,
    StaticApprovalPolicy,
    ToolCall,
)
from pi_agent_py.testing import FakeProvider
from pi_agent_py.tools.mcp.contracts import (
    AllowedMcpConnections,
    McpConnectionSpec,
    McpLimits,
    McpServerSettings,
)
from pi_agent_py.tools.mcp.plugin import mcp_plugin
from pi_agent_py.tools.mcp.sdk import McpSdkOptions, SdkMcpConnector
from pi_agent_py.tools.mcp.tool import mcp_tool_name


@unittest.skipUnless(
    os.environ.get("PI_AGENT_MCP_INTEGRATION") == "1"
    and importlib.util.find_spec("mcp"),
    "enable loopback MCP integration and install [mcp]",
)
class McpHttpIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_modern_http_cancel_closes_active_response_stream(self):
        started, cancelled = threading.Event(), threading.Event()
        with local_peer("wait", started=started, cancelled=cancelled) as (
            url,
            calls,
            _,
            _,
        ):
            async with await self.builder(url).build() as runtime:
                task = asyncio.create_task(runtime.run("s", "q"))
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(await asyncio.to_thread(cancelled.wait, 3))
            self.assertEqual(calls, ["echo"])

    def builder(self, url, *, mode="auto", token="fixture-only", limit=1024 * 1024):
        spec = McpConnectionSpec(
            "local", "streamable_http", url=url, headers={"Authorization": token}
        )
        validator = JsonSchemaValidator()
        provider = FakeProvider(
            [
                [
                    ToolCall(
                        "c", mcp_tool_name("local", "echo"), {"value": "local echo"}
                    ),
                    Finish("tool_calls"),
                ],
                [Finish()],
            ]
        )
        return (
            AgentRuntimeBuilder()
            .with_provider(provider)
            .with_schema_validator(validator)
            .with_approval_policy(StaticApprovalPolicy(True))
            .with_execution_gate(
                DeclaredSideEffectGate(allowed_side_effects=("external",))
            )
            .with_plugin(
                "mcp",
                mcp_plugin(
                    (McpServerSettings(spec, ("echo",)),),
                    schema_validator=validator,
                    connection_policy=AllowedMcpConnections((spec,)),
                    connector=SdkMcpConnector(
                        McpSdkOptions(
                            mode=mode,
                            allow_insecure_local=True,
                            limits=McpLimits(max_result_bytes=limit),
                        )
                    ),
                ),
            )
        )

    async def test_json_sse_both_protocols_explicit_credentials_and_client_close(self):
        from pi_agent_py.tools.mcp._http import BoundedHttpTransport

        original = BoundedHttpTransport.aclose
        closed = []

        async def close(transport):
            await original(transport)
            closed.append(True)

        with patch.object(BoundedHttpTransport, "aclose", close):
            for protocol in ("auto", "legacy", "2026-07-28"):
                for response in ("json", "sse"):
                    with local_peer(response) as (url, calls, methods, auth):
                        async with await self.builder(
                            url, mode=protocol
                        ).build() as runtime:
                            output = (await runtime.run("s", "q")).messages[2]
                            self.assertFalse(output.is_error, output.content)
                            self.assertEqual(output.content, "local echo")
                        self.assertEqual(calls, ["echo"])
                        self.assertTrue(all(value == "fixture-only" for value in auth))
                        if protocol == "legacy":
                            self.assertIn("DELETE", methods)
        self.assertEqual(len(closed), 6)

    async def test_401_closes_failed_startup_and_does_not_call_tools(self):
        with local_peer("unauthorized") as (url, calls, _, _):
            with self.assertRaises(RuntimeBuildError) as caught:
                await self.builder(url).build()
            self.assertEqual(calls, [])
            self.assertNotIn("fixture-only", str(caught.exception))

    async def test_large_results_redirect_disconnect_and_sse_resumption_do_not_resend(
        self,
    ):
        for fault in (
            "large-json",
            "large-sse",
            "redirect",
            "disconnect",
            "broken-sse",
        ):
            with local_peer(fault) as (url, calls, methods, _):
                async with await self.builder(url, limit=1024).build() as runtime:
                    output = (await runtime.run("s", "q")).messages[2]
                    self.assertTrue(output.is_error, fault)
                    self.assertEqual(
                        output.structured_content["mcp"]["outcome"], "outcome_unknown"
                    )
                    self.assertNotIn("private", output.content)
                self.assertEqual(calls, ["echo"], fault)
                self.assertNotIn("GET", methods, fault)


if __name__ == "__main__":
    unittest.main()
