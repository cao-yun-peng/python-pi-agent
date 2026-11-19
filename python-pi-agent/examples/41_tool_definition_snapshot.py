"""Offline: mutate source data and replace a definition without drifting a live Agent."""

import asyncio
from dataclasses import replace

from pi_agent_py.api import (
    Agent, AgentComponents, CallbackEventSink, Finish, JSONObject, ToolCall,
    ToolDefinition, ToolExecutionContext, ToolOutput,
)
from pi_agent_py.testing import FakeProvider


class EchoTool:
    def __init__(self, definition: ToolDefinition) -> None:
        self.definition = definition
        self.calls = 0

    async def execute(self, arguments: JSONObject, context: ToolExecutionContext) -> ToolOutput:
        self.calls += 1
        return ToolOutput(f"{context.definition.description}: {arguments['n']} (call {self.calls})")


def assemble(tool: EchoTool, name: str) -> Agent:
    provider = FakeProvider([
        [ToolCall("bad", name, {"n": "wrong"}), ToolCall("good", name, {"n": 1}), Finish("tool_calls")],
        [Finish()],
    ])
    return Agent(AgentComponents(provider, tools=(tool,)), event_sink=CallbackEventSink(
        lambda event: print(event.channel, event.kind)
    ))


async def main() -> None:
    schema: JSONObject = {
        "type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"],
    }
    tool = EchoTool(ToolDefinition("echo", "Original contract", schema))
    old_agent = assemble(tool, "echo")
    schema["properties"] = {}
    exposed = dict(tool.definition.input_schema)
    exposed["properties"]["n"]["type"] = "string"
    print("Captured schema:", dict(tool.definition.input_schema))
    tool.definition = replace(tool.definition, name="echo_v2", description="Updated contract")
    for agent in (old_agent, assemble(tool, "echo_v2")):
        result = await agent.run("Demonstrate stable definitions")
        for message in result.messages:
            if message.role == "tool":
                print("result:", message.is_error, message.content)
    print("Original handler calls:", tool.calls)


if __name__ == "__main__":
    asyncio.run(main())
