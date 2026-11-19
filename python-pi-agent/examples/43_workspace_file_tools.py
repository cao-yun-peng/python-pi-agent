"""Offline: all six workspace tools, explicit write approval and stale-version refusal."""

import asyncio

from pi_agent_py.api import (
    AgentRuntimeBuilder, CallbackEventSink, EditTool, FindTool, Finish, GrepTool,
    LsTool, ReadTool, StaticApprovalPolicy, ToolCall, WorkspaceReadRequest,
    WriteTool, coding_profile,
)
from pi_agent_py.testing import FakeProvider, InMemoryWorkspaceFiles


async def main() -> None:
    files = InMemoryWorkspaceFiles({"notes/plan.txt": b"TODO draft\n"}, allow_write=True)
    # A scripted fixture knows this version; a real model obtains it from read.
    version = (await files.read_text(WorkspaceReadRequest("notes/plan.txt"))).version
    provider = FakeProvider([
        [ToolCall("ls", "ls", {}), ToolCall("find", "find", {"pattern": "*.txt"}),
         ToolCall("grep", "grep", {"pattern": "TODO"}), Finish("tool_calls")],
        [ToolCall("write", "write", {"path": "notes/new.txt", "content": "created\n"}), Finish("tool_calls")],
        [ToolCall("edit", "edit", {"path": "notes/plan.txt", "old_text": "draft", "new_text": "approved", "expected_version": version}), Finish("tool_calls")],
        [ToolCall("stale", "edit", {"path": "notes/plan.txt", "old_text": "approved", "new_text": "stale replacement", "expected_version": version}), Finish("tool_calls")],
        [ToolCall("read", "read", {"path": "notes/plan.txt"}), Finish("tool_calls")],
        [Finish()],
    ])
    tools = (ReadTool(files), WriteTool(files), EditTool(files), LsTool(files), FindTool(files), GrepTool(files))
    policy = StaticApprovalPolicy(True, reason="This offline fixture explicitly permits writes.")
    async with await AgentRuntimeBuilder().with_provider(provider).with_profile(coding_profile()).with_tools(
        tools
    ).with_approval_policy(policy).with_event_sink(
        CallbackEventSink(lambda event: print(event.channel, event.kind))
    ).build() as runtime:
        result = await runtime.run("workspace-files", "Inspect, create and edit the offline fixture")
        for message in result.messages:
            if message.role == "tool":
                print("RESULT", message.tool_name, message.is_error, message.content)
                print("METADATA", message.structured_content)
    print("Approval requests:", [context.call.name for context in policy.requests])


if __name__ == "__main__":
    asyncio.run(main())
