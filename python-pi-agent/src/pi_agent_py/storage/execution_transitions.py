"""Pure run journal and execution-authority transitions. No clocks or I/O."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, replace
from hashlib import sha256
import json

from ..domain import DurableEventIdentity
from ..errors import SessionConflictError, SessionReplayError, SessionValidationError
from ..execution_domain import (
    ClaimRun,
    CommitAssistant,
    CommitPrompt,
    CommitToolResults,
    RepairToolTail,
    ContinueRun,
    DispatchModel,
    ExecutionAuthority,
    ExecutionGrant,
    ExhaustRun,
    FinishRun,
    GrantPurpose,
    OperationIdentity,
    RunCommand,
    RunEvent,
    RunRecord,
    RunSubmission,
    RunTransitionPlan,
    _text,
    _time,
)


def operation_identity(operation_id: str, command: RunCommand) -> OperationIdentity:
    """Digest includes the command type; raw inputs must not enter public traces."""
    encoded = json.dumps(
        {"kind": type(command).__name__, "payload": asdict(command)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return OperationIdentity(operation_id, sha256(encoded).hexdigest())


def claim_execution(
    authority: ExecutionAuthority,
    *,
    owner_id: str,
    claim_id: str,
    now: float,
    ttl: float,
    purpose: GrantPurpose = "run",
    run_id: str | None = None,
) -> ExecutionAuthority:
    """Adapter must lock the Session and verify Redis coordination separately."""
    _time(now, "now")
    _time(ttl, "ttl")
    if ttl <= 0:
        raise SessionValidationError("ttl must be positive")
    if authority.grant is not None and authority.grant.expires_at > now:
        raise SessionConflictError("execution is still leased")
    if authority.active_run_id is not None and authority.active_run_id != run_id:
        raise SessionConflictError("interrupted active run must be recovered first")
    grant = ExecutionGrant(
        authority.session_id,
        owner_id,
        claim_id,
        authority.epoch + 1,
        now + ttl,
        purpose,
        run_id,
        "compact" if purpose == "maintenance" else None,
    )
    return ExecutionAuthority(authority.session_id, grant.epoch, grant, run_id)


def _validate_identity(authority: ExecutionAuthority, grant: ExecutionGrant) -> None:
    current = authority.grant
    if current is None or (
        current.session_id,
        current.owner_id,
        current.claim_id,
        current.epoch,
        current.purpose,
        current.run_id,
        current.maintenance,
    ) != (
        grant.session_id,
        grant.owner_id,
        grant.claim_id,
        grant.epoch,
        grant.purpose,
        grant.run_id,
        grant.maintenance,
    ):
        raise SessionConflictError("stale execution grant")


def validate_execution(
    authority: ExecutionAuthority,
    grant: ExecutionGrant,
    *,
    now: float,
) -> None:
    _time(now, "now")
    _validate_identity(authority, grant)
    current = authority.grant
    assert current is not None
    if current.expires_at <= now:
        raise SessionConflictError("execution lease expired")


def renew_execution(
    authority: ExecutionAuthority,
    grant: ExecutionGrant,
    *,
    now: float,
    ttl: float,
) -> ExecutionAuthority:
    validate_execution(authority, grant, now=now)
    _time(ttl, "ttl")
    if ttl <= 0:
        raise SessionValidationError("ttl must be positive")
    assert authority.grant is not None
    renewed = replace(
        authority.grant, expires_at=max(authority.grant.expires_at, now + ttl)
    )
    return replace(authority, grant=renewed)


def release_execution(
    authority: ExecutionAuthority, grant: ExecutionGrant
) -> ExecutionAuthority:
    """Compare-release, even after expiry; preserve interrupted run and epoch."""
    if authority.grant is None:
        return authority
    # Compare identity without requiring the caller's expiry to match a heartbeat.
    _validate_identity(authority, grant)
    return replace(authority, grant=None)


def settle_execution(
    authority: ExecutionAuthority,
    grant: ExecutionGrant,
    record: RunRecord,
    *,
    now: float,
) -> ExecutionAuthority:
    validate_execution(authority, grant, now=now)
    if grant.purpose != "run" or (
        record.submission.session_id != grant.session_id
        or record.submission.run_id != grant.run_id
        or record.epoch != grant.epoch
        or record.state not in ("completed", "failed", "cancelled")
    ):
        raise SessionValidationError("settlement requires the matching terminal run")
    return replace(authority, grant=None, active_run_id=None)


def _reference(record: RunRecord, event: DurableEventIdentity) -> None:
    if event.session_id != record.submission.session_id:
        raise SessionValidationError("checkpoint reference belongs to another session")


def _apply(
    record: RunRecord | None, command: RunCommand, queue_order: int
) -> RunRecord:
    if isinstance(command, RunSubmission):
        if record is not None:
            raise SessionValidationError("run already accepted")
        return RunRecord(command, 1, queue_order)
    if record is None:
        raise SessionValidationError("run has not been accepted")
    if record.state in ("completed", "failed", "cancelled"):
        raise SessionValidationError("terminal run cannot transition")
    cp = record.checkpoint
    if isinstance(command, (ClaimRun, ExhaustRun)):
        grant = command.grant
        if (
            grant.purpose != "run"
            or grant.session_id != record.submission.session_id
            or grant.run_id != record.submission.run_id
        ):
            raise SessionValidationError("claim does not identify this run")
        if grant.epoch <= record.epoch:
            raise SessionConflictError("claim epoch must increase")
        if isinstance(command, ExhaustRun):
            if record.attempt < record.submission.max_attempts:
                raise SessionValidationError("attempt budget is not exhausted")
            return replace(
                record,
                revision=record.revision + 1,
                state="failed",
                epoch=grant.epoch,
                checkpoint=replace(
                    cp, phase="terminal", unresolved_tool_ids=(), dispatched=False
                ),
                error_code="attempt_budget_exhausted",
            )
        if record.attempt >= record.submission.max_attempts:
            raise SessionValidationError("recovery attempt budget exhausted")
        # A reservation from a dead attempt cannot fund another physical request.
        cp = replace(cp, dispatched=False) if cp.phase == "awaiting_model" else cp
        return replace(
            record,
            revision=record.revision + 1,
            state="running",
            checkpoint=cp,
            attempt=record.attempt + 1,
            epoch=grant.epoch,
        )
    if isinstance(command, FinishRun):
        if record.state == "queued" and command.state != "cancelled":
            raise SessionValidationError("queued run can only be cancelled")
        if command.state == "completed" and (
            cp.phase != "awaiting_boundary" or cp.requires_continuation
        ):
            raise SessionValidationError("completion requires a final safe boundary")
        return replace(
            record,
            revision=record.revision + 1,
            state=command.state,
            checkpoint=replace(
                cp, phase="terminal", unresolved_tool_ids=(), dispatched=False
            ),
            error_code=command.error_code,
        )
    if record.state != "running":
        raise SessionValidationError("progress requires a running run")
    if isinstance(command, RepairToolTail):
        if cp.phase != "before_prompt":
            raise SessionValidationError("tail repair requires before_prompt")
    elif isinstance(command, CommitPrompt):
        if cp.phase != "before_prompt":
            raise SessionValidationError("prompt is already committed")
        _reference(record, command.event)
        cp = replace(cp, phase="awaiting_model", prompt_event=command.event)
    elif isinstance(command, DispatchModel):
        if cp.phase != "awaiting_model":
            raise SessionValidationError("dispatch requires awaiting_model")
        if cp.dispatches >= record.submission.max_dispatches:
            raise SessionValidationError("model dispatch budget exhausted")
        cp = replace(cp, dispatches=cp.dispatches + 1, dispatched=True)
    elif isinstance(command, CommitAssistant):
        if cp.phase != "awaiting_model" or not cp.dispatched:
            raise SessionValidationError("assistant requires reserved dispatch")
        _reference(record, command.event)
        assert cp.prompt_event is not None
        previous = cp.assistant_event or cp.prompt_event
        if command.event.sequence <= previous.sequence:
            raise SessionValidationError("assistant must follow previous messages")
        if command.finish_reason == "tool_calls" and not command.tool_call_ids:
            raise SessionValidationError("tool_calls finish requires tool calls")
        cp = replace(
            cp,
            phase="awaiting_tools" if command.tool_call_ids else "awaiting_boundary",
            assistant_event=command.event,
            finish_reason=command.finish_reason,
            unresolved_tool_ids=command.tool_call_ids,
            dispatched=False,
            requires_continuation=bool(command.tool_call_ids),
        )
    elif isinstance(command, CommitToolResults):
        if cp.phase != "awaiting_tools" or not set(command.tool_call_ids) <= set(
            cp.unresolved_tool_ids
        ):
            raise SessionValidationError("tool result has no unresolved call")
        remaining = tuple(
            value
            for value in cp.unresolved_tool_ids
            if value not in command.tool_call_ids
        )
        cp = replace(
            cp,
            phase="awaiting_tools" if remaining else "awaiting_boundary",
            unresolved_tool_ids=remaining,
        )
    elif isinstance(command, ContinueRun):
        if cp.phase != "awaiting_boundary" or not (
            cp.requires_continuation or command.steering_events
        ):
            raise SessionValidationError(
                "continuation requires tools or steering at boundary"
            )
        for event in command.steering_events:
            _reference(record, event)
            assert cp.assistant_event is not None
            if event.sequence <= cp.assistant_event.sequence:
                raise SessionValidationError("steering must follow assistant")
        if cp.request_number >= record.submission.max_requests:
            raise SessionValidationError("logical request budget exhausted")
        cp = replace(
            cp,
            phase="awaiting_model",
            request_number=cp.request_number + 1,
            requires_continuation=False,
        )
    else:
        raise SessionValidationError("unsupported run command")
    return replace(record, revision=record.revision + 1, checkpoint=cp)


def replay_run(
    session_id: str, run_id: str, events: Sequence[RunEvent]
) -> RunRecord | None:
    """Reconstruct durable progress, not live lease validity or tool outcomes."""
    _text(session_id, "session_id")
    _text(run_id, "run_id")
    record: RunRecord | None = None
    seen: set[str] = set()
    for revision, event in enumerate(events, 1):
        try:
            if (
                event.session_id != session_id
                or event.run_id != run_id
                or event.revision != revision
            ):
                raise SessionValidationError("run identity or revision gap")
            if (
                event.operation.operation_id in seen
                or operation_identity(event.operation.operation_id, event.command)
                != event.operation
            ):
                raise SessionValidationError("duplicate or corrupt operation")
            if record is not None and record.queue_order != event.queue_order:
                raise SessionValidationError("queue_order cannot change")
            record = _apply(record, event.command, event.queue_order)
            if (
                record.submission.session_id != session_id
                or record.submission.run_id != run_id
            ):
                raise SessionValidationError("submission identity mismatch")
            seen.add(event.operation.operation_id)
        except (SessionValidationError, SessionConflictError) as error:
            raise SessionReplayError(
                f"invalid run revision {revision}: {error}"
            ) from error
    return record


def prepare_run_transition(
    session_id: str,
    run_id: str,
    events: Sequence[RunEvent],
    expected_revision: int,
    command: RunCommand,
    *,
    operation_id: str,
    queue_order: int = 1,
    authority: ExecutionAuthority | None = None,
    grant: ExecutionGrant | None = None,
    now: float = 0,
) -> RunTransitionPlan:
    """Calculate a persistable fact; adapters atomically save it with session writes.

    Duplicate operations return the original receipt, even after later progress.
    Submission-key uniqueness and cross-run FIFO are Session aggregate duties.
    """
    record = replay_run(session_id, run_id, events)
    operation = operation_identity(operation_id, command)
    for index, event in enumerate(events):
        if event.operation.operation_id == operation_id:
            if event.operation != operation:
                raise SessionValidationError("operation id reused with different input")
            receipt = replay_run(session_id, run_id, events[: index + 1])
            assert receipt is not None
            return RunTransitionPlan(None, receipt)
    if type(expected_revision) is not int or expected_revision < 0:
        raise SessionValidationError("expected_revision must be non-negative integer")
    if (record.revision if record else 0) != expected_revision:
        raise SessionConflictError("execution revision conflict")
    if not isinstance(command, RunSubmission) and not (
        isinstance(command, FinishRun)
        and record is not None
        and record.state == "queued"
    ):
        if authority is None or grant is None:
            raise SessionConflictError("execution grant required")
        validate_execution(authority, grant, now=now)
        if (
            authority.session_id != session_id
            or grant.purpose != "run"
            or grant.run_id != run_id
        ):
            raise SessionConflictError("grant does not authorize this run")
        if isinstance(command, (ClaimRun, ExhaustRun)):
            if command.grant != grant:
                raise SessionConflictError("claim payload does not match grant")
        elif record is not None and record.epoch != grant.epoch:
            raise SessionConflictError("run must record the current claim first")
    order = record.queue_order if record else queue_order
    updated = _apply(record, command, order)
    if (
        updated.submission.session_id != session_id
        or updated.submission.run_id != run_id
    ):
        raise SessionValidationError("submission identity mismatch")
    event = RunEvent(session_id, run_id, updated.revision, order, operation, command)
    return RunTransitionPlan(event, updated)
