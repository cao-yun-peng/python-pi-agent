from __future__ import annotations

import asyncio

from pi_agent_py import (
    Agent,
    FakeProvider,
    Finish,
    InMemoryEventSink,
    Message,
    TextDelta,
)


async def main() -> None:
    provider = FakeProvider([[TextDelta("这是第三条回答。"), Finish()]])
    events = InMemoryEventSink()
    agent = Agent(
        provider,
        initial_messages=(
            Message(role="user", content="第一条问题"),
            Message(role="assistant", content="第一条回答"),
        ),
        event_sink=events,
    )

    result = await agent.run("第三条问题")
    prepared = next(
        event for event in events.events if event.kind == "context_prepared"
    )

    print("provider context:", [message.content for message in provider.requests[0]])
    print("durable history:", [message.content for message in result.messages])
    print("context trace:", dict(prepared.payload))


if __name__ == "__main__":
    asyncio.run(main())
