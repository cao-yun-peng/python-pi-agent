from __future__ import annotations

import asyncio
from collections.abc import Sequence

from pi_agent_py import (
    AgentSession,
    FakeCompactionStrategy,
    FakeProvider,
    Finish,
    FunctionTool,
    InMemoryEventSink,
    InMemorySessionStore,
    JSONObject,
    Message,
    TextDelta,
    TokenThresholdCompactionPolicy,
    ToolCall,
    ToolDefinition,
    ToolOutput,
)


class MessageCountEstimator:
    """Make the two threshold decisions obvious in this teaching example."""

    def estimate(self, messages: Sequence[Message]) -> int:
        return len(messages)


async def main() -> None:
    store = InMemorySessionStore()
    await store.append(
        "lesson",
        0,
        (
            Message(role="user", content="Explain append-only session logs."),
            Message(
                role="assistant",
                content="They retain facts and derive state through replay.",
            ),
            Message(role="user", content="What does a snapshot contain?"),
            Message(
                role="assistant",
                content="A version and the current model-visible messages.",
            ),
        ),
    )

    events = InMemoryEventSink()
    provider = FakeProvider(
        [
            [ToolCall("call-1", "load_notes", {}), Finish("tool_calls")],
            [TextDelta("Compaction ran before this second request."), Finish()],
        ]
    )

    async def load_notes(arguments: JSONObject) -> ToolOutput:
        del arguments
        return ToolOutput(content="A large model-visible tool result.")

    tool = FunctionTool(
        ToolDefinition(
            name="load_notes",
            description="Load notes for the current task.",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        load_notes,
    )
    session = AgentSession(
        "lesson",
        store,
        provider,
        tools=[tool],
        compaction_policy=TokenThresholdCompactionPolicy(
            7,
            estimator=MessageCountEstimator(),
        ),
        compaction_strategy=FakeCompactionStrategy(
            ["The user learned append-only logs and replayed snapshots."]
        ),
        event_sink=events,
    )

    await session.run("Load the notes, then explain the trigger point.")
    decisions = [
        event.payload
        for event in events.events
        if event.kind == "compaction_evaluated"
    ]

    print("decisions:", [dict(decision) for decision in decisions])
    print("second provider context:")
    for message in provider.requests[1]:
        print(f"  {message.role}: {message.content or message.tool_calls}")
    print("trace:", [event.kind for event in events.events])


if __name__ == "__main__":
    asyncio.run(main())
