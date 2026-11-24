"""Two independent Workers, real PG/Redis, deterministic interruption, FakeProvider."""

import argparse
import asyncio
import importlib
import os
from uuid import uuid4

from pi_agent_py.api import (
    AgentRuntimeBuilder,
    CallbackEventSink,
    CommitAssistant,
    DispatchModel,
    DistributedExecution,
    DistributedExecutionSettings,
    ExecutionStepRequest,
    Finish,
    PostgresExecutionStore,
    PostgresSessionStore,
    RedisLeaseManager,
    RedisLeaseSettings,
    RegisteredRecoveryDispatcher,
    RunSubmission,
    SessionConflictError,
    SessionRecoveryWorker,
    SessionStorageError,
    TextDelta,
    execution_operation,
)
from pi_agent_py.testing import FakeProvider


class InterruptAfterAssistant:
    """Fault proxy only: the formal writer commits before the reply is interrupted."""

    def __init__(self, store):
        self.store, self.pending, self.old_grant = store, True, None

    def __getattr__(self, name):
        return getattr(self.store, name)

    def bind_writer(self, grant):
        self.old_grant = grant
        bound, outer = self.store.bind_writer(grant), self

        class Writer:
            async def commit_step(self, operation, command, mutations, **kwargs):
                receipt = await bound.commit_step(
                    operation, command, mutations, **kwargs
                )
                if outer.pending and isinstance(command, CommitAssistant):
                    outer.pending = False
                    raise SessionStorageError("injected after durable assistant")
                return receipt

        return Writer()


async def run(migrate: bool, partial_stream: bool) -> None:
    pg = importlib.import_module("asyncpg")
    redis = importlib.import_module("redis.asyncio")
    retry = importlib.import_module("redis.asyncio.retry")
    backoff = importlib.import_module("redis.backoff")
    pools, clients, workers = [], [], []
    sid, rid = "worker-demo-" + uuid4().hex, uuid4().hex
    sink = CallbackEventSink(
        lambda event: (
            print(event.kind, event.payload)
            if event.kind.startswith("recovery_")
            else None
        )
    )
    try:
        for _ in range(2):
            pools.append(
                await pg.create_pool(
                    os.environ["PI_AGENT_POSTGRES_DSN"],
                    min_size=1,
                    max_size=3,
                    server_settings={"search_path": "pi_recovery_demo"},
                )
            )
            clients.append(
                redis.from_url(
                    os.environ["PI_AGENT_REDIS_URL"],
                    socket_timeout=1,
                    socket_connect_timeout=1,
                    retry=retry.Retry(backoff.NoBackoff(), 0),
                )
            )
        if migrate:
            async with pools[0].acquire() as connection:
                await connection.execute("CREATE SCHEMA IF NOT EXISTS pi_recovery_demo")
        stores = [PostgresExecutionStore(pool) for pool in pools]
        if migrate:
            print("applied_migrations=", await stores[0].ensure_schema())
        proxy = InterruptAfterAssistant(stores[0])
        providers = [
            FakeProvider(
                [[TextDelta("partial"), SessionStorageError("injected mid-stream")]]
                if partial_stream
                else [[TextDelta("durable answer"), Finish()]]
            ),
            FakeProvider(
                [[TextDelta("recovered answer"), Finish()]] if partial_stream else []
            ),
        ]
        for node in range(2):
            reader = PostgresSessionStore(pools[node])
            store = proxy if node == 0 else stores[node]
            leases = RedisLeaseManager(
                clients[node],
                settings=RedisLeaseSettings(key_prefix="pi:recovery:demo:"),
            )
            config = DistributedExecution(
                store,
                leases,
                DistributedExecutionSettings(
                    "demo-" + str(node), "demo:v1", "demo-hash-v1"
                ),
            )

            async def factory(
                receipt, reader=reader, config=config, provider=providers[node]
            ):
                return await (
                    AgentRuntimeBuilder()
                    .with_provider(provider)
                    .with_session_store(reader)
                    .with_event_sink(sink)
                    .with_distributed_execution(
                        config.store, config.leases, settings=config.settings
                    )
                    .with_graceful_shutdown(0)
                    .build()
                )

            dispatcher = RegisteredRecoveryDispatcher(
                reader, config, {("demo:v1", "demo-hash-v1"): factory}, event_sink=sink
            )
            workers.append(SessionRecoveryWorker(config, dispatcher, event_sink=sink))
        await stores[0].submit_run(
            RunSubmission(sid, rid, "hello", rid, "demo:v1", "demo-hash-v1")
        )
        print("session_id=", sid, "run_id=", rid)
        print("worker_A=", (await workers[0].run_once()).attempts)
        interrupted = await stores[0].load_execution(sid, rid)
        print("interrupted_checkpoint=", interrupted.checkpoint.phase)
        print("worker_B=", (await workers[1].run_once()).attempts)
        record = await stores[1].load_execution(sid, rid)
        snapshot = await PostgresSessionStore(pools[1]).load(sid)
        print(
            "state=", record.state, "attempt=", record.attempt, "epoch=", record.epoch
        )
        print(
            "prompt_count=",
            sum(message.role == "user" for message in snapshot.messages),
        )
        print("model_requests_A_B=", [len(provider.requests) for provider in providers])
        command = DispatchModel()
        request = ExecutionStepRequest(
            proxy.old_grant, command, (), snapshot.version, record.revision
        )
        try:
            await (
                stores[0]
                .bind_writer(proxy.old_grant)
                .commit_step(
                    execution_operation(uuid4().hex, request),
                    command,
                    (),
                    expected_session_version=snapshot.version,
                    expected_revision=record.revision,
                )
            )
        except SessionConflictError:
            print("old_writer_rejected=", True)
        else:
            raise AssertionError("old writer unexpectedly committed")
        assert record.state == "completed" and record.attempt == 2
        assert sum(message.role == "user" for message in snapshot.messages) == 1
        assert len(providers[1].requests) == int(partial_stream)
    finally:
        try:
            for worker in reversed(workers):
                await worker.aclose()
        finally:
            try:
                for client in clients:
                    await client.aclose()
            finally:
                for pool in pools:
                    await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--partial-stream", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.migrate, args.partial_stream))
