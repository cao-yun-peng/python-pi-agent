"""Exercise PG execution storage with FakeProvider and synthetic coordination."""

import argparse
import asyncio
import importlib
import os
from uuid import uuid4

from pi_agent_py.api import (
    AppendExecutionMessages,
    ClaimExecutionRequest,
    CommitAssistant,
    CommitPrompt,
    DispatchModel,
    DurableEventIdentity,
    ExecutionStepRequest,
    FinishExecutionRequest,
    FinishRun,
    Message,
    PostgresExecutionStore,
    ProviderRequestContext,
    RunSubmission,
    SessionEventContext,
    SessionLease,
    TextDelta,
    Finish,
    execution_operation,
)
from pi_agent_py.testing import FakeProvider


async def run(migrate: bool) -> None:
    driver = importlib.import_module("asyncpg")
    pool = await driver.create_pool(
        os.environ["PI_AGENT_POSTGRES_DSN"], min_size=1, max_size=2
    )
    try:
        store = PostgresExecutionStore(pool)
        if migrate:
            print("applied_migrations=", await store.ensure_schema())
        session_id, run_id = "execution-demo-" + uuid4().hex, "run"
        accepted = await store.submit_run(
            RunSubmission(
                session_id, run_id, "hello", "submission", "demo:v1", "demo-hash"
            )
        )
        print(f"accepted={accepted.state} revision={accepted.revision}")
        now = await pool.fetchval(
            "SELECT extract(epoch FROM clock_timestamp())::double precision"
        )
        # Stage 5 supplies a real LeaseManager. This is a deterministic boundary fixture.
        lease = SessionLease(
            session_id, "demo-worker", "demo-claim", "demo-nonce", now + 60
        )
        receipt = await store.claim_next(
            lease, 30, execution_operation("claim", ClaimExecutionRequest(lease, 30))
        )
        assert (
            receipt is not None
            and receipt.grant is not None
            and receipt.run is not None
        )
        writer = store.bind_writer(receipt.grant)

        async def commit(name, command, mutations=()):
            nonlocal receipt
            request = ExecutionStepRequest(
                receipt.grant,
                command,
                mutations,
                receipt.session.version,
                receipt.run.revision,
            )
            operation = execution_operation(name, request)
            receipt = await writer.commit_step(
                operation,
                command,
                mutations,
                expected_session_version=request.expected_session_version,
                expected_revision=request.expected_revision,
            )
            print(
                f"step={name} phase={receipt.run.checkpoint.phase} version={receipt.session.version} revision={receipt.run.revision}"
            )

        context = SessionEventContext(run_id=run_id)
        await commit(
            "prompt",
            CommitPrompt(DurableEventIdentity(session_id, 1, session_id + ":1")),
            (AppendExecutionMessages((Message("user", "hello"),), context),),
        )
        prompt_receipt = receipt
        await commit("dispatch", DispatchModel())
        provider = FakeProvider([[TextDelta("persisted"), Finish()]])
        text = ""
        async for event in provider.stream(
            receipt.session.messages,
            context=ProviderRequestContext(
                "request",
                "attempt",
                "agent_turn",
                1,
                session_id=session_id,
                run_id=run_id,
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
        finish = FinishRun("completed")
        request = FinishExecutionRequest(
            receipt.grant, finish, receipt.session.version, receipt.run.revision
        )
        receipt = await store.finish_run(
            receipt.grant,
            execution_operation("finish", request),
            finish,
            expected_session_version=request.expected_session_version,
            expected_revision=request.expected_revision,
        )
        historical = await store.resolve_operation(session_id, "prompt")
        print(f"terminal={receipt.run.state} active_grant={receipt.grant}")
        print(
            f"original_receipt_unchanged={historical == prompt_receipt} historical_version={historical.session.version}"
        )
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate", action="store_true")
    asyncio.run(run(parser.parse_args().migrate))
