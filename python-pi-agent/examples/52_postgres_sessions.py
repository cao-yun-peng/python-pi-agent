"""Real PG persistence with FakeProvider; set PI_AGENT_POSTGRES_DSN first."""

import argparse
import asyncio
import importlib
import os

from pi_agent_py.api import (
    AgentComponents,
    AgentSession,
    Finish,
    PostgresSessionStore,
    TextDelta,
)
from pi_agent_py.errors import SessionConflictError
from pi_agent_py.testing import FakeProvider


async def run(migrate: bool, session_id: str) -> None:
    driver = importlib.import_module("asyncpg")
    pool = await driver.create_pool(
        os.environ["PI_AGENT_POSTGRES_DSN"], min_size=1, max_size=2, command_timeout=30
    )
    try:
        store = PostgresSessionStore(pool)
        if migrate:
            print(f"applied_migrations={await store.ensure_schema()}")
        before = await store.load(session_id)
        session = AgentSession(
            session_id,
            store,
            AgentComponents(FakeProvider([[TextDelta("persisted offline"), Finish()]])),
        )
        try:
            await session.run("demonstrate persistence")
        finally:
            await session.close()
        reopened = await PostgresSessionStore(pool).load(session_id)
        print(f"before_version={before.version} reopened_version={reopened.version}")
        from pi_agent_py.domain import Message

        try:
            await store.append(session_id, before.version, (Message("user", "stale"),))
        except SessionConflictError:
            print("stale_append=rejected history_unchanged=True")
        for event in await store.read_events(session_id):
            print(
                f"sequence={event.sequence} kind={event.kind} event_id={event.event_id}"
            )
    finally:
        await pool.close()  # the example owns it; Store never closes a borrowed pool.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--session-id", default="postgres-demo")
    args = parser.parse_args()
    asyncio.run(run(args.migrate, args.session_id))


if __name__ == "__main__":
    main()
