from __future__ import annotations

import asyncio

from pi_agent_py import (
    Agent,
    CharacterTokenEstimator,
    FakeProvider,
    Finish,
    InMemoryEventSink,
    Message,
    TextDelta,
    TokenBudgetContextStrategy,
)


async def main() -> None:
    provider = FakeProvider([[TextDelta("保留最近上下文后的回答。"), Finish()]])
    events = InMemoryEventSink()
    strategy = TokenBudgetContextStrategy(
        42,
        estimator=CharacterTokenEstimator(
            characters_per_token=1,
            per_message_overhead=0,
        ),
    )
    agent = Agent(
        provider,
        initial_messages=(
            Message(role="system", content="回答简洁"),
            Message(role="user", content="旧问题"),
            Message(role="assistant", content="旧回答"),
            Message(role="user", content="较新的问题"),
            Message(role="assistant", content="较新的回答"),
        ),
        context_strategy=strategy,
        event_sink=events,
    )

    result = await agent.run("当前问题")
    prepared = next(
        event for event in events.events if event.kind == "context_prepared"
    )

    print("provider context:", [message.content for message in provider.requests[0]])
    print("durable history:", [message.content for message in result.messages])
    print("context trace:", dict(prepared.payload))


if __name__ == "__main__":
    asyncio.run(main())
