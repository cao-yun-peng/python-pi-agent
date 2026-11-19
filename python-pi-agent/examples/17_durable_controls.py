"""Run with: python examples/17_durable_controls.py."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from pi_agent_py import (
    AgentComponents,
    AgentSession,
    FakeProvider,
    Finish,
    JsonlSessionStore,
    QueuedControl,
    TextDelta,
)


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "sessions.jsonl"
        first_process = JsonlSessionStore(path)
        await first_process.record_control(
            "teaching-session",
            0,
            "control_enqueued",
            QueuedControl(
                control_id="follow-up-1",
                delivery="follow_up",
                prompt="resume this after restart",
                run_id="run-after-restart",
            ),
        )

        reopened_store = JsonlSessionStore(path)
        session = AgentSession(
            "teaching-session",
            reopened_store,
            AgentComponents(FakeProvider([[TextDelta("recovered"), Finish()]])),
        )
        await session.wait_until_idle()

        events = await reopened_store.read_events("teaching-session")
        snapshot = await reopened_store.load("teaching-session")
        print("durable transitions:", [event.kind for event in events])
        print("messages:", [message.content for message in snapshot.messages])
        print("pending controls:", snapshot.pending_controls)


if __name__ == "__main__":
    asyncio.run(main())
