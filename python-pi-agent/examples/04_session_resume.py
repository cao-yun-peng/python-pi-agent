from __future__ import annotations

import asyncio

from pi_agent_py import (
    AgentSession,
    FakeProvider,
    Finish,
    InMemorySessionStore,
    TextDelta,
)


async def main() -> None:
    provider = FakeProvider(
        [
            [TextDelta("你好，小明。"), Finish(reason="stop")],
            [TextDelta("你叫小明。"), Finish(reason="stop")],
        ]
    )
    store = InMemorySessionStore()

    first_session = AgentSession(
        "learning-session",
        store,
        provider,
        system_prompt="记住用户明确告诉你的事实。",
    )
    await first_session.run("我叫小明。")

    resumed_session = AgentSession(
        "learning-session",
        store,
        provider,
        system_prompt="记住用户明确告诉你的事实。",
    )
    result = await resumed_session.run("我叫什么？")

    print(result.message.content)
    print("\n--- persisted history ---")
    for index, message in enumerate(result.messages, start=1):
        print(f"{index}. {message.role}: {message.content}")


if __name__ == "__main__":
    asyncio.run(main())
