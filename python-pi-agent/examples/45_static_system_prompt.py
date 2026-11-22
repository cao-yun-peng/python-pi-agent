"""Run from python-pi-agent: python examples/45_static_system_prompt.py."""

import asyncio

from pi_agent_py.api import (
    AgentRuntimeBuilder, Finish, InMemorySessionStore, ReadTool, TextDelta,
    coding_profile,
)
from pi_agent_py.testing import FakeProvider, InMemoryWorkspaceFiles


async def main() -> None:
    provider = FakeProvider([[TextDelta("Ready."), Finish()]])
    store = InMemorySessionStore()
    files = InMemoryWorkspaceFiles({"note.txt": b"example"})
    runtime = await (AgentRuntimeBuilder().with_provider(provider)
                     .with_session_store(store).with_profile(coding_profile())
                     .with_tools((ReadTool(files),)).build())
    async with runtime:
        await runtime.run("prompt-demo", "Explain the available read operation.")
    print("MODEL INPUT\n" + provider.requests[0][0].content)
    snapshot = await store.load("prompt-demo")
    print("DURABLE ROLES:", [message.role for message in snapshot.messages])
    assert snapshot.messages[0].content == provider.requests[0][0].content


if __name__ == "__main__":
    asyncio.run(main())
