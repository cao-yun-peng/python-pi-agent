"""Observe retrieval projection and durable result truncation without network I/O."""

import asyncio
from dataclasses import replace

from pi_agent_py.api import (
    AgentRuntimeBuilder,
    CallbackEventSink,
    ContextPlan,
    Finish,
    InMemorySessionStore,
    Message,
    Middleware,
    ProviderRequestContext,
    ToolCall,
    TruncateToolResults,
    tool,
)
from pi_agent_py.testing import FakeProvider


class FakeRetrieval:
    async def prepare(
        self, context: ProviderRequestContext, plan: ContextPlan
    ) -> ContextPlan:
        # Preserve leading system instructions and keep tool call/result pairs intact.
        index = 0
        while index < len(plan.messages) and plan.messages[index].role == "system":
            index += 1
        reference = Message(
            "system", "Retrieved: notes are stored in the offline catalogue."
        )
        return replace(
            plan, messages=plan.messages[:index] + (reference,) + plan.messages[index:]
        )


@tool
async def read_note() -> str:
    """Read a deliberately long offline note."""
    return "The note contains teaching material. " * 40


async def main() -> None:
    store = InMemorySessionStore()
    provider = FakeProvider(
        [[ToolCall("read", "read_note", {}), Finish("tool_calls")], [Finish()]]
    )

    def trace(event):
        if (
            event.kind.startswith("middleware_")
            or event.kind == "tool_result_transformed"
        ):
            print(event.kind, event.envelope.span_id, dict(event.payload))

    runtime = await (
        AgentRuntimeBuilder()
        .with_provider(provider)
        .with_session_store(store)
        .with_system_prompt("Answer from notes.")
        .with_tools((read_note,))
        .with_middleware(
            (
                Middleware("retrieval", provider_request=FakeRetrieval()),
                Middleware("truncate", tool_result=TruncateToolResults(100)),
            )
        )
        .with_event_sink(CallbackEventSink(trace))
        .build()
    )
    async with runtime:
        await runtime.run("demo", "Read a note")
    snapshot = await store.load("demo")
    print("Request projection:", [m.content[:100] for m in provider.requests[0]])
    print("Durable messages:", [(m.role, m.content) for m in snapshot.messages])
    print("Retrieval is absent from durable history; the truncated result is present.")


if __name__ == "__main__":
    asyncio.run(main())
