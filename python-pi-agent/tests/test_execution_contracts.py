"""Offline run journals and injected-time authority tests; no event loop or I/O."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import inspect
import unittest
from typing import get_type_hints

from pi_agent_py.domain import (
    ControlClaim,
    ControlDelivery,
    DurableEventIdentity,
    Message,
    QueuedControl,
)
from pi_agent_py.errors import (
    SessionConflictError,
    SessionReplayError,
    SessionValidationError,
)
from pi_agent_py.execution_domain import (
    ClaimRun,
    CommitAssistant,
    CommitPrompt,
    CommitToolResults,
    ContinueRun,
    DispatchModel,
    ExecutionAuthority,
    ExecutionGrant,
    AppendExecutionMessages,
    ExhaustRun,
    FinishRun,
    RunCheckpoint,
    RunCommand,
    RunEvent,
    RunRecord,
    RunSubmission,
    OperationIdentity,
    RecordExecutionControl,
)
from pi_agent_py.ports import (
    ExecutionWriter,
    SessionExecutionStore,
    SessionLeaseManager,
    SessionStore,
)
from pi_agent_py.storage.execution_transitions import (
    claim_execution,
    prepare_run_transition,
    release_execution,
    renew_execution,
    replay_run,
    settle_execution,
    validate_execution,
)
from pi_agent_py.storage.transitions import prepare_session_control, replay_session


def ref(sequence: int, session: str = "s") -> DurableEventIdentity:
    return DurableEventIdentity(session, sequence, f"{session}:{sequence}")


class Journal:
    """Adapter stand-in: append accepted plans, reload from detached durable facts."""

    def __init__(self, submission: RunSubmission | None = None) -> None:
        self.submission = submission or RunSubmission(
            "s", "r", "hello", "key", "profile:v1", "hash"
        )
        self.events: tuple[RunEvent, ...] = ()
        self.authority = ExecutionAuthority("s")
        self.now = 1.0
        self.step(self.submission)

    @property
    def record(self) -> RunRecord:
        result = replay_run("s", "r", self.events)
        assert result is not None
        return result

    def step(self, command: RunCommand) -> RunRecord:
        plan = prepare_run_transition(
            "s",
            "r",
            self.events,
            len(self.events),
            command,
            operation_id=f"op-{len(self.events) + 1}",
            authority=self.authority,
            grant=self.authority.grant,
            now=self.now,
        )
        assert plan.event is not None
        self.events += (plan.event,)
        return plan.record

    def claim(self) -> RunRecord:
        self.authority = claim_execution(
            self.authority,
            owner_id=f"w-{self.now}",
            claim_id=f"c-{self.now}",
            now=self.now,
            ttl=10,
            run_id="r",
        )
        assert self.authority.grant is not None
        return self.step(ClaimRun(self.authority.grant))

    def model(self) -> None:
        self.claim()
        self.step(CommitPrompt(ref(1)))

    def boundary(self, *, tools: bool = False) -> None:
        self.model()
        self.step(DispatchModel())
        self.step(
            CommitAssistant(
                ref(2), "tool_calls" if tools else "stop", ("a", "b") if tools else ()
            )
        )


class RunJournalTests(unittest.TestCase):
    def test_ordinary_prompt_acceptance_is_replayable_without_control(self) -> None:
        journal = Journal()
        restored = replay_run("s", "r", tuple(journal.events))
        self.assertEqual(restored, journal.record)
        self.assertEqual(restored.state, "queued")  # type: ignore[union-attr]
        self.assertIsNone(journal.record.submission.control_id)
        self.assertEqual(journal.record.submission.prompt, "hello")

    def test_follow_up_delivered_does_not_complete_run(self) -> None:
        submission = RunSubmission(
            "s", "r", "next", "key", "p", "h", "follow_up", "control"
        )
        journal = Journal(submission)
        journal.claim()
        controls = ()
        for kind, value in (
            ("control_enqueued", QueuedControl("control", "follow_up", "next", "r")),
            ("control_claimed", ControlClaim("control", "c", "w", 20)),
            (
                "control_delivered",
                ControlDelivery("control", "c", Message("user", "next")),
            ),
        ):
            event = prepare_session_control(
                "s", controls, len(controls), kind, value
            ).event
            controls += (event,)
        journal.step(CommitPrompt(ref(3)))
        self.assertEqual(replay_session("s", controls).pending_controls, ())
        self.assertEqual(journal.record.state, "running")
        self.assertEqual(journal.record.checkpoint.phase, "awaiting_model")

    def test_complete_then_replay_and_no_further_dispatch(self) -> None:
        journal = Journal()
        journal.boundary()
        result = journal.step(FinishRun("completed"))
        self.assertEqual(result.checkpoint.phase, "terminal")
        self.assertEqual(result.checkpoint.assistant_event, ref(2))
        with self.assertRaisesRegex(SessionValidationError, "terminal"):
            journal.step(DispatchModel())

    def test_final_assistant_survives_crash_before_completion(self) -> None:
        journal = Journal()
        journal.boundary()
        journal.now = 11
        journal.claim()
        self.assertEqual(journal.record.checkpoint.phase, "awaiting_boundary")
        self.assertEqual(journal.record.checkpoint.dispatches, 1)
        journal.step(FinishRun("completed"))
        self.assertEqual(journal.record.attempt, 2)
        self.assertEqual(journal.record.submission.run_id, "r")

    def test_partial_tool_results_and_unknown_compensation_close_only_missing(
        self,
    ) -> None:
        journal = Journal()
        journal.boundary(tools=True)
        journal.step(CommitToolResults(("a",)))
        self.assertEqual(journal.record.checkpoint.unresolved_tool_ids, ("b",))
        with self.assertRaises(SessionValidationError):
            journal.step(CommitToolResults(("a",)))
        journal.now = 11
        journal.claim()
        journal.step(CommitToolResults(("b",)))
        self.assertEqual(journal.record.checkpoint.phase, "awaiting_boundary")
        with self.assertRaises(SessionValidationError):
            journal.step(FinishRun("completed"))
        journal.step(ContinueRun())
        self.assertEqual(journal.record.checkpoint.request_number, 2)
        self.assertEqual(journal.record.checkpoint.dispatches, 1)

    def test_steering_can_continue_final_assistant_without_resetting_prompt(
        self,
    ) -> None:
        journal = Journal()
        journal.boundary()
        journal.step(ContinueRun((ref(3),)))
        self.assertEqual(journal.record.checkpoint.prompt_event, ref(1))
        with self.assertRaisesRegex(SessionValidationError, "already committed"):
            journal.step(CommitPrompt(ref(4)))

    def test_takeover_cannot_reuse_a_dead_attempt_dispatch_reservation(self) -> None:
        journal = Journal()
        journal.model()
        journal.step(DispatchModel())
        journal.now = 11
        journal.claim()
        with self.assertRaisesRegex(SessionValidationError, "reserved dispatch"):
            journal.step(CommitAssistant(ref(2), "stop"))
        journal.step(DispatchModel())
        journal.step(CommitAssistant(ref(2), "stop"))
        self.assertEqual(journal.record.checkpoint.dispatches, 2)

    def test_all_budgets_survive_takeover(self) -> None:
        journal = Journal(
            replace(
                Journal().submission, max_requests=1, max_dispatches=1, max_attempts=2
            )
        )
        journal.boundary(tools=True)
        journal.now = 11
        journal.claim()
        journal.step(CommitToolResults(("a", "b")))
        with self.assertRaisesRegex(SessionValidationError, "logical request"):
            journal.step(ContinueRun())
        journal.now = 21
        with self.assertRaisesRegex(SessionValidationError, "attempt budget"):
            journal.claim()
        other = Journal(replace(Journal().submission, max_dispatches=1))
        other.model()
        other.step(DispatchModel())
        other.now = 11
        other.claim()
        with self.assertRaisesRegex(SessionValidationError, "dispatch budget"):
            other.step(DispatchModel())

    def test_operation_retry_returns_original_receipt_without_new_event(self) -> None:
        journal = Journal()
        original = journal.record
        journal.claim()
        plan = prepare_run_transition(
            "s",
            "r",
            journal.events,
            0,
            journal.submission,
            operation_id="op-1",
        )
        self.assertIsNone(plan.event)
        self.assertEqual(plan.record, original)
        with self.assertRaisesRegex(SessionValidationError, "different input"):
            prepare_run_transition(
                "s",
                "r",
                journal.events,
                0,
                replace(journal.submission, prompt="changed"),
                operation_id="op-1",
            )

    def test_revision_conflict_and_failure_leave_original_history_unchanged(
        self,
    ) -> None:
        journal = Journal()
        original = journal.events
        with self.assertRaises(SessionConflictError):
            prepare_run_transition(
                "s",
                "r",
                journal.events,
                0,
                FinishRun("cancelled", "user_abort"),
                operation_id="new",
            )
        with self.assertRaises(SessionConflictError):
            journal.step(DispatchModel())
        self.assertEqual(journal.events, original)

    def test_failed_and_cancelled_states_and_queued_cancellation(self) -> None:
        for terminal in ("failed", "cancelled"):
            for phase in (
                "before_prompt",
                "awaiting_model",
                "awaiting_tools",
                "awaiting_boundary",
            ):
                with self.subTest(terminal=terminal, phase=phase):
                    journal = self._at(phase)
                    result = journal.step(FinishRun(terminal, "reason"))
                    self.assertEqual(result.state, terminal)
                    self.assertEqual(result.checkpoint.phase, "terminal")
        journal = Journal()
        journal.step(FinishRun("cancelled", "queued_abort"))
        self.assertEqual(journal.record.attempt, 0)

    @staticmethod
    def _at(phase: str) -> Journal:
        journal = Journal()
        if phase == "queued":
            return journal
        journal.claim()
        if phase == "before_prompt":
            return journal
        journal.step(CommitPrompt(ref(1)))
        if phase == "awaiting_model":
            return journal
        journal.step(DispatchModel())
        journal.step(
            CommitAssistant(
                ref(2),
                "tool_calls" if phase == "awaiting_tools" else "stop",
                ("a",) if phase == "awaiting_tools" else (),
            )
        )
        if phase == "terminal":
            journal.step(FinishRun("completed"))
        return journal

    def test_progress_transition_matrix_rejects_every_illegal_phase(self) -> None:
        cases = (
            (CommitPrompt(ref(4)), {"before_prompt"}),
            (DispatchModel(), {"awaiting_model"}),
            (CommitAssistant(ref(4), "stop"), {"awaiting_model"}),
            (CommitToolResults(("a",)), {"awaiting_tools"}),
            (ContinueRun((ref(4),)), {"awaiting_boundary"}),
            (FinishRun("completed"), {"awaiting_boundary"}),
        )
        for phase in (
            "queued",
            "before_prompt",
            "awaiting_model",
            "awaiting_tools",
            "awaiting_boundary",
            "terminal",
        ):
            for command, allowed in cases:
                with self.subTest(phase=phase, command=command):
                    journal = self._at(phase)
                    if phase == "awaiting_model" and isinstance(
                        command, CommitAssistant
                    ):
                        journal.step(DispatchModel())
                    if phase in allowed:
                        journal.step(command)
                    else:
                        error = (
                            SessionConflictError
                            if phase == "queued" and not isinstance(command, FinishRun)
                            else SessionValidationError
                        )
                        with self.assertRaises(error):
                            journal.step(command)

    def test_claim_and_reaccept_matrix_including_terminal_rejection(self) -> None:
        for phase in (
            "queued",
            "before_prompt",
            "awaiting_model",
            "awaiting_tools",
            "awaiting_boundary",
            "terminal",
        ):
            with self.subTest(phase=phase):
                journal = self._at(phase)
                with self.assertRaises(SessionValidationError):
                    journal.step(journal.submission)
                journal.now = 11
                if phase == "terminal":
                    with self.assertRaises(SessionValidationError):
                        journal.claim()
                else:
                    previous = journal.record
                    result = journal.claim()
                    self.assertEqual(result.state, "running")
                    self.assertEqual(result.attempt, previous.attempt + 1)

    def test_unexhausted_run_cannot_be_forced_to_budget_failure(self) -> None:
        journal = Journal()
        journal.claim()
        journal.authority = claim_execution(
            journal.authority, owner_id="b", claim_id="b", now=11, ttl=10, run_id="r"
        )
        journal.now = 11
        assert journal.authority.grant is not None
        with self.assertRaisesRegex(SessionValidationError, "not exhausted"):
            journal.step(ExhaustRun(journal.authority.grant))

    def test_exhausted_run_can_fail_under_new_grant_without_another_attempt(
        self,
    ) -> None:
        journal = Journal(replace(Journal().submission, max_attempts=1))
        journal.claim()
        assert journal.authority.grant is not None
        with self.assertRaises(SessionConflictError):
            journal.step(ExhaustRun(journal.authority.grant))
        journal.now = 11
        journal.authority = claim_execution(
            journal.authority, owner_id="b", claim_id="b", now=11, ttl=10, run_id="r"
        )
        assert journal.authority.grant is not None
        result = journal.step(ExhaustRun(journal.authority.grant))
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.attempt, 1)
        self.assertEqual(result.epoch, 2)
        self.assertEqual(result.error_code, "attempt_budget_exhausted")

    def test_assistant_requires_dispatch_and_matching_message_identity(self) -> None:
        journal = Journal()
        journal.model()
        with self.assertRaises(SessionValidationError):
            journal.step(CommitAssistant(ref(2), "stop"))
        journal.step(DispatchModel())
        for command in (
            CommitAssistant(ref(2, "other"), "stop"),
            CommitAssistant(ref(1), "stop"),
            CommitAssistant(ref(2), "tool_calls"),
        ):
            with (
                self.subTest(command=command),
                self.assertRaises(SessionValidationError),
            ):
                journal.step(command)

    def test_corrupt_journal_rejected(self) -> None:
        journal = Journal()
        journal.claim()
        first, second = journal.events
        variants = (
            (replace(first, revision=2),),
            (replace(first, session_id="other"),),
            (replace(first, command=replace(journal.submission, prompt="tampered")),),
            (first, replace(second, queue_order=2)),
            (first, replace(second, operation=first.operation)),
            (second,),
        )
        for events in variants:
            with self.subTest(events=events), self.assertRaises(SessionReplayError):
                replay_run("s", "r", events)

    def test_missing_foreign_and_stale_grants_do_not_authorize_progress(self) -> None:
        journal = Journal()
        journal.claim()
        old = journal.authority.grant
        journal.now = 11
        journal.claim()
        for grant in (None, old):
            with self.subTest(grant=grant), self.assertRaises(SessionConflictError):
                prepare_run_transition(
                    "s",
                    "r",
                    journal.events,
                    len(journal.events),
                    CommitPrompt(ref(1)),
                    operation_id="bad",
                    authority=journal.authority,
                    grant=grant,
                    now=11,
                )


class ExecutionAuthorityTests(unittest.TestCase):
    def test_compare_release_does_not_require_live_or_positive_expiry(self) -> None:
        grant = ExecutionGrant("s", "a", "c", 1, 0, run_id="r")
        authority = ExecutionAuthority("s", 1, grant, "r")
        released = release_execution(authority, grant)
        self.assertIsNone(released.grant)
        self.assertEqual(released.active_run_id, "r")

    def test_mutual_exclusion_expiry_takeover_and_monotonic_epoch(self) -> None:
        first = claim_execution(
            ExecutionAuthority("s"),
            owner_id="a",
            claim_id="a",
            now=0,
            ttl=10,
            run_id="r",
        )
        with self.assertRaises(SessionConflictError):
            claim_execution(
                first, owner_id="b", claim_id="b", now=9, ttl=10, run_id="r"
            )
        second = claim_execution(
            first, owner_id="b", claim_id="b", now=10, ttl=10, run_id="r"
        )
        self.assertEqual(second.epoch, 2)
        assert first.grant is not None
        with self.assertRaises(SessionConflictError):
            validate_execution(second, first.grant, now=10)
        with self.assertRaises(SessionConflictError):
            release_execution(second, first.grant)

    def test_renewal_is_ephemeral_and_cannot_revive_expired_grant(self) -> None:
        journal = Journal()
        journal.claim()
        events = journal.events
        grant = journal.authority.grant
        assert grant is not None
        renewed = renew_execution(journal.authority, grant, now=5, ttl=10)
        self.assertEqual(renewed.grant.expires_at, 15)  # type: ignore[union-attr]
        validate_execution(renewed, grant, now=12)
        with self.assertRaises(SessionConflictError):
            renew_execution(renewed, grant, now=15, ttl=10)
        self.assertEqual(journal.events, events)
        released = release_execution(renewed, grant)
        self.assertEqual(released.epoch, 1)
        self.assertEqual(released.active_run_id, "r")
        self.assertEqual(release_execution(released, grant), released)

    def test_maintenance_and_other_runs_cannot_skip_interrupted_active_run(
        self,
    ) -> None:
        authority = ExecutionAuthority("s", 7, active_run_id="r")
        for kwargs in ({"purpose": "maintenance"}, {"run_id": "other"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(SessionConflictError):
                claim_execution(
                    authority, owner_id="w", claim_id="c", now=20, ttl=10, **kwargs
                )
        recovered = claim_execution(
            authority, owner_id="w", claim_id="c", now=20, ttl=10, run_id="r"
        )
        self.assertEqual(recovered.epoch, 8)
        maintenance = claim_execution(
            ExecutionAuthority("s"),
            owner_id="w",
            claim_id="c",
            now=0,
            ttl=10,
            purpose="maintenance",
        )
        self.assertEqual(maintenance.grant.maintenance, "compact")  # type: ignore[union-attr]

    def test_settlement_clears_active_run_only_after_matching_terminal_record(
        self,
    ) -> None:
        journal = Journal()
        journal.boundary()
        grant = journal.authority.grant
        assert grant is not None
        with self.assertRaises(SessionValidationError):
            settle_execution(journal.authority, grant, journal.record, now=1)
        journal.step(FinishRun("completed"))
        settled = settle_execution(journal.authority, grant, journal.record, now=1)
        self.assertIsNone(settled.active_run_id)
        self.assertEqual(settled.epoch, 1)

    def test_non_finite_time_bool_integer_and_mutable_ids_are_rejected(self) -> None:
        for value in (float("nan"), float("inf"), -1, True):
            with self.subTest(value=value), self.assertRaises(SessionValidationError):
                claim_execution(
                    ExecutionAuthority("s"),
                    owner_id="a",
                    claim_id="c",
                    now=0,
                    ttl=value,
                    run_id="r",
                )
        with self.assertRaises(SessionValidationError):
            RunSubmission("s", "r", "hi", "key", "p", "h", max_requests=True)
        with self.assertRaises(SessionValidationError):
            CommitToolResults(["mutable"])  # type: ignore[arg-type]
        with self.assertRaises(SessionValidationError):
            CommitToolResults(("duplicate", "duplicate"))
        with self.assertRaises(SessionValidationError):
            ExecutionGrant("s", "a", "c", 1, 10, purpose="maintenance", run_id="r")
        with self.assertRaises(SessionValidationError):
            RunCheckpoint(phase="awaiting_tools")
        with self.assertRaises(FrozenInstanceError):
            Journal().submission.prompt = "changed"  # type: ignore[misc]


class ExecutionPortTests(unittest.TestCase):
    def test_mutation_and_operation_boundaries_reject_invalid_requests(self) -> None:
        from pi_agent_py.domain import SessionEventContext

        context = SessionEventContext(run_id="r")
        with self.assertRaises(SessionValidationError):
            AppendExecutionMessages((), context)
        with self.assertRaises(SessionValidationError):
            RecordExecutionControl(
                "control_cancelled",
                QueuedControl("c", "follow_up", "hi", "r"),
                context,
            )
        with self.assertRaises(SessionValidationError):
            OperationIdentity("op", "not-a-digest")
        record = Journal().record
        with self.assertRaises(SessionValidationError):
            replace(record, checkpoint=RunCheckpoint(dispatches=1))

    def test_existing_store_protocol_unchanged_and_new_hints_resolve(self) -> None:
        names = {
            name
            for name, value in vars(SessionStore).items()
            if inspect.iscoroutinefunction(value)
        }
        self.assertEqual(
            names,
            {"load", "read_events", "append", "compact", "record_control", "fork"},
        )
        for port in (ExecutionWriter, SessionExecutionStore, SessionLeaseManager):
            for name, value in vars(port).items():
                if not name.startswith("_") and inspect.isfunction(value):
                    with self.subTest(port=port, method=name):
                        self.assertIn("return", get_type_hints(value))


if __name__ == "__main__":
    unittest.main()
