"""Offline: signature-derived schema, explicit errors, and runtime validation."""

import asyncio
import json
from typing import Annotated, Literal

from pi_agent_py.api import (
    Agent, AgentComponents, CallbackEventSink, Finish, TextDelta,
    ToolCall, ToolError, tool,
)
from pi_agent_py.testing import FakeProvider


@tool
async def read_note(
    path: Annotated[str, "Workspace-relative note path"],
    mode: Literal["head", "tail"] = "head",
) -> str:
    """Read one note from an offline catalogue."""
    if path != "welcome.txt":
        raise ToolError("Note not found", structured_content={"status": "missing"})
    return f"{mode}: Welcome to the agent harness."


@tool
async def collect_tags(tags: list[str] = ["demo"]) -> list[str]:
    """Show that omitted mutable defaults are isolated per invocation."""
    tags.append("processed")
    return tags


async def main() -> None:
    print(json.dumps(read_note.definition.input_schema, ensure_ascii=False, indent=2))
    async def invalid_default(limit: int = "ten") -> int:
        """A deliberately invalid default, rejected during tool definition."""
        return limit
    try:
        tool(invalid_default)
    except TypeError as error:
        print("definition rejected:", error)
    provider = FakeProvider([
        [ToolCall("ok", "read_note", {"path": "welcome.txt"}),
         ToolCall("missing", "read_note", {"path": "missing.txt"}),
         ToolCall("invalid", "read_note", {"path": 7}),
         ToolCall("tags-1", "collect_tags", {}),
         ToolCall("tags-2", "collect_tags", {}), Finish("tool_calls")],
        [TextDelta("One note read; one missing; one invalid argument."), Finish()],
    ])
    sink = CallbackEventSink(lambda event: print(event.channel, event.kind))
    result = await Agent(AgentComponents(provider, tools=(read_note, collect_tags)), event_sink=sink).run("Read notes")
    for message in result.messages:
        print(message.role, message.is_error, message.content)


if __name__ == "__main__":
    asyncio.run(main())
