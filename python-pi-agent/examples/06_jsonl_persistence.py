from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from pi_agent_py import AgentSession, FakeProvider, Finish, JsonlSessionStore, TextDelta


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write and resume one Agent session across two processes."
    )
    parser.add_argument("command", choices=("seed", "resume"))
    parser.add_argument("path", type=Path)
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    store = JsonlSessionStore(args.path)

    if args.command == "seed":
        snapshot = await store.load("jsonl-demo")
        if snapshot.version != 0:
            raise SystemExit("seed requires an empty session log")

        session = AgentSession(
            "jsonl-demo",
            store,
            FakeProvider([[TextDelta("我记住了。"), Finish()]]),
        )
        await session.run("项目代号是松鼠。")
        print(f"seeded session in {args.path}")
        return

    provider = FakeProvider([[TextDelta("项目代号是松鼠。"), Finish()]])
    session = AgentSession("jsonl-demo", store, provider)
    result = await session.run("项目代号是什么？")

    print(result.message.content)
    print("\n--- context loaded from JSONL ---")
    for message in provider.requests[0]:
        print(f"{message.role}: {message.content}")


if __name__ == "__main__":
    asyncio.run(main())
