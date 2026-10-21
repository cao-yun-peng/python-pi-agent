from __future__ import annotations

import asyncio

from pi_agent_py import (
    AgentSession,
    FakeProvider,
    Finish,
    InMemorySessionStore,
    TextDelta,
    ToolCall,
)


async def main() -> None:
    store = InMemorySessionStore()
    session = AgentSession(
        "metadata-demo",
        store,
        FakeProvider(
            [
                [ToolCall("call-1", "missing", {}), Finish("tool_calls")],
                [TextDelta("I recovered from the unavailable tool."), Finish()],
            ]
        ),
    )

    await session.run("Show the durable event metadata.")

    for event in await store.read_events("metadata-demo"):
        print(
            event.event_id,
            event.run_id,
            event.turn_id,
            event.provenance.source,
            event.provenance.actor_id,
        )


if __name__ == "__main__":
    asyncio.run(main())
