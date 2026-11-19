"""Trusted local fixture for MCP examples/integration tests; never downloaded."""

import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.mcpserver import MCPServer


def trace(event: str) -> None:
    target = os.environ.get("MCP_DEMO_TRACE")
    if target:
        with Path(target).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, "pid": os.getpid()}) + "\n")


@asynccontextmanager
async def lifespan(server):
    trace("opened")
    try:
        yield None
    finally:
        trace("closed")
        if os.environ.get("MCP_DEMO_STALL_CLOSE") == "1":
            await asyncio.Event().wait()
        for child in children:
            child.terminate()
            child.wait()


server = MCPServer("local-teaching-fixture", lifespan=lifespan)
children = []


@server.tool()
async def spawn_child() -> dict[str, int]:
    """Create an owned descendant for the explicit process-tree cleanup test."""
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", "import time; time.sleep(3600)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    children.append(child)
    return {"pid": child.pid}


@server.tool()
async def echo(value: str) -> dict[str, str | int]:
    """Return a local value and the fixture process ID."""
    trace("echo")
    return {"value": value, "pid": os.getpid()}


@server.tool()
async def wait() -> str:
    """Block until cancelled; record cleanup without logging request data."""
    trace("wait")
    try:
        await asyncio.Event().wait()
    finally:
        trace("wait_cancelled")
    return "unreachable"


@server.tool()
async def crash() -> str:
    """Exit abruptly to exercise an unknown remote outcome."""
    trace("crash")
    os._exit(3)


if __name__ == "__main__":
    # Only protocol frames may go to stdout.
    print("fixture diagnostic (stderr is intentionally discarded)", file=sys.stderr)
    server.run()
