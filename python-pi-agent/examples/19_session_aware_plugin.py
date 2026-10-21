"""Run with PYTHONPATH=python-pi-agent/src python examples/19_session_aware_plugin.py."""

from __future__ import annotations

import asyncio

from pi_agent_py import (
    AgentSession,
    ContextPlan,
    FakeProvider,
    Finish,
    InMemorySessionStore,
    Message,
    PluginAPI,
    PluginHost,
    ProviderRequestContext,
    TextDelta,
)


class TenantContextPlugin:
    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    async def before_provider_request(
        self,
        context: ProviderRequestContext,
        plan: ContextPlan,
    ) -> ContextPlan:
        print("provider request", context.request_id, context.session_id)
        return ContextPlan(
            messages=(
                Message(
                    role="system",
                    content=f"Session identity: {self._session_id}",
                ),
                *plan.messages,
            ),
            source_message_count=plan.source_message_count,
            omitted_message_count=plan.omitted_message_count,
            token_budget=plan.token_budget,
            estimated_tokens=plan.estimated_tokens,
        )

    async def on_run_settled(self, run_id: str) -> None:
        print("settled", self._session_id, run_id)

    async def close(self) -> None:
        print("closed", self._session_id)


async def main() -> None:
    host = PluginHost()

    def plugin(api: PluginAPI) -> None:
        api.register_session_plugin(
            lambda context: TenantContextPlugin(context.session_id)
        )

    await host.load("tenant-context", plugin)
    provider = FakeProvider([[TextDelta("done"), Finish()]])
    store = InMemorySessionStore()
    async with AgentSession(
        "teaching-session",
        store,
        provider,
        plugin_host=host,
    ) as session:
        await session.run("hello")

    print("provider projection:", provider.requests[0])
    print("durable history:", (await store.load("teaching-session")).messages)
    await host.close()


if __name__ == "__main__":
    asyncio.run(main())
