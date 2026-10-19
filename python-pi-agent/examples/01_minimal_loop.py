from __future__ import annotations

import asyncio

from pi_agent_py import Agent, AgentEvent, CallbackEventSink, FakeProvider, Finish, TextDelta


async def main() -> None:
    trace: list[AgentEvent] = []

    def observe(event: AgentEvent) -> None:
        trace.append(event)
        if event.kind == "text_delta":
            print(event.payload["text"], end="", flush=True)

    provider = FakeProvider(
        [[TextDelta("你好，"), TextDelta("我是最小 Agent。"), Finish()]]
    )
    agent = Agent(
        provider,
        event_sink=CallbackEventSink(observe),
        system_prompt="你是一个用于学习 Agent 原理的助手。",
    )

    await agent.run("介绍一下自己")

    print("\n\n--- trace ---")
    for event in trace:
        print(f"{event.channel:<7} {event.kind}")


if __name__ == "__main__":
    asyncio.run(main())
