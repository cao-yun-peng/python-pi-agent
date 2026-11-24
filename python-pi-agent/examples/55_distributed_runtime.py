"""Builder wiring and explicit resume, with FakeProvider and optional crash injection."""

import argparse
import asyncio
import importlib
import os
from uuid import uuid4

from pi_agent_py.api import (
    AgentRuntimeBuilder,
    CallbackEventSink,
    CommitAssistant,
    DistributedExecutionSettings,
    Finish,
    PostgresExecutionStore,
    PostgresSessionStore,
    RedisLeaseManager,
    SessionStorageError,
    TextDelta,
)
from pi_agent_py.testing import FakeProvider


class InterruptAfterAssistant:
    """Teaching-only fault injection: durable success, then process interruption."""

    def __init__(self, store):
        self.store = store
        self.pending = True

    def __getattr__(self, name):
        return getattr(self.store, name)

    def bind_writer(self, grant):
        bound, outer = self.store.bind_writer(grant), self

        class Writer:
            async def commit_step(self, operation, command, mutations, **kwargs):
                receipt = await bound.commit_step(
                    operation, command, mutations, **kwargs
                )
                if outer.pending and isinstance(command, CommitAssistant):
                    outer.pending = False
                    raise SessionStorageError(
                        "injected interruption after durable assistant"
                    )
                return receipt

        return Writer()


async def run(migrate: bool, interrupt: bool) -> None:
    pg = importlib.import_module("asyncpg")
    redis = importlib.import_module("redis.asyncio")
    retry = importlib.import_module("redis.asyncio.retry")
    backoff = importlib.import_module("redis.backoff")
    client = redis.from_url(
        os.environ["PI_AGENT_REDIS_URL"],
        socket_timeout=1,
        socket_connect_timeout=1,
        retry=retry.Retry(backoff.NoBackoff(), 0),
    )
    pool, runtimes = None, []
    try:
        pool = await pg.create_pool(
            os.environ["PI_AGENT_POSTGRES_DSN"], min_size=1, max_size=3
        )
        reader, execution, leases = (
            PostgresSessionStore(pool),
            PostgresExecutionStore(pool),
            RedisLeaseManager(client),
        )
        if migrate:
            print("applied_migrations=", await execution.ensure_schema())
        settings = DistributedExecutionSettings(
            "demo-worker", "demo:v1", "demo-configuration-hash"
        )
        sink = CallbackEventSink(
            lambda event: (
                print(event.kind, event.payload)
                if event.kind
                in {
                    "run_started",
                    "run_completed",
                    "run_failed",
                    "lease_heartbeat_stopped",
                }
                else None
            )
        )

        async def build(provider, store):
            runtime = await (
                AgentRuntimeBuilder()
                .with_provider(provider)
                .with_session_store(reader)
                .with_event_sink(sink)
                .with_graceful_shutdown(0)
                .with_distributed_execution(store, leases, settings=settings)
                .build()
            )
            runtimes.append(runtime)
            return runtime

        provider = FakeProvider([[TextDelta("durable final answer"), Finish()]])
        runtime = await build(
            provider, InterruptAfterAssistant(execution) if interrupt else execution
        )
        sid = "distributed-runtime-demo-" + uuid4().hex
        session = await runtime.open_session(sid)
        handle = await session.submit("hello")
        await handle.events().aclose()
        print("session_id=", sid, "run_id=", handle.run_id)
        try:
            result = await handle.result()
        except SessionStorageError:
            if not interrupt:
                raise
            print(
                "interrupted_checkpoint=",
                (await execution.load_execution(sid, handle.run_id)).checkpoint.phase,
            )
            await runtime.aclose()
            recovered_provider = FakeProvider([])
            recovered = await (await build(recovered_provider, execution)).open_session(
                sid
            )
            result = await recovered.resume(handle.run_id)
            print("resume_model_requests=", len(recovered_provider.requests))
        record = await execution.load_execution(sid, handle.run_id)
        print("result=", result.message.content)
        print("prompt_count=", sum(m.role == "user" for m in result.messages))
        print(
            "state=",
            record.state,
            "request_number=",
            record.checkpoint.request_number,
            "dispatches=",
            record.checkpoint.dispatches,
            "attempt=",
            record.attempt,
        )
    finally:
        try:
            for runtime in reversed(runtimes):
                await runtime.aclose()
        finally:
            try:
                if pool is not None:
                    await pool.close()
            finally:
                await client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--interrupt-after-assistant", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.migrate, args.interrupt_after_assistant))
