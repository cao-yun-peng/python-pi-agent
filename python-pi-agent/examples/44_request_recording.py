"""Record in one process and restore normalized inputs in a later offline process."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path

from pi_agent_py.api import (
    AgentComponents, AgentSession, FileModelRequestStore, Finish, JsonlSessionStore,
    Message, Middleware, ModelRequestTarget, RecordingModelProvider, TextDelta, ToolCall, tool,
)


async def record(root: Path) -> None:
    """Use FakeProvider and a retrieval source that is needed only while recording."""
    from pi_agent_py.testing import FakeProvider

    class Retrieve:
        async def prepare(self, context, plan):
            fact = (root / "reference.txt").read_text(encoding="utf-8")
            return replace(plan, messages=(Message("system", fact),) + plan.messages)

    @tool
    async def echo(value: str) -> str:
        """Return the supplied text."""
        return value

    requests = FileModelRequestStore(root / "requests")
    provider = RecordingModelProvider(
        FakeProvider([[ToolCall("call", "echo", {"value": "checked"}), Finish("tool_calls")],
                      [TextDelta("done"), Finish()]]),
        requests, ModelRequestTarget("fake", "scripted"),
    )
    async with AgentSession("demo", JsonlSessionStore(root / "session.jsonl"), AgentComponents(
        provider, tools=(echo,), middleware=(Middleware("retrieve", provider_request=Retrieve()),),
    )) as session:
        await session.run("Check the retrieved fact.")
    print(json.dumps({"recorded_requests": len(await requests.read_requests("demo"))}))


async def restore(root: Path) -> None:
    """Read recorded arguments without constructing a provider or retrieval hook."""
    requests = sorted(
        await FileModelRequestStore(root / "requests").read_requests("demo"),
        key=lambda request: request.context.request_number,
    )
    history = await JsonlSessionStore(root / "session.jsonl").load("demo")
    print(json.dumps({
        "retrieved": [request.messages[0].content for request in requests],
        "tool_names": [[tool.name for tool in request.tools] for request in requests],
        "request_numbers": [request.context.request_number for request in requests],
        "history_excludes_retrieval": all(message.role != "system" for message in history.messages),
    }, ensure_ascii=True))


def main() -> None:
    """Select the record or offline restore phase and an explicit data directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("record", "restore"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    asyncio.run(record(args.root) if args.phase == "record" else restore(args.root))


if __name__ == "__main__":
    main()
