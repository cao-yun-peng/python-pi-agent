from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from pi_agent_py import (
    AgentSession,
    FakeProvider,
    Finish,
    InMemoryEventSink,
    JsonlSessionStore,
    Message,
    ModelCompactionStrategy,
    TextDelta,
)


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "sessions.jsonl"
        store = JsonlSessionStore(path)
        await store.append(
            "lesson",
            0,
            (
                Message(role="system", content="Teach with concrete examples."),
                Message(role="user", content="Why use an event log?"),
                Message(
                    role="assistant",
                    content="It preserves facts and enables replay.",
                ),
                Message(role="user", content="What does replay produce?"),
                Message(
                    role="assistant",
                    content="A current SessionSnapshot projection.",
                ),
            ),
        )

        summary_provider = FakeProvider(
            [[TextDelta("The user learned event log and replay basics."), Finish()]]
        )
        trace = InMemoryEventSink()
        session = AgentSession(
            "lesson",
            store,
            FakeProvider([]),
            compaction_strategy=ModelCompactionStrategy(summary_provider),
            event_sink=trace,
        )
        compacted = await session.compact(keep_recent_turns=1)

        resumed_provider = FakeProvider(
            [[TextDelta("Compaction is a durable replayable event."), Finish()]]
        )
        resumed = AgentSession(
            "lesson",
            JsonlSessionStore(path),
            resumed_provider,
        )
        await resumed.run("What did we add next?")

        events = await JsonlSessionStore(path).read_events("lesson")
        print("durable event kinds:", [event.kind for event in events])
        print("compacted version:", compacted.version)
        print(
            "replayed messages:",
            [(message.role, message.content) for message in compacted.messages],
        )
        print(
            "next model context:",
            [
                (message.role, message.content)
                for message in resumed_provider.requests[0]
            ],
        )
        print("compaction trace:", [event.kind for event in trace.events])


if __name__ == "__main__":
    asyncio.run(main())
