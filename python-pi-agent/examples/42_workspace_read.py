"""Read through production tools with an in-memory or explicitly selected Docker workspace."""

import argparse
import asyncio
from pathlib import Path

from pi_agent_py.api import (
    AgentRuntimeBuilder, CallbackEventSink, Finish, PluginAPI, ReadTool,
    SandboxedWorkspaceFiles, ToolCall, WorkspaceFiles, WorkspaceReadLimits, read_only_profile,
)
from pi_agent_py.testing import FakeProvider, InMemoryWorkspaceFiles
from pi_agent_py.tools.sandbox import DockerProcessSandbox


async def main(workspace: Path | None, image: str | None, path: str) -> None:
    files: WorkspaceFiles
    limits = WorkspaceReadLimits()
    if workspace is None:
        files = InMemoryWorkspaceFiles({"notes/demo.txt": b"first\nsecond\nthird\n"}, limits=limits)
    else:
        if not image:
            raise ValueError("--image must name an already installed Linux Python image")
        sandbox = DockerProcessSandbox(workspace, image=image,
            max_output_bytes=2 * limits.max_file_bytes + 4096)
        files = SandboxedWorkspaceFiles(sandbox, limits=limits)

    def register(api: PluginAPI) -> None:
        # Host owns registration; files/sandbox are borrowed and own no idle tasks.
        api.register_tool(ReadTool(files))

    provider = FakeProvider([
        [ToolCall("page-1", "read", {"path": path, "limit": 1}), Finish("tool_calls")],
        [ToolCall("page-2", "read", {"path": path, "offset": 2, "limit": 2}),
         ToolCall("missing", "read", {"path": "missing-example.txt"}), Finish("tool_calls")],
        [Finish()],
    ])
    async with await AgentRuntimeBuilder().with_provider(provider).with_profile(
        read_only_profile()
    ).with_plugin("workspace-read", register).with_event_sink(
        CallbackEventSink(lambda event: print(event.channel, event.kind))
    ).build() as runtime:
        result = await runtime.run("workspace-demo", "Read two pages and report a missing file")
        for message in result.messages:
            if message.role == "tool":
                print("RESULT", message.is_error, message.content)
                print("METADATA", message.structured_content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, help="Explicit host directory mounted at /workspace")
    parser.add_argument("--image", help="Local Linux image containing python3; never pulled automatically")
    parser.add_argument("--path", default="notes/demo.txt")
    args = parser.parse_args()
    asyncio.run(main(args.workspace, args.image, args.path))
