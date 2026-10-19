from __future__ import annotations

import asyncio

from pi_agent_py import InMemorySessionStore, Message, replay_session


async def main() -> None:
    store = InMemorySessionStore()

    await store.append(
        "replay-demo",
        expected_version=0,
        messages=(
            Message(role="user", content="请记住：项目代号是松鼠。"),
            Message(role="assistant", content="我记住了。"),
        ),
    )

    events = await store.read_events("replay-demo")
    rebuilt = replay_session("replay-demo", events)

    print("--- durable event log ---")
    for event in events:
        print(
            f"sequence={event.sequence} kind={event.kind} "
            f"role={event.message.role}"
        )

    print("\n--- replayed snapshot ---")
    print(f"version={rebuilt.version}")
    for message in rebuilt.messages:
        print(f"{message.role}: {message.content}")


if __name__ == "__main__":
    asyncio.run(main())
