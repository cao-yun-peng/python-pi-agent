from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from pi_agent_py import JsonlSessionStore, Message


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "sessions.jsonl"
        store = JsonlSessionStore(path)

        await store.append(
            "source",
            0,
            [
                Message(role="user", content="第一个问题"),
                Message(role="assistant", content="第一个回答"),
                Message(role="user", content="源分支继续追问"),
            ],
        )
        branch = await store.fork("source", "branch", at_version=2)

        await store.append(
            "branch",
            branch.version,
            [Message(role="user", content="目标分支改问另一个问题")],
        )

        reopened = JsonlSessionStore(path)
        source = await reopened.load("source")
        branch = await reopened.load("branch")

        print("source:", [message.content for message in source.messages])
        print("branch:", [message.content for message in branch.messages])


if __name__ == "__main__":
    asyncio.run(main())
