"""Offline AgentRuntime assembly, session reuse, and lifecycle trace."""

from __future__ import annotations

import asyncio

from pi_agent_py.api import (
    Agent,
    AgentProfile,
    AgentRuntimeBuilder,
    Finish,
    InMemoryEventSink,
    TextDelta,
)
from pi_agent_py.testing import FakeProvider


async def main() -> None:
    events = InMemoryEventSink()
    profile = AgentProfile(
        name="teaching-assistant",
        system_prompt="Answer briefly and preserve session context.",
    )
    provider = FakeProvider(
        [
            [TextDelta("first answer"), Finish()],
            [TextDelta("second answer"), Finish()],
        ]
    )
    runtime = await (
        AgentRuntimeBuilder()
        .with_provider(provider)
        .with_profile(profile)
        .with_event_sink(events)
        .build()
    )

    async with runtime:
        first = await runtime.run("demo-session", "first question")
        second = await runtime.run("demo-session", "second question")
        snapshot = await runtime.snapshot("demo-session")
        print(first.message.content)
        print(second.message.content)
        print("roles:", [message.role for message in snapshot.messages])

    print("\n--- runtime trace ---")
    for event in events.events:
        if event.kind.startswith("runtime_"):
            print(event.kind, event.envelope.session_id or "-")

    standalone_provider = FakeProvider([[TextDelta("standalone answer"), Finish()]])
    components = profile.build_components(standalone_provider).replace(
        max_model_requests=1
    )
    standalone = await Agent(components).run("one request")
    print("\nstandalone:", standalone.message.content)


if __name__ == "__main__":
    asyncio.run(main())
