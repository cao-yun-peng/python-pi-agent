from __future__ import annotations

import asyncio

from pi_agent_py import (
    Agent,
    AgentEvent,
    CallbackEventSink,
    FakeProvider,
    Finish,
    FunctionTool,
    JSONObject,
    StaticApprovalPolicy,
    TextDelta,
    ToolCall,
    ToolDefinition,
    ToolOutput,
)


async def add(arguments: JSONObject) -> ToolOutput:
    left = arguments["a"]
    right = arguments["b"]
    assert isinstance(left, int) and not isinstance(left, bool)
    assert isinstance(right, int) and not isinstance(right, bool)
    value = left + right
    return ToolOutput(content=str(value), structured_content={"value": value})


def observe(event: AgentEvent) -> None:
    if event.channel == "capability":
        print(f"[tool] {event.kind}: {dict(event.payload)}")
    elif event.kind == "text_delta":
        print(event.payload["text"], end="", flush=True)


async def main() -> None:
    definition = ToolDefinition(
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
        requires_approval=True,
        retry_safe=True,
        replay_safe=True,
    )
    provider = FakeProvider(
        [
            [ToolCall("call-1", "add", {"a": 2, "b": 3}), Finish("tool_calls")],
            [TextDelta("2 + 3 = 5"), Finish()],
        ]
    )
    agent = Agent(
        provider,
        tools=[FunctionTool(definition, add)],
        approval_policy=StaticApprovalPolicy(
            True,
            reason="This deterministic example pre-authorizes the add tool.",
        ),
        event_sink=CallbackEventSink(observe),
    )

    await agent.run("计算 2 + 3")
    print()


if __name__ == "__main__":
    asyncio.run(main())
