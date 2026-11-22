"""Offline: python examples/46_structured_compaction.py (scripted summary)."""

import asyncio

from pi_agent_py.api import (
    AgentSession, CompactionComponents, Finish, InMemoryEventSink,
    InMemorySessionStore, ModelCompactionStrategy, TextDelta, coding_profile,
)
from pi_agent_py.testing import FakeProvider


CHECKPOINT = """## Goal
Understand session replay.
## Constraints & Preferences
Use offline examples.
## Done
Read the durable history design; no code changes were made.
## In Progress
Replay verification is still unverified.
## Blocked
(none)
## Key Decisions
Use FakeProvider to avoid external model calls.
## Next Steps
Run the replay example.
## Critical Evidence
The first response only described a design; no test result was reported.
"""


async def main() -> None:
    provider = FakeProvider([
        [TextDelta("Read the durable history design; tests are still pending."), Finish()],
        [TextDelta("Next: run the replay example."), Finish()],
    ])
    summarizer = FakeProvider([[TextDelta(CHECKPOINT), Finish()]])
    components = coding_profile().build_components(provider).replace(
        compaction=CompactionComponents(ModelCompactionStrategy(summarizer)),
    )
    store, events = InMemorySessionStore(), InMemoryEventSink()
    async with AgentSession("checkpoint-demo", store, components, event_sink=events) as session:
        await session.run("Explain replay using offline examples.")
        await session.run("What remains to verify?")
        snapshot = await session.compact(keep_recent_turns=1)
    print("SUMMARY REQUEST RULES\n" + summarizer.requests[0][0].content)
    print("DURABLE CHECKPOINT\n" + snapshot.messages[1].content)
    print("EVENTS:", [event.kind for event in events.events if event.kind.startswith("compaction_")])
    assert summarizer.requested_tools == [()]
    assert snapshot.messages[0].role == "system"
    assert "<conversation-summary>" in snapshot.messages[1].content


if __name__ == "__main__":
    asyncio.run(main())
