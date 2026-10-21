from __future__ import annotations

import asyncio

from pi_agent_py import (
    AgentSession,
    FakeProvider,
    Finish,
    FunctionTool,
    InMemorySessionStore,
    JSONObject,
    TextDelta,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
)


async def main() -> None:
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    async def pause(
        arguments: JSONObject,
        context: ToolExecutionContext,
    ) -> ToolOutput:
        del arguments, context
        tool_started.set()
        await release_tool.wait()
        return ToolOutput(content="safe boundary reached")

    provider = FakeProvider(
        [
            [ToolCall("call-1", "pause", {}), Finish("tool_calls")],
            [TextDelta("I applied the steering instruction."), Finish()],
            [TextDelta("I also completed the queued follow-up."), Finish()],
        ]
    )
    store = InMemorySessionStore()
    session = AgentSession(
        "control-demo",
        store,
        provider,
        tools=[
            FunctionTool(
                ToolDefinition(
                    name="pause",
                    description="Pause so the control plane can be observed.",
                    input_schema={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                ),
                pause,
            )
        ],
    )

    active = asyncio.create_task(session.run("Start the work."))
    await tool_started.wait()
    await session.steer("Use the safer implementation.")
    follow_up_id = await session.follow_up("Summarize the completed work.")
    print("running:", session.state, session.active_run_id)
    print("queued follow-up:", follow_up_id)

    release_tool.set()
    await active
    await session.wait_until_idle()

    print("final state:", session.state)
    for message in (await store.load("control-demo")).messages:
        print(message.role, message.content)


if __name__ == "__main__":
    asyncio.run(main())
