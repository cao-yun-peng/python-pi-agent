from __future__ import annotations

import asyncio

from pi_agent_py import (
    AgentSession,
    CharacterTokenEstimator,
    FakeCompactionStrategy,
    FakeProvider,
    Finish,
    InMemoryEventSink,
    InMemorySessionStore,
    Message,
    TextDelta,
    TokenThresholdCompactionPolicy,
)


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
        [[TextDelta("The policy checks before each session run."), Finish()]]
    )
    session = AgentSession(
        "lesson",
        store,
        provider,
        compaction_policy=TokenThresholdCompactionPolicy(
            100,
            estimator=CharacterTokenEstimator(
                characters_per_token=1,
                per_message_overhead=0,
            ),
        ),
        compaction_strategy=FakeCompactionStrategy(
            ["The user learned append-only logs and replayed snapshots."]
        ),
        event_sink=events,
    )

    await session.run("When is compaction triggered?")
    decision = next(
        event for event in events.events if event.kind == "compaction_evaluated"
    )

    print("decision:", dict(decision.payload))
    print("provider context:")
    for message in provider.requests[0]:
        print(f"  {message.role}: {message.content}")
    print("trace:", [event.kind for event in events.events])


if __name__ == "__main__":
    asyncio.run(main())
