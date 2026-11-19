"""Explicit local-process integration tests: PI_AGENT_MCP_INTEGRATION=1."""

import asyncio
import ctypes
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager, ExitStack
from pathlib import Path
from unittest.mock import patch

from pi_agent_py.api import (
    AgentRuntimeBuilder,
    DeclaredSideEffectGate,
    Finish,
    JsonSchemaValidator,
    StaticApprovalPolicy,
    ToolCall,
)
from pi_agent_py.testing import FakeProvider
from pi_agent_py.tools.mcp.contracts import (
    AllowedMcpConnections,
    McpConnectionSpec,
    McpServerSettings,
    McpProtocolError,
)
from pi_agent_py.tools.mcp.plugin import mcp_plugin
from pi_agent_py.tools.mcp.sdk import McpSdkOptions, SdkMcpConnector
from pi_agent_py.tools.mcp.tool import mcp_tool_name


def process_exited(pid):
    if sys.platform == "win32":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 87
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@contextmanager
def watch_windows_process(pid):
    """Hold a live-process handle: reopening an exited PID can return access denied."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield lambda: kernel.WaitForSingleObject(handle, 1000) == 0
    finally:
        kernel.CloseHandle(handle)


@unittest.skipUnless(
    os.environ.get("PI_AGENT_MCP_INTEGRATION") == "1"
    and importlib.util.find_spec("mcp"),
    "enable local MCP integration and install [mcp]",
)
class McpStdioTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows process-tree evidence")
    async def test_windows_forced_shutdown_reaps_fixture_and_descendant(self):
        fixture = (
            Path(__file__).resolve().parents[1] / "examples" / "_mcp_demo_server.py"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            spec = McpConnectionSpec(
                "local",
                "stdio",
                command=sys.executable,
                args=("-B", str(fixture)),
                env={"MCP_DEMO_TRACE": str(path), "MCP_DEMO_STALL_CLOSE": "1"},
            )
            endpoint = await SdkMcpConnector().open(spec)
            with ExitStack() as stack:
                try:
                    await endpoint.list_tools()
                    child = await endpoint.call_tool("spawn_child", {})
                    child_pid = child.structured_content["pid"]
                    parent_pid = json.loads(
                        path.read_text(encoding="utf-8").splitlines()[0]
                    )["pid"]
                    parent_exited = stack.enter_context(
                        watch_windows_process(parent_pid)
                    )
                    child_exited = stack.enter_context(watch_windows_process(child_pid))
                finally:
                    # Zero SDK grace; OS handle signals prove actual process termination.
                    with patch("mcp.client.stdio.PROCESS_TERMINATION_TIMEOUT", 0):
                        await endpoint.aclose()
                self.assertTrue(parent_exited())
                self.assertTrue(child_exited())

    async def test_actual_protocol_cancellation_and_abrupt_exit_do_not_replay(self):
        fixture = (
            Path(__file__).resolve().parents[1] / "examples" / "_mcp_demo_server.py"
        )
        for action in ("wait", "crash"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "trace.jsonl"
                spec = McpConnectionSpec(
                    "local",
                    "stdio",
                    command=sys.executable,
                    args=("-B", str(fixture)),
                    env={"MCP_DEMO_TRACE": str(path)},
                )
                endpoint = await SdkMcpConnector().open(spec)
                try:
                    await endpoint.list_tools()
                    if action == "wait":
                        call = asyncio.create_task(endpoint.call_tool(action, {}))
                        # External fixture readiness, not a sleep-based timeout assertion.
                        async with asyncio.timeout(5):
                            while not any(
                                json.loads(line)["event"] == "wait"
                                for line in path.read_text(
                                    encoding="utf-8"
                                ).splitlines()
                            ):
                                await asyncio.sleep(0)
                        call.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await call
                        self.assertFalse(
                            (
                                await endpoint.call_tool("echo", {"value": "next"})
                            ).is_error
                        )
                    else:
                        with self.assertRaises(McpProtocolError):
                            await endpoint.call_tool(action, {})
                finally:
                    await endpoint.aclose()
                records = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(sum(r["event"] == action for r in records), 1)
                if action == "wait":
                    self.assertIn("wait_cancelled", [r["event"] for r in records])
                self.assertTrue(process_exited(records[0]["pid"]))

    async def test_real_stdio_both_protocols_and_cross_task_lifecycle(self):
        fixture = (
            Path(__file__).resolve().parents[1] / "examples" / "_mcp_demo_server.py"
        )
        for mode, protocol in (("auto", "2026-07-28"), ("legacy", "2025-11-25")):
            with tempfile.TemporaryDirectory() as directory:
                trace = Path(directory) / "trace.jsonl"
                spec = McpConnectionSpec(
                    "local",
                    "stdio",
                    command=sys.executable,
                    args=("-B", str(fixture)),
                    env={"MCP_DEMO_TRACE": str(trace)},
                )
                connector = SdkMcpConnector(McpSdkOptions(mode=mode))
                name = mcp_tool_name("local", "echo")
                provider = FakeProvider(
                    [
                        [
                            ToolCall("c", name, {"value": "local-only"}),
                            Finish("tool_calls"),
                        ],
                        [Finish()],
                    ]
                )
                validator = JsonSchemaValidator()
                builder = (
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
                            connector=connector,
                            schema_validator=validator,
                            connection_policy=AllowedMcpConnections((spec,)),
                        ),
                    )
                )
                runtime = await asyncio.create_task(builder.build())
                try:
                    result = await asyncio.create_task(runtime.run("s", "echo"))
                    output = result.messages[2]
                    self.assertFalse(output.is_error, output.content)
                    self.assertEqual(
                        output.structured_content["mcp"]["protocol_version"], protocol
                    )
                    self.assertEqual(
                        output.structured_content["result"]["value"], "local-only"
                    )
                    pid = output.structured_content["result"]["pid"]
                finally:
                    await asyncio.create_task(runtime.aclose())
                self.assertTrue(
                    process_exited(pid), f"fixture process {pid} survived close"
                )
                records = [
                    json.loads(line)
                    for line in trace.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(
                    [r["event"] for r in records], ["opened", "echo", "closed"]
                )


if __name__ == "__main__":
    unittest.main()
