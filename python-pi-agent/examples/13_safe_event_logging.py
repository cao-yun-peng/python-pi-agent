"""Log redacted agent events without blocking or controlling the agent loop."""

from __future__ import annotations

import asyncio
import json

from pi_agent_py import (
    Agent,
    AgentEvent,
    BufferedEventSink,
    CallbackEventSink,
    FakeProvider,
    Finish,
    RedactingEventSink,
    TextDelta,
)


def write_json_log(event: AgentEvent) -> None:
    print(
        json.dumps(
            {
                "kind": event.kind,
                "channel": event.channel,
                "payload": event.payload,
            },
            ensure_ascii=False,
        )
    )


async def main() -> None:
    # Redact synchronously before the event enters the background queue.
    buffered = BufferedEventSink(
        CallbackEventSink(write_json_log),
        max_queue_size=32,
    )
    logging_sink = RedactingEventSink(buffered)
    agent = Agent(
        FakeProvider([[TextDelta("private answer"), Finish()]]),
        event_sink=logging_sink,
    )

    try:
        result = await agent.run("Use api_key=sk-demo to answer this question.")
        await buffered.flush()
        print(f"actual result stays unchanged: {result.message.content}")
        print(f"runtime observer failures: {agent.event_sink_failures}")
        print(f"buffered delivery failures: {buffered.failures}")
    finally:
        await buffered.aclose()


if __name__ == "__main__":
    asyncio.run(main())
