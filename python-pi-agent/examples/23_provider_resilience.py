"""Offline retry/fallback trace using only FakeProvider."""

from __future__ import annotations

import asyncio

from pi_agent_py import (
    Agent,
    AgentComponents,
    FakeProvider,
    Finish,
    FullJitterBackoff,
    InMemoryEventSink,
    ProviderCandidate,
    ProviderTimeoutError,
    ResilientModelRuntime,
    TextDelta,
)


async def main() -> None:
    events = InMemoryEventSink()
    primary = FakeProvider(
        [
            [ProviderTimeoutError("primary timeout 1")],
            [ProviderTimeoutError("primary timeout 2")],
        ]
    )
    fallback = FakeProvider(
        [[TextDelta("由 fallback Provider 完成。"), Finish("stop")]]
    )
    model_runtime = ResilientModelRuntime(
        [
            ProviderCandidate("primary", primary),
            ProviderCandidate("fallback", fallback),
        ],
        event_sink=events,
        # The example stays instant; production uses the default jitter delays.
        backoff=FullJitterBackoff(
            base_delay_seconds=0,
            max_delay_seconds=0,
        ),
    )
    agent = Agent(AgentComponents(model_runtime), event_sink=events)

    result = await agent.run("演示 Provider resilience")
    print(result.message.content)
    print("\n--- provider trace ---")
    for event in events.events:
        if event.kind.startswith("provider_"):
            provider = event.payload.get("provider_name", "-")
            attempt = event.payload.get("total_attempt", "-")
            print(f"{event.kind:29} provider={provider} attempt={attempt}")


if __name__ == "__main__":
    asyncio.run(main())
