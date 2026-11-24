"""Single-loop offline port substitute; all plans use the formal pure transitions."""

from dataclasses import replace

from pi_agent_py.domain import ControlCancellation, QueuedControl, SessionEventContext
from pi_agent_py.errors import (
    SessionConflictError,
    SessionStorageError,
    SessionValidationError,
    SessionCommitUnknownError,
)
from pi_agent_py.execution_domain import (
    ExecutionAuthority,
    ExecutionReceipt,
    ClaimRun,
    ExhaustRun,
    FinishRun,
    ExecutionStepRequest,
    FinishExecutionRequest,
    CommitAssistant,
    CommitToolResults,
    ContinueRun,
)
from pi_agent_py.storage.execution_codec import execution_operation
from pi_agent_py.storage.execution_mutations import plan_execution_mutations
from pi_agent_py.storage.execution_transitions import (
    claim_execution,
    renew_execution,
    release_execution,
    settle_execution,
    validate_execution,
    prepare_run_transition,
)
from pi_agent_py.storage.memory import InMemorySessionStore


class ExecutionMemory(InMemorySessionStore):
    supports_distributed_execution = (
        True  # Explicit testing substitute, never a production fallback.
    )

    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.journals, self.authorities, self.receipts = {}, {}, {}
        self.fail_after_assistant = False
        self.fail_after_tool = False
        self.fail_after_continue = False
        self.unknown_finish = False
        self.unknown_after_tool = False
        self.claims = 0
        self.maintenance_claims = 0

    def authority(self, sid):
        return self.authorities.get(sid, ExecutionAuthority(sid))

    async def list_candidates(self, limit, *, exclude=()):
        candidates = []
        for sid in dict.fromkeys(s for s, _ in self.journals):
            if sid in exclude:
                continue
            authority = self.authority(sid)
            if authority.grant is not None and authority.grant.expires_at > self.clock.unix_time():
                continue
            records = [await self.load_execution(s, r) for s, r in self.journals if s == sid]
            if any(r.state in ("queued", "running") for r in records):
                candidates.append(sid)
        return tuple(candidates[:limit])

    async def load_execution(self, sid, rid):
        from pi_agent_py.storage.execution_transitions import replay_run

        return replay_run(sid, rid, self.journals.get((sid, rid), ()))

    def advance(self, sid, rid, command, grant=None, operation_id="event"):
        journal = self.journals.get((sid, rid), ())
        plan = prepare_run_transition(
            sid,
            rid,
            journal,
            len(journal),
            command,
            operation_id=operation_id,
            queue_order=len(self.journals) + 1,
            authority=self.authority(sid) if grant else None,
            grant=grant,
            now=self.clock.unix_time(),
        )
        self.journals[sid, rid] = (*journal, plan.event)
        return plan.record

    async def submit_run(self, submission):
        old = await self.load_execution(submission.session_id, submission.run_id)
        if old:
            if old.submission != submission:
                raise SessionValidationError("changed submission")
            return old
        if submission.control_id is not None:
            snapshot = await self.load(submission.session_id)
            await super().record_control(
                submission.session_id,
                snapshot.version,
                "control_enqueued",
                QueuedControl(
                    submission.control_id,
                    "follow_up",
                    submission.prompt,
                    submission.run_id,
                ),
                event_context=SessionEventContext(run_id=submission.run_id),
            )
        return self.advance(submission.session_id, submission.run_id, submission)

    async def claim_next(self, lease, ttl, operation):
        sid = lease.session_id
        records = [
            await self.load_execution(s, r) for s, r in self.journals if s == sid
        ]
        records = sorted(
            (r for r in records if r.state in ("queued", "running")),
            key=lambda r: (r.state != "running", r.queue_order),
        )
        if not records:
            return None
        record = records[0]
        authority = claim_execution(
            self.authority(sid),
            owner_id=lease.owner_id,
            claim_id=lease.claim_id,
            now=self.clock.unix_time(),
            ttl=ttl,
            run_id=record.submission.run_id,
        )
        self.authorities[sid] = authority
        self.claims += 1
        command = (
            ClaimRun(authority.grant)
            if record.attempt < record.submission.max_attempts
            else ExhaustRun(authority.grant)
        )
        record = self.advance(
            sid,
            record.submission.run_id,
            command,
            authority.grant,
            operation.operation_id,
        )
        if record.state == "failed":
            self.authorities[sid] = ExecutionAuthority(sid, authority.epoch)
        receipt = ExecutionReceipt(
            await self.load(sid), record, self.authority(sid).grant
        )
        self.receipts[sid, operation.operation_id] = (operation, receipt)
        return receipt

    async def claim_run(self, lease, run_id, ttl, operation):
        records = [
            await self.load_execution(s, r)
            for s, r in self.journals
            if s == lease.session_id
        ]
        records = sorted(
            (r for r in records if r.state in ("queued", "running")),
            key=lambda r: (r.state != "running", r.queue_order),
        )
        if records and records[0].submission.run_id != run_id:
            raise SessionConflictError("earlier durable Run")
        return await self.claim_next(lease, ttl, operation)

    async def renew_grant(self, grant, ttl):
        updated = renew_execution(
            self.authority(grant.session_id), grant, now=self.clock.unix_time(), ttl=ttl
        )
        self.authorities[grant.session_id] = updated
        return updated.grant

    async def release_grant(self, grant):
        authority = self.authority(grant.session_id)
        if authority.grant is None:
            return False
        try:
            updated = release_execution(authority, grant)
        except SessionConflictError:
            return False
        self.authorities[grant.session_id] = updated
        return True

    def bind_writer(self, grant):
        outer = self

        class Writer:
            async def commit_step(self, operation, command, mutations, **kwargs):
                return await outer.commit_step(
                    grant, operation, command, mutations, **kwargs
                )

        return Writer()

    async def commit_step(
        self,
        grant,
        operation,
        command,
        mutations,
        *,
        expected_session_version,
        expected_revision,
    ):
        request = ExecutionStepRequest(
            grant, command, mutations, expected_session_version, expected_revision
        )
        if execution_operation(operation.operation_id, request) != operation:
            raise SessionValidationError("changed fingerprint")
        validate_execution(
            self.authority(grant.session_id), grant, now=self.clock.unix_time()
        )
        record = (
            await self.load_execution(grant.session_id, grant.run_id)
            if grant.run_id
            else None
        )
        if expected_revision != (record.revision if record else 0):
            raise SessionConflictError("changed revision")
        snapshot = await self.load(grant.session_id)
        if snapshot.version != expected_session_version:
            raise SessionConflictError("changed history")
        events, snapshot = plan_execution_mutations(
            await self.read_events(grant.session_id), grant, record, command, mutations
        )
        if command is not None:
            record = self.advance(
                grant.session_id, grant.run_id, command, grant, operation.operation_id
            )
        self._events_by_session[grant.session_id] = [
            *(await self.read_events(grant.session_id)),
            *events,
        ]
        receipt = ExecutionReceipt(
            snapshot, record, self.authority(grant.session_id).grant
        )
        self.receipts[grant.session_id, operation.operation_id] = (operation, receipt)
        if self.fail_after_assistant and isinstance(command, CommitAssistant):
            self.fail_after_assistant = False
            raise SessionStorageError("injected crash after durable assistant")
        if self.fail_after_tool and isinstance(command, CommitToolResults):
            self.fail_after_tool = False
            raise SessionStorageError("injected crash after first tool result")
        if self.unknown_after_tool and isinstance(command, CommitToolResults):
            self.unknown_after_tool = False
            raise SessionCommitUnknownError(grant.session_id, operation.operation_id)
        if self.fail_after_continue and isinstance(command, ContinueRun):
            self.fail_after_continue = False
            raise SessionStorageError("injected crash before next request")
        return receipt

    async def finish_run(
        self, grant, operation, finish, *, expected_session_version, expected_revision
    ):
        request = FinishExecutionRequest(
            grant, finish, expected_session_version, expected_revision
        )
        if execution_operation(operation.operation_id, request) != operation:
            raise SessionValidationError("changed fingerprint")
        snapshot = await self.load(grant.session_id)
        record = await self.load_execution(grant.session_id, grant.run_id)
        if (
            snapshot.version != expected_session_version
            or record.revision != expected_revision
        ):
            raise SessionConflictError("changed terminal version")
        if finish.state == "completed" and any(
            p.control.run_id == grant.run_id and p.control.delivery == "steer"
            for p in snapshot.pending_controls
        ):
            raise SessionConflictError("pending steering")
        record = self.advance(
            grant.session_id, grant.run_id, finish, grant, operation.operation_id
        )
        self.authorities[grant.session_id] = settle_execution(
            self.authority(grant.session_id), grant, record, now=self.clock.unix_time()
        )
        for pending in snapshot.pending_controls:
            if pending.control.run_id == grant.run_id:
                snapshot = await super().record_control(
                    grant.session_id,
                    snapshot.version,
                    "control_cancelled",
                    ControlCancellation(pending.control.control_id, "terminal"),
                    event_context=SessionEventContext(run_id=grant.run_id),
                )
        receipt = ExecutionReceipt(snapshot, record)
        self.receipts[grant.session_id, operation.operation_id] = (operation, receipt)
        if self.unknown_finish:
            self.unknown_finish = False
            raise SessionCommitUnknownError(grant.session_id, operation.operation_id)
        return receipt

    async def resolve_operation(self, sid, operation_id):
        cached = self.receipts.get((sid, operation_id))
        return cached[1] if cached else None

    async def claim_maintenance(self, lease, ttl, operation):
        updated = claim_execution(
            self.authority(lease.session_id),
            owner_id=lease.owner_id,
            claim_id=lease.claim_id,
            now=self.clock.unix_time(),
            ttl=ttl,
            purpose="maintenance",
        )
        self.authorities[lease.session_id] = updated
        self.maintenance_claims += 1
        return updated.grant

    async def cancel_queued(self, sid, rid, operation, reason, *, expected_revision):
        record = await self.load_execution(sid, rid)
        if record.revision != expected_revision or record.state != "queued":
            raise SessionConflictError("not queued")
        record = self.advance(
            sid,
            rid,
            FinishRun("cancelled", reason),
            operation_id=operation.operation_id,
        )
        return ExecutionReceipt(await self.load(sid), record)

    def expire(self, sid):
        current = self.authority(sid)
        if current.grant:
            self.authorities[sid] = replace(
                current, grant=replace(current.grant, expires_at=0)
            )
