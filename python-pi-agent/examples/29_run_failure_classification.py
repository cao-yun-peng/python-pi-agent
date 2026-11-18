"""Show how a foreign storage error becomes a safe, observable run failure."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from sqlite3 import OperationalError

from pi_agent_py import (
    AgentSession,
    FakeProvider,
    Finish,
    InMemoryEventSink,
    InMemorySessionStore,
    Message,
    SessionEventContext,
    SessionSnapshot,
    SessionStorageError,
    TextDelta,
)


class FailingOnceStore(InMemorySessionStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_append = True

    async def append(
        self,
        session_id: str,
        expected_version: int,
        messages: Sequence[Message],
        *,
        event_context: SessionEventContext | None = None,
    ) -> SessionSnapshot:
        if self.fail_next_append:
            self.fail_next_append = False
            raise OperationalError("simulated database write failure")
        return await super().append(
            session_id, expected_version, messages, event_context=event_context
        )


async def main() -> None:
    store = FailingOnceStore()
    provider = FakeProvider([[TextDelta("answer"), Finish()]])
    events = InMemoryEventSink()
    async with AgentSession("demo", store, provider, event_sink=events) as session:
        try:
            await session.run("first question")
        except SessionStorageError as error:
            print(f"{type(error).__name__} caused by {type(error.__cause__).__name__}")

        for event in events.events:
            print(event.kind, dict(event.payload))
        print(f"durable messages after failure: {len((await store.load('demo')).messages)}")
        print(f"model requests after failure: {len(provider.requests)}")

        result = await session.run("second question")
        print(f"next run: {result.message.content}")


if __name__ == "__main__":
    asyncio.run(main())
