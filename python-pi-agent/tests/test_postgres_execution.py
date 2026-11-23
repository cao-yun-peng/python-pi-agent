"""Explicit PostgreSQL execution-store acceptance tests, no model API."""

from contextlib import asynccontextmanager
from dataclasses import replace
import unittest
import asyncio

from pi_agent_py.domain import DurableEventIdentity, Message, SessionEventContext
from pi_agent_py.errors import (
    SessionConflictError,
    SessionValidationError,
    SessionCommitUnknownError,
    SessionStorageError,
    SessionReplayError,
)
from pi_agent_py.execution_domain import (
    AppendExecutionMessages,
    ClaimExecutionRequest,
    CommitPrompt,
    DispatchModel,
    CommitAssistant,
    ExecutionStepRequest,
    FinishExecutionRequest,
    FinishRun,
    RunSubmission,
    SessionLease,
)
from pi_agent_py.storage.execution import PostgresExecutionStore
from pi_agent_py.storage.execution_codec import execution_operation
from postgres_fixture import PostgresFixture, _DSN


class LostReplyPool:
    """Commit for real, then lose exactly one COMMIT reply at the pool boundary."""

    def __init__(self, pool):
        self.pool = pool
        self.lose = True

    @asynccontextmanager
    async def acquire(self, *, timeout):
        async with self.pool.acquire(timeout=timeout) as connection:
            outer = self

            class Proxy:
                def __getattr__(self, name):
                    return getattr(connection, name)

                @asynccontextmanager
                async def transaction(self, **kwargs):
                    async with connection.transaction(**kwargs):
                        yield
                    if outer.lose:
                        outer.lose = False
                        raise ConnectionError("injected lost COMMIT reply")

            yield Proxy()


@unittest.skipUnless(_DSN, "set PI_AGENT_POSTGRES_TEST_DSN for migration integration")
class ExecutionMigrationTests(PostgresFixture, unittest.IsolatedAsyncioTestCase):
    legacy_schema = True

    async def test_upgrade_001_preserves_existing_local_history(self):
        await self.store.append("local", 0, (Message("user", "existing"),))
        self.assertEqual(await self.store.ensure_schema(), (2,))
        self.assertEqual(
            (await self.store.load("local")).messages, (Message("user", "existing"),)
        )
        execution = PostgresExecutionStore(self.pool)
        record = await execution.submit_run(
            RunSubmission("new", "r", "hello", "key", "profile", "hash")
        )
        self.assertEqual(record.state, "queued")


