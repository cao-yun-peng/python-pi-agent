"""Run with: python examples/18_provider_request_context.py."""

from __future__ import annotations

import asyncio

from pi_agent_py import (
    AgentComponents,
    AgentSession,
    CompactionComponents,
    FakeCompactionStrategy,
    FakeProvider,
    Finish,
    InMemorySessionStore,
    Message,
    TextDelta,
)


async def main() -> None:
    store = InMemorySessionStore()
    await store.append(
        "teaching-session",
        0,
        [
            Message(role="user", content="old question"),
            Message(role="assistant", content="old answer"),
            Message(role="user", content="recent question"),
            Message(role="assistant", content="recent answer"),
        ],
    )
    provider = FakeProvider([[TextDelta("new answer"), Finish()]])
    compactor = FakeCompactionStrategy(["Old topic was resolved."])
    session = AgentSession(
        "teaching-session",
        store,
        AgentComponents(
            provider,
            compaction=CompactionComponents(strategy=compactor),
        ),
    )

    await session.compact(keep_recent_turns=1)
    await session.run("new question")

    compact_context = compactor.request_contexts[0]
    agent_context = provider.request_contexts[0]
    print("compaction:", compact_context)
    print("agent turn:", agent_context)
    print("same session:", compact_context.session_id == agent_context.session_id)
    print("different purposes:", compact_context.purpose, agent_context.purpose)


if __name__ == "__main__":
    asyncio.run(main())
