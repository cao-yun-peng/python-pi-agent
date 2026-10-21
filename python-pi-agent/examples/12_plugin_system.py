"""Register a tool and hook through an explicitly assembled plugin."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from pi_agent_py import (
    Agent,
    FakeProvider,
    Finish,
    FunctionTool,
    JSONObject,
    Message,
    PluginAPI,
    PluginHost,
    TextDelta,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
)


class LearningTraceHook:
    async def prepare(
        self,
        messages: Sequence[Message],
        *,
        next_request_number: int,
    ) -> Sequence[Message]:
        roles = " -> ".join(message.role for message in messages)
        print(f"hook before request {next_request_number}: {roles}")
        return messages


def calculator_plugin(api: PluginAPI) -> None:
    async def add(
        arguments: JSONObject,
        context: ToolExecutionContext,
    ) -> ToolOutput:
        del context
        left = arguments["a"]
        right = arguments["b"]
        assert isinstance(left, int) and not isinstance(left, bool)
        assert isinstance(right, int) and not isinstance(right, bool)
        return ToolOutput(content=str(left + right))

    api.register_tool(
        FunctionTool(
            ToolDefinition(
                name="add",
                description="Add two integers.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "a": {"type": "integer"},
                        "b": {"type": "integer"},
                    },
                    "required": ["a", "b"],
                    "additionalProperties": False,
                },
                retry_safe=True,
                replay_safe=True,
            ),
            add,
        )
    )
    api.register_next_turn_hook(LearningTraceHook(), timeout_seconds=1)
    api.register_cleanup(lambda: print("calculator plugin released"))


async def main() -> None:
    host = PluginHost()
    await host.load("calculator", calculator_plugin)
    capabilities = host.snapshot()
    provider = FakeProvider(
        [
            [ToolCall("add-1", "add", {"a": 20, "b": 22}), Finish("tool_calls")],
            [TextDelta("20 + 22 = 42"), Finish()],
        ]
    )
    agent = Agent(
        provider,
        tools=capabilities.tools,
        next_turn_hook=capabilities.next_turn_hook,
    )

    try:
        result = await agent.run("Use the tool to add 20 and 22.")
        print(result.message.content)
    finally:
        await host.close()


if __name__ == "__main__":
    asyncio.run(main())
