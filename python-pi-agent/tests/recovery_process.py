"""Integration child: signal a partial stream, then remain alive for parent kill."""

import asyncio
import importlib
import json
import os

from pi_agent_py.api import (
    AgentRuntimeBuilder,
    DistributedExecution,
    DistributedExecutionSettings,
    PostgresSessionStore,
    PostgresExecutionStore,
    RedisLeaseManager,
    RedisLeaseSettings,
    RegisteredRecoveryDispatcher,
    SessionRecoveryWorker,
    TextDelta,
)
from pi_agent_py.testing import FakeProvider, WaitUntilCancelled


class BarrierProvider(FakeProvider):
    def __init__(self):
        super().__init__([[TextDelta("uncommitted partial"), WaitUntilCancelled()]])

    async def stream(self, messages, tools=(), *, context):
        async for event in super().stream(messages, tools, context=context):
            yield event
            if isinstance(event, TextDelta):
                print(
                    json.dumps({"barrier": "partial_stream", "run_id": context.run_id}),
                    flush=True,
                )


async def main():
    driver = importlib.import_module("asyncpg")
    redis = importlib.import_module("redis.asyncio")
    retry = importlib.import_module("redis.asyncio.retry")
    backoff = importlib.import_module("redis.backoff")
    pool = await driver.create_pool(
        os.environ["PI_AGENT_POSTGRES_TEST_DSN"],
        min_size=1,
        max_size=3,
        server_settings={"search_path": os.environ["PI_AGENT_RECOVERY_SCHEMA"]},
    )
    client = redis.from_url(
        os.environ["PI_AGENT_REDIS_TEST_URL"],
        socket_timeout=1,
        socket_connect_timeout=1,
        retry=retry.Retry(backoff.NoBackoff(), 0),
    )
    worker = None
    try:
        reader, store = PostgresSessionStore(pool), PostgresExecutionStore(pool)
        leases = RedisLeaseManager(
            client,
            settings=RedisLeaseSettings(
                key_prefix=os.environ["PI_AGENT_RECOVERY_PREFIX"]
            ),
        )
        settings = DistributedExecutionSettings("process-A", "profile:v1", "hash")
        execution = DistributedExecution(store, leases, settings)

        async def factory(receipt):
            return await (
                AgentRuntimeBuilder()
                .with_provider(BarrierProvider())
                .with_session_store(reader)
                .with_distributed_execution(store, leases, settings=settings)
                .with_graceful_shutdown(0)
                .build()
            )

        dispatcher = RegisteredRecoveryDispatcher(
            reader, execution, {("profile:v1", "hash"): factory}
        )
        worker = SessionRecoveryWorker(execution, dispatcher)
        await worker.start()
        await asyncio.Event().wait()
    finally:
        if worker is not None:
            await worker.aclose()
        await client.aclose()
        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__}), flush=True)
        raise SystemExit(1)