@unittest.skipUnless(_DSN, "set PI_AGENT_POSTGRES_TEST_DSN for execution integration")
class PostgresExecutionTests(PostgresFixture, unittest.IsolatedAsyncioTestCase):
    # Reuse the isolated schema fixture, not the base Store test methods below.
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.execution = PostgresExecutionStore(self.pool)

    async def lease(self, claim="c", ttl=60):
        now = await self.pool.fetchval(
            "SELECT extract(epoch FROM clock_timestamp())::double precision"
        )
        return SessionLease("s", "worker", claim, "nonce-" + claim, now + ttl)

    async def accept_and_claim(self):
        await self.execution.submit_run(
            RunSubmission("s", "r", "hello", "key", "profile", "hash")
        )
        lease = await self.lease()
        operation = execution_operation("claim", ClaimExecutionRequest(lease, 30))
        return await self.execution.claim_next(lease, 30, operation)

    async def step(self, receipt, command, mutations=(), op="step"):
        request = ExecutionStepRequest(
            receipt.grant,
            command,
            mutations,
            receipt.session.version,
            receipt.run.revision,
        )
        return await self.execution.bind_writer(receipt.grant).commit_step(
            execution_operation(op, request),
            command,
            mutations,
            expected_session_version=request.expected_session_version,
            expected_revision=request.expected_revision,
        )

    async def prompt(self, receipt, op="prompt"):
        return await self.step(
            receipt,
            CommitPrompt(
                DurableEventIdentity(
                    "s", receipt.session.version + 1, f"s:{receipt.session.version + 1}"
                )
            ),
            (
                AppendExecutionMessages(
                    (Message("user", receipt.run.submission.prompt),),
                    SessionEventContext(run_id=receipt.run.submission.run_id),
                ),
            ),
            op=op,
        )

    async def final_boundary(self):
        receipt = await self.prompt(await self.accept_and_claim())
        receipt = await self.step(receipt, DispatchModel(), op="dispatch")
        return await self.step(
            receipt,
            CommitAssistant(DurableEventIdentity("s", 2, "s:2"), "stop"),
            (
                AppendExecutionMessages(
                    (Message("assistant", "done"),), SessionEventContext(run_id="r")
                ),
            ),
            op="assistant",
        )

    async def finish(self, receipt, finish=FinishRun("completed"), op="finish"):
        request = FinishExecutionRequest(
            receipt.grant, finish, receipt.session.version, receipt.run.revision
        )
        return await self.execution.finish_run(
            receipt.grant,
            execution_operation(op, request),
            finish,
            expected_session_version=receipt.session.version,
            expected_revision=receipt.run.revision,
        )

    async def test_concurrent_claim_and_same_operation_are_serialized_in_pg(self):
        await self.execution.submit_run(
            RunSubmission("s", "r", "hello", "key", "profile", "hash")
        )
        other = PostgresExecutionStore(await self.new_pool())

        async def claim(i):
            lease = await self.lease(str(i))
            store = self.execution if i % 2 else other
            return await store.claim_next(
                lease,
                30,
                execution_operation(f"claim-{i}", ClaimExecutionRequest(lease, 30)),
            )

        results = await asyncio.gather(
            *(claim(i) for i in range(10)), return_exceptions=True
        )
        self.assertEqual(sum(isinstance(r, SessionConflictError) for r in results), 9)
        receipt = next(r for r in results if not isinstance(r, BaseException))
        copies = await asyncio.gather(
            *(self.prompt(receipt, op="same-step") for _ in range(10))
        )
        self.assertTrue(all(r == copies[0] for r in copies))
        self.assertEqual(copies[0].session.version, 1)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM run_events"), 3)

    async def test_forged_digest_does_not_hide_changed_message(self):
        receipt = await self.accept_and_claim()
        command = CommitPrompt(DurableEventIdentity("s", 1, "s:1"))
        context = SessionEventContext(run_id="r")
        mutations = (AppendExecutionMessages((Message("user", "hello"),), context),)
        operation = execution_operation(
            "forged",
            ExecutionStepRequest(
                receipt.grant, command, mutations, 0, receipt.run.revision
            ),
        )
        await self.step(receipt, command, mutations, op="forged")
        with self.assertRaisesRegex(SessionValidationError, "digest"):
            await self.execution.commit_step(
                receipt.grant,
                operation,
                command,
                (AppendExecutionMessages((Message("user", "changed"),), context),),
                expected_session_version=0,
                expected_revision=receipt.run.revision,
            )

    async def test_invalid_reference_and_driver_failure_rollback_entire_step(self):
        receipt = await self.accept_and_claim()
        with self.assertRaises(SessionValidationError):
            await self.step(
                receipt,
                CommitPrompt(DurableEventIdentity("s", 99, "s:99")),
                (
                    AppendExecutionMessages(
                        (Message("user", "hello"),), SessionEventContext(run_id="r")
                    ),
                ),
            )
        self.assertIsNone(await self.execution.resolve_operation("s", "step"))
        await self.pool.execute(
            "CREATE FUNCTION reject_message() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected'; END $$"
        )
        await self.pool.execute(
            "CREATE TRIGGER reject_message BEFORE INSERT ON session_events FOR EACH ROW EXECUTE FUNCTION reject_message()"
        )
        with self.assertRaises(SessionStorageError):
            await self.prompt(receipt)
        self.assertEqual(await self.execution.load_execution("s", "r"), receipt.run)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM run_events"), 2)
        self.assertEqual((await self.store.load("s")).version, 0)
        self.assertIsNone(await self.execution.resolve_operation("s", "prompt"))

    async def test_follow_up_delivery_and_partial_tool_results_are_atomic(self):
        from pi_agent_py.domain import ControlDelivery, ToolCall
        from pi_agent_py.execution_domain import (
            DeliverExecutionControl,
            CommitToolResults,
            ContinueRun,
        )

        await self.execution.submit_run(
            RunSubmission("s", "r", "hello", "key", "profile", "hash", "follow_up", "q")
        )
        lease = await self.lease()
        receipt = await self.execution.claim_next(
            lease, 30, execution_operation("claim", ClaimExecutionRequest(lease, 30))
        )
        self.assertEqual(receipt.session.version, 2)
        command = CommitPrompt(DurableEventIdentity("s", 3, "s:3"))
        receipt = await self.step(
            receipt,
            command,
            (
                DeliverExecutionControl(
                    ControlDelivery(
                        "q", receipt.grant.claim_id, Message("user", "hello")
                    ),
                    SessionEventContext(run_id="r"),
                ),
            ),
        )
        self.assertEqual(receipt.session.pending_controls, ())
        self.assertEqual(receipt.run.state, "running")
        receipt = await self.step(receipt, DispatchModel(), op="dispatch")
        assistant = Message(
            "assistant",
            "",
            tool_calls=(ToolCall("a", "read", {}), ToolCall("b", "read", {})),
        )
        receipt = await self.step(
            receipt,
            CommitAssistant(
                DurableEventIdentity("s", 4, "s:4"), "tool_calls", ("a", "b")
            ),
            (AppendExecutionMessages((assistant,), SessionEventContext(run_id="r")),),
            op="assistant",
        )
        for call in ("a", "b"):
            tool = Message(
                "tool", "unknown", tool_call_id=call, tool_name="read", is_error=True
            )
            receipt = await self.step(
                receipt,
                CommitToolResults((call,)),
                (AppendExecutionMessages((tool,), SessionEventContext(run_id="r")),),
                op="tool-" + call,
            )
        self.assertEqual(receipt.run.checkpoint.phase, "awaiting_boundary")
        receipt = await self.step(receipt, ContinueRun(), op="continue")
        self.assertEqual(receipt.run.checkpoint.request_number, 2)

    async def test_fifo_queued_cancellation_and_release_preserve_interrupted_run(self):
        from pi_agent_py.execution_domain import CancelQueuedRequest

        first = await self.execution.submit_run(
            RunSubmission("s", "first", "one", "one", "p", "h")
        )
        second = await self.execution.submit_run(
            RunSubmission("s", "second", "two", "two", "p", "h", "follow_up", "q")
        )
        req = CancelQueuedRequest("s", "first", "abort", first.revision)
        await self.execution.cancel_queued(
            "s",
            "first",
            execution_operation("cancel", req),
            "abort",
            expected_revision=first.revision,
        )
        lease = await self.lease()
        claimed = await self.execution.claim_next(
            lease, 30, execution_operation("claim", ClaimExecutionRequest(lease, 30))
        )
        self.assertEqual(claimed.run.submission.run_id, "second")
        self.assertGreater(second.queue_order, first.queue_order)
        self.assertTrue(await self.execution.release_grant(claimed.grant))
        self.assertEqual(await self.execution.list_candidates(10), ("s",))
        new_lease = await self.lease("new")
        resumed = await self.execution.claim_next(
            new_lease,
            30,
            execution_operation("resume", ClaimExecutionRequest(new_lease, 30)),
        )
        self.assertEqual(resumed.run.attempt, 2)
        self.assertFalse(await self.execution.release_grant(claimed.grant))

    async def test_maintenance_is_compact_only_and_renewal_does_not_append_journal(
        self,
    ):
        from pi_agent_py.domain import SessionCompaction
        from pi_agent_py.execution_domain import CompactExecutionHistory

        receipt = await self.final_boundary()
        original_count = await self.pool.fetchval("SELECT count(*) FROM run_events")
        renewed = await self.execution.renew_grant(receipt.grant, 60)
        self.assertGreater(renewed.expires_at, receipt.grant.expires_at)
        self.assertEqual(
            await self.pool.fetchval("SELECT count(*) FROM run_events"), original_count
        )
        await self.finish(receipt)
        lease = await self.lease("maintenance")
        grant = await self.execution.claim_maintenance(
            lease,
            30,
            execution_operation(
                "maint", ClaimExecutionRequest(lease, 30, "maintenance")
            ),
        )
        mutations = (
            CompactExecutionHistory(
                SessionCompaction(0, 2, Message("user", "summary")),
                SessionEventContext(),
            ),
        )
        request = ExecutionStepRequest(grant, None, mutations, 2, 0)
        compacted = await self.execution.bind_writer(grant).commit_step(
            execution_operation("compact", request),
            None,
            mutations,
            expected_session_version=2,
            expected_revision=0,
        )
        self.assertIsNone(compacted.run)
        self.assertEqual(compacted.session.messages, (Message("user", "summary"),))
        bad = (
            AppendExecutionMessages(
                (Message("user", "unsafe"),), SessionEventContext()
            ),
        )
        request = ExecutionStepRequest(grant, None, bad, 3, 0)
        with self.assertRaises(SessionValidationError):
            await self.execution.commit_step(
                grant,
                execution_operation("bad", request),
                None,
                bad,
                expected_session_version=3,
                expected_revision=0,
            )

    async def test_finalization_serializes_with_accepted_steering(self):
        from pi_agent_py.domain import QueuedControl, ControlClaim, ControlDelivery
        from pi_agent_py.execution_domain import (
            RecordExecutionControl,
            DeliverExecutionControl,
            ContinueRun,
        )

        receipt = await self.final_boundary()
        context = SessionEventContext(run_id="r")
        original = receipt
        queued = RecordExecutionControl(
            "control_enqueued",
            QueuedControl("steer", "steer", "redirect", "r", "r"),
            context,
        )
        receipt = await self.step(receipt, None, (queued,), op="steer")
        with self.assertRaises(SessionConflictError):
            await self.finish(original)
        with self.assertRaises(SessionValidationError):
            await self.finish(receipt)
        claimed = RecordExecutionControl(
            "control_claimed",
            ControlClaim("steer", "steer-claim", "worker", receipt.grant.expires_at),
            context,
        )
        receipt = await self.step(receipt, None, (claimed,), op="steer-claim")
        ref = DurableEventIdentity("s", 5, "s:5")
        delivered = DeliverExecutionControl(
            ControlDelivery("steer", "steer-claim", Message("user", "redirect")),
            context,
        )
        continued = await self.step(
            receipt, ContinueRun((ref,)), (delivered,), op="steer-deliver"
        )
        self.assertEqual(continued.run.checkpoint.request_number, 2)
        self.assertEqual(continued.session.pending_controls, ())

    async def test_attempt_exhaustion_is_durable_without_another_dispatch(self):
        submission = RunSubmission("s", "r", "hi", "key", "p", "h", max_attempts=1)
        await self.execution.submit_run(submission)
        lease = await self.lease()
        receipt = await self.execution.claim_next(
            lease, 30, execution_operation("claim", ClaimExecutionRequest(lease, 30))
        )
        await self.execution.release_grant(receipt.grant)
        next_lease = await self.lease("new")
        failed = await self.execution.claim_next(
            next_lease,
            30,
            execution_operation("exhaust", ClaimExecutionRequest(next_lease, 30)),
        )
        self.assertEqual(failed.run.state, "failed")
        self.assertEqual(failed.run.attempt, 1)
        self.assertIsNone(failed.grant)

    async def test_renewal_cannot_revive_expired_grant_and_local_mode_stays_local(self):
        receipt = await self.accept_and_claim()
        await self.pool.execute(
            "UPDATE sessions SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE session_id='s'"
        )
        with self.assertRaises(SessionConflictError):
            await self.execution.renew_grant(receipt.grant, 30)
        await self.store.append("local", 0, (Message("user", "local"),))
        with self.assertRaises(SessionConflictError):
            await self.execution.submit_run(
                RunSubmission("local", "r", "hi", "k", "p", "h")
            )

    async def test_corrupt_journal_or_receipt_is_replay_error(self):
        await self.accept_and_claim()
        await self.pool.execute(
            "UPDATE session_operations SET receipt=jsonb_set(receipt,'{session_id}','\"foreign\"'::jsonb) WHERE operation_id='claim'"
        )
        with self.assertRaises(SessionReplayError):
            await self.execution.resolve_operation("s", "claim")
        await self.pool.execute(
            "UPDATE session_runs SET record=jsonb_set(record,'{checkpoint,request_number}','2'::jsonb) WHERE run_id='r'"
        )
        with self.assertRaises(SessionReplayError):
            await self.execution.load_execution("s", "r")

    async def test_cancellation_after_run_progress_before_session_insert_rolls_back(
        self,
    ):
        receipt = await self.accept_and_claim()
        await self.pool.execute(
            "CREATE FUNCTION wait_for_cancel() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_advisory_xact_lock(18990617); RETURN NEW; END $$"
        )
        await self.pool.execute(
            "CREATE TRIGGER wait_for_cancel BEFORE INSERT ON session_events FOR EACH ROW EXECUTE FUNCTION wait_for_cancel()"
        )
        async with self.pool.acquire() as blocker:
            async with blocker.transaction():
                await blocker.execute("SELECT pg_advisory_xact_lock(18990617)")
                task = asyncio.create_task(self.prompt(receipt))

                async def wait_for_lock():
                    while not await self.admin.fetchval(
                        "SELECT count(*) FROM pg_stat_activity WHERE application_name=$1 AND wait_event_type='Lock'",
                        self.schema,
                    ):
                        await asyncio.sleep(0)

                await asyncio.wait_for(wait_for_lock(), 5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(await self.execution.load_execution("s", "r"), receipt.run)
        self.assertIsNone(await self.execution.resolve_operation("s", "prompt"))

    async def test_submission_key_replays_original_acceptance(self):
        submission = RunSubmission("s", "r", "hello", "key", "profile", "hash")
        original = await self.execution.submit_run(submission)
        receipt = await self.accept_and_claim()
        self.assertEqual(await self.execution.submit_run(submission), original)
        self.assertEqual(
            (await self.execution.load_execution("s", "r")).state, "running"
        )
        with self.assertRaises(SessionValidationError):
            await self.execution.submit_run(replace(submission, prompt="changed"))
        self.assertEqual(receipt.run.attempt, 1)

    async def test_checkpoint_and_cached_result_survive_reopen(self):
        receipt = await self.accept_and_claim()
        command = CommitPrompt(DurableEventIdentity("s", 1, "s:1"))
        mutations = (
            AppendExecutionMessages(
                (Message("user", "hello"),), SessionEventContext(run_id="r")
            ),
        )
        first = await self.step(receipt, command, mutations)
        dispatched = await self.step(first, DispatchModel(), op="dispatch")
        reopened = PostgresExecutionStore(await self.new_pool())
        self.assertEqual(await reopened.resolve_operation("s", "step"), first)
        duplicate = await self.step(receipt, command, mutations)
        self.assertEqual(duplicate, first)
        self.assertEqual((await reopened.load_execution("s", "r")), dispatched.run)
        changed = (
            AppendExecutionMessages(
                (Message("user", "different"),), SessionEventContext(run_id="r")
            ),
        )
        with self.assertRaises(SessionValidationError):
            await self.step(receipt, command, changed)

    async def test_stale_epoch_and_unbound_store_rejected(self):
        old = await self.accept_and_claim()
        await self.pool.execute(
            "UPDATE sessions SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE session_id='s'"
        )
        lease = await self.lease("new")
        new = await self.execution.claim_next(
            lease, 30, execution_operation("takeover", ClaimExecutionRequest(lease, 30))
        )
        self.assertEqual(new.grant.epoch, old.grant.epoch + 1)
        with self.assertRaises(SessionConflictError):
            await self.step(old, DispatchModel(), op="old-write")
        with self.assertRaises(SessionConflictError):
            await self.store.append("s", 0, (Message("user", "unsafe"),))
        with self.assertRaises(SessionConflictError):
            self.execution.bind_writer(None)

    async def test_real_commit_with_lost_reply_is_queryable_and_not_duplicated(self):
        receipt = await self.accept_and_claim()
        uncertain = PostgresExecutionStore(LostReplyPool(self.pool))
        command = CommitPrompt(DurableEventIdentity("s", 1, "s:1"))
        mutations = (
            AppendExecutionMessages(
                (Message("user", "hello"),), SessionEventContext(run_id="r")
            ),
        )
        request = ExecutionStepRequest(
            receipt.grant, command, mutations, 0, receipt.run.revision
        )
        operation = execution_operation("uncertain", request)
        with self.assertRaises(SessionCommitUnknownError) as caught:
            await uncertain.commit_step(
                receipt.grant,
                operation,
                command,
                mutations,
                expected_session_version=0,
                expected_revision=receipt.run.revision,
            )
        self.assertEqual(caught.exception.operation_id, "uncertain")
        resolved = await self.execution.resolve_operation("s", "uncertain")
        self.assertEqual(resolved.session.version, 1)
        self.assertEqual(
            await self.execution.commit_step(
                receipt.grant,
                operation,
                command,
                mutations,
                expected_session_version=0,
                expected_revision=receipt.run.revision,
            ),
            resolved,
        )

    async def test_complete_releases_active_run_and_never_requeues(self):
        receipt = await self.accept_and_claim()
        receipt = await self.step(
            receipt,
            CommitPrompt(DurableEventIdentity("s", 1, "s:1")),
            (
                AppendExecutionMessages(
                    (Message("user", "hello"),), SessionEventContext(run_id="r")
                ),
            ),
        )
        receipt = await self.step(receipt, DispatchModel(), op="dispatch")
        receipt = await self.step(
            receipt,
            CommitAssistant(DurableEventIdentity("s", 2, "s:2"), "stop"),
            (
                AppendExecutionMessages(
                    (Message("assistant", "done"),), SessionEventContext(run_id="r")
                ),
            ),
            op="assistant",
        )
        finish = FinishRun("completed")
        request = FinishExecutionRequest(receipt.grant, finish, 2, receipt.run.revision)
        result = await self.execution.finish_run(
            receipt.grant,
            execution_operation("finish", request),
            finish,
            expected_session_version=2,
            expected_revision=receipt.run.revision,
        )
        self.assertEqual(result.run.state, "completed")
        self.assertEqual(await self.execution.list_candidates(10), ())
        self.assertIsNone(
            await self.pool.fetchval(
                "SELECT active_run_id FROM sessions WHERE session_id='s'"
            )
        )
