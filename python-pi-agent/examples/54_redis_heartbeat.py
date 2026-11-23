"""Run FakeProvider under Redis + PG heartbeats; optionally demonstrate lease loss."""

import argparse
import asyncio
import importlib
import os
from uuid import uuid4

from pi_agent_py.api import (
    AppendExecutionMessages,
    CallbackEventSink,
    ClaimExecutionRequest,
    CommitAssistant,
    CommitPrompt,
    DispatchModel,
    DurableEventIdentity,
    ExecutionStepRequest,
    Finish,
    FinishExecutionRequest,
    FinishRun,
    LeaseHeartbeat,
    LeaseHeartbeatSettings,
    LeaseLostError,
    Message,
    PostgresExecutionStore,
    ProviderRequestContext,
    RedisLeaseManager,
    RunSubmission,
    SessionEventContext,
    TextDelta,
    execution_operation,
)
from pi_agent_py.testing import FakeProvider


async def run(migrate: bool, lose_lease: bool) -> None:
    pg = importlib.import_module("asyncpg")
    redis = importlib.import_module("redis.asyncio")
    # Applications own clients and their retry policy. Never retry lease writes implicitly.
    retry = importlib.import_module("redis.asyncio.retry")
    backoff = importlib.import_module("redis.backoff")
    client = redis.from_url(
        os.environ["PI_AGENT_REDIS_URL"],
        socket_timeout=1,
        socket_connect_timeout=1,
        retry=retry.Retry(backoff.NoBackoff(), 0),
    )
    pool = None
    try:
        pool = await pg.create_pool(
            os.environ["PI_AGENT_POSTGRES_DSN"], min_size=1, max_size=2
        )
        store, leases = PostgresExecutionStore(pool), RedisLeaseManager(client)
        if migrate:
            print("applied_migrations=", await store.ensure_schema())
        session_id = "heartbeat-demo-" + uuid4().hex
        print("session_id=", session_id)
        await store.submit_run(
            RunSubmission(session_id, "run", "hello", "submit", "demo:v1", "demo-hash")
        )
        settings = LeaseHeartbeatSettings(10, 1, 1, 1, 1, 0.5)
        lease = await leases.acquire(session_id, "demo-worker", uuid4().hex, 10)
        assert lease is not None
        guard = None
        grant = None
        try:
            receipt = await store.claim_next(
                lease,
                10,
                execution_operation("claim", ClaimExecutionRequest(lease, 10)),
            )
            assert receipt is not None and receipt.run is not None
            grant = receipt.grant
            assert grant is not None
            guard = LeaseHeartbeat(
                leases,
                store,
                lease,
                grant,
                settings=settings,
                event_sink=CallbackEventSink(
                    lambda event: print(event.kind, event.payload)
                ),
            )

            async def work(scope):
                current = receipt
                context = SessionEventContext(run_id="run")

                async def commit(name, command, mutations=()):
                    nonlocal current
                    scope.ensure_active()
                    # A fingerprint must describe the exact grant captured by this writer.
                    writer = store.bind_writer(scope.grant)
                    request = ExecutionStepRequest(
                        scope.grant,
                        command,
                        mutations,
                        current.session.version,
                        current.run.revision,
                    )
                    current = await writer.commit_step(
                        execution_operation(name, request),
                        command,
                        mutations,
                        expected_session_version=request.expected_session_version,
                        expected_revision=request.expected_revision,
                    )
                    print("checkpoint=", current.run.checkpoint.phase)

                await commit(
                    "prompt",
                    CommitPrompt(
                        DurableEventIdentity(session_id, 1, session_id + ":1")
                    ),
                    (AppendExecutionMessages((Message("user", "hello"),), context),),
                )
                if lose_lease:
                    # Delete only this example's own key; never clear a shared database.
                    await client.delete(leases.key_for_session(session_id))
                    try:
                        await asyncio.Event().wait()
                    finally:
                        print("owned_work_cancelled=True")
                await commit("dispatch", DispatchModel())
                scope.ensure_active()
                provider = FakeProvider([[TextDelta("persisted"), Finish()]])
                text = ""
                async for event in provider.stream(
                    current.session.messages,
                    context=ProviderRequestContext(
                        "request",
                        "attempt",
                        "agent_turn",
                        1,
                        session_id=session_id,
                        run_id="run",
                        turn_id="run:turn:1",
                    ),
                ):
                    if isinstance(event, TextDelta):
                        text += event.text
                await commit(
                    "assistant",
                    CommitAssistant(
                        DurableEventIdentity(session_id, 2, session_id + ":2"), "stop"
                    ),
                    (AppendExecutionMessages((Message("assistant", text),), context),),
                )
                return current

            async def finalize(current, final_grant):
                finish = FinishRun("completed")
                request = FinishExecutionRequest(
                    final_grant, finish, current.session.version, current.run.revision
                )
                await store.finish_run(
                    final_grant,
                    execution_operation("finish", request),
                    finish,
                    expected_session_version=request.expected_session_version,
                    expected_revision=request.expected_revision,
                )

            try:
                await guard.run(work, finalize=finalize)
            except LeaseLostError as error:
                if not lose_lease:
                    raise
                print("lost_reason=", error.reason)
            print("run_state=", (await store.load_execution(session_id, "run")).state)
            print(
                "scope_state=", guard.state, "cleanup_failures=", guard.cleanup_failures
            )
        finally:
            if guard is not None:
                await guard.aclose()
            else:
                try:
                    if grant is not None:
                        await store.release_grant(grant)
                finally:
                    await leases.release(lease)
    finally:
        try:
            if pool is not None:
                await pool.close()
        finally:
            await client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--lose-lease", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.migrate, args.lose_lease))
