"""Run execution contracts offline: final-message recovery and stale-owner rejection.

Run from repository root: .venv/Scripts/python.exe python-pi-agent/examples/50_execution_journal.py
This illustrates persistable plans, not a database adapter or a model request.
"""

from pi_agent_py.domain import DurableEventIdentity
from pi_agent_py.errors import SessionConflictError
from pi_agent_py.execution_domain import (
    ClaimRun,
    CommitAssistant,
    CommitPrompt,
    DispatchModel,
    ExecutionAuthority,
    FinishRun,
    RunCommand,
    RunEvent,
    RunSubmission,
)
from pi_agent_py.storage.execution_transitions import (
    claim_execution,
    prepare_run_transition,
    replay_run,
    settle_execution,
    validate_execution,
)


def main() -> None:
    events: tuple[RunEvent, ...] = ()
    authority = ExecutionAuthority("demo")
    now = 0.0

    def commit(command: RunCommand) -> None:
        nonlocal events
        plan = prepare_run_transition(
            "demo",
            "run-1",
            events,
            len(events),
            command,
            operation_id=f"operation-{len(events) + 1}",
            authority=authority,
            grant=authority.grant,
            now=now,
        )
        assert plan.event is not None
        # A real adapter atomically persists this fact with session mutations.
        events += (plan.event,)
        record = plan.record
        print(
            f"revision={record.revision} command={type(command).__name__} "
            f"state={record.state} checkpoint={record.checkpoint.phase} "
            f"attempt={record.attempt} dispatches={record.checkpoint.dispatches}"
        )

    commit(RunSubmission("demo", "run-1", "hello", "request-1", "demo:v1", "demo-hash"))
    authority = claim_execution(
        authority, owner_id="A", claim_id="A-1", now=now, ttl=10, run_id="run-1"
    )
    old_grant = authority.grant
    assert old_grant is not None
    commit(ClaimRun(old_grant))
    commit(CommitPrompt(DurableEventIdentity("demo", 1, "demo:1")))
    commit(DispatchModel())
    commit(CommitAssistant(DurableEventIdentity("demo", 2, "demo:2"), "stop"))

    # A dies after the complete assistant was saved, before Run completion.
    now = 10.0
    authority = claim_execution(
        authority, owner_id="B", claim_id="B-1", now=now, ttl=10, run_id="run-1"
    )
    assert authority.grant is not None
    commit(ClaimRun(authority.grant))
    commit(FinishRun("completed"))
    record = replay_run("demo", "run-1", events)
    assert record is not None
    assert record.checkpoint.dispatches == 1  # B did not call the model again.
    try:
        validate_execution(authority, old_grant, now=now)
    except SessionConflictError:
        print("old-owner-write=rejected")
    else:
        raise AssertionError("stale owner was accepted")
    authority = settle_execution(authority, authority.grant, record, now=now)
    print(f"active_run={authority.active_run_id} retained_epoch={authority.epoch}")


if __name__ == "__main__":
    main()
