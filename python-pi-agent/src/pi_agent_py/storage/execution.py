"""PostgreSQL execution journal, authoritative epochs and atomic bound writers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import cast

from ..ports import ExecutionWriter

from ..domain import (
    ControlCancellation,
    ControlClaim,
    EventProvenance,
    QueuedControl,
    SessionEventContext,
    SessionSnapshot,
)
from ..errors import SessionConflictError, SessionReplayError, SessionValidationError
from ..execution_domain import (
    CancelQueuedRequest,
    ClaimExecutionRequest,
    ClaimRun,
    ExecutionAuthority,
    ExecutionGrant,
    ExecutionMutation,
    ExecutionReceipt,
    ExecutionRequest,
    ExecutionStepRequest,
    ExhaustRun,
    FinishExecutionRequest,
    FinishRun,
    GrantPurpose,
    OperationIdentity,
    ProgressCommand,
    RunCommand,
    RunEvent,
    RunRecord,
    RunSubmission,
    SessionLease,
    _integer,
    _text,
)
from ._codec import _decode_event, _require_int, _require_object, _require_exact_keys
from ._postgres_driver import (
    PostgresConnection,
    PostgresPool,
    PostgresSettings,
    postgres_transaction,
)
from .execution_codec import (
    decode_grant,
    decode_record,
    decode_run_event,
    dump,
    encode_run_event,
    execution_operation,
    load_record,
    record_data,
)
from .execution_mutations import plan_execution_mutations
from .execution_transitions import (
    claim_execution,
    prepare_run_transition,
    release_execution,
    renew_execution,
    replay_run,
    settle_execution,
    validate_execution,
)
from .postgres import PostgresSessionStore
from .transitions import prepare_session_control, replay_session, validate_session_id


@dataclass(frozen=True, slots=True)
class _BoundExecutionWriter:
    store: "PostgresExecutionStore"
    grant: ExecutionGrant

    async def commit_step(
        self,
        operation: OperationIdentity,
        command: ProgressCommand | None,
        mutations: tuple[ExecutionMutation, ...],
        *,
        expected_session_version: int,
        expected_revision: int,
    ) -> ExecutionReceipt:
        return await self.store.commit_step(
            self.grant,
            operation,
            command,
            mutations,
            expected_session_version=expected_session_version,
            expected_revision=expected_revision,
        )


class PostgresExecutionStore:
    def __init__(
        self, pool: PostgresPool, *, settings: PostgresSettings = PostgresSettings()
    ) -> None:
        self._pool, self._settings = pool, settings

    async def ensure_schema(self) -> tuple[int, ...]:
        return await PostgresSessionStore(
            self._pool, settings=self._settings
        ).ensure_schema()

    @staticmethod
    def _verify(operation: OperationIdentity, request: ExecutionRequest) -> None:
        if execution_operation(operation.operation_id, request) != operation:
            raise SessionValidationError("operation digest does not match full request")

    @asynccontextmanager
    async def _locked(
        self,
        session_id: str,
        operation: OperationIdentity | None = None,
        *,
        create: bool = False,
    ) -> AsyncIterator[tuple[PostgresConnection, ExecutionAuthority, float, bool]]:
        validate_session_id(session_id)
        async with postgres_transaction(
            self._pool,
            self._settings,
            commit_identity=(session_id, operation.operation_id) if operation else None,
        ) as connection:
            if create:
                await connection.execute(
                    "INSERT INTO sessions(session_id,execution_mode) VALUES($1,'distributed') ON CONFLICT(session_id) DO NOTHING",
                    session_id,
                )
            row = await connection.fetchrow(
                """SELECT execution_mode,epoch,owner_id,claim_id,lease_purpose,active_run_id,
                extract(epoch FROM lease_expires_at)::double precision AS expires_at
                FROM sessions WHERE session_id=$1 FOR UPDATE""",
                session_id,
            )
            # Read DB time only AFTER the row lock was acquired.
            clock = await connection.fetchrow(
                "SELECT extract(epoch FROM clock_timestamp())::double precision AS now"
            )
            assert clock is not None
            now = cast(float, clock["now"])
            if row is None:
                yield connection, ExecutionAuthority(session_id), now, False
                return
            if row["execution_mode"] != "distributed":
                raise SessionConflictError(
                    "existing local session requires explicit migration"
                )
            active = cast(str | None, row["active_run_id"])
            grant = None
            if row["lease_purpose"] is not None:
                purpose = cast(GrantPurpose, row["lease_purpose"])
                grant = ExecutionGrant(
                    session_id,
                    cast(str, row["owner_id"]),
                    cast(str, row["claim_id"]),
                    cast(int, row["epoch"]),
                    cast(float, row["expires_at"]),
                    purpose,
                    active,
                    "compact" if purpose == "maintenance" else None,
                )
            yield (
                connection,
                ExecutionAuthority(session_id, cast(int, row["epoch"]), grant, active),
                now,
                True,
            )

    @staticmethod
    async def _session(
        connection: PostgresConnection, session_id: str
    ) -> SessionSnapshot:
        _, events = await PostgresSessionStore._state(connection, session_id)
        return replay_session(session_id, events)

    @staticmethod
    async def _run(
        connection: PostgresConnection,
        session_id: str,
        run_id: str,
        *,
        lock: bool = False,
    ) -> tuple[RunRecord | None, tuple[RunEvent, ...]]:
        row = await connection.fetchrow(
            "SELECT record::text AS record FROM session_runs WHERE session_id=$1 AND run_id=$2"
            + (" FOR UPDATE" if lock else ""),
            session_id,
            run_id,
        )
        if row is None:
            return None, ()
        rows = await connection.fetch(
            "SELECT payload::text AS payload FROM run_events WHERE session_id=$1 AND run_id=$2 ORDER BY revision",
            session_id,
            run_id,
        )
        events = tuple(decode_run_event(cast(str, r["payload"])) for r in rows)
        record = load_record(cast(str, row["record"]))
        if replay_run(session_id, run_id, events) != record:
            raise SessionReplayError("run projection disagrees with execution journal")
        return record, events

    @staticmethod
    async def _authority(
        connection: PostgresConnection, authority: ExecutionAuthority
    ) -> None:
        grant = authority.grant
        await connection.execute(
            """UPDATE sessions SET epoch=$2,owner_id=$3,claim_id=$4,
            lease_expires_at=to_timestamp($5::double precision),lease_purpose=$6,active_run_id=$7,
            updated_at=clock_timestamp() WHERE session_id=$1""",
            authority.session_id,
            authority.epoch,
            grant.owner_id if grant else None,
            grant.claim_id if grant else None,
            grant.expires_at if grant else None,
            grant.purpose if grant else None,
            authority.active_run_id,
        )

    @staticmethod
    async def _advance(
        connection: PostgresConnection,
        session_id: str,
        run_id: str,
        events: tuple[RunEvent, ...],
        command: RunCommand,
        operation_id: str,
        authority: ExecutionAuthority | None = None,
        *,
        now: float = 0,
        queue_order: int = 1,
    ) -> RunRecord:
        plan = prepare_run_transition(
            session_id,
            run_id,
            events,
            len(events),
            command,
            operation_id=operation_id,
            queue_order=queue_order,
            authority=authority,
            grant=authority.grant if authority else None,
            now=now,
        )
        if plan.event is None:
            raise SessionReplayError(
                "operation exists in journal without transaction receipt"
            )
        record = plan.record
        if events:
            row = await connection.fetchrow(
                "UPDATE session_runs SET record=$3::jsonb WHERE session_id=$1 AND run_id=$2 AND revision=$4 RETURNING revision",
                session_id,
                run_id,
                dump(record_data(record)),
                len(events),
            )
            if row is None:
                raise SessionConflictError("execution revision conflict")
        else:
            await connection.execute(
                "INSERT INTO session_runs(session_id,run_id,record) VALUES($1,$2,$3::jsonb)",
                session_id,
                run_id,
                dump(record_data(record)),
            )
        await connection.execute(
            "INSERT INTO run_events(session_id,run_id,revision,payload) VALUES($1,$2,$3,$4::jsonb)",
            session_id,
            run_id,
            record.revision,
            encode_run_event(plan.event),
        )
        return record

    @staticmethod
    async def _receipt(
        connection: PostgresConnection, raw: object, session_id: str
    ) -> ExecutionReceipt | None:
        if raw is None:
            return None
        try:
            data = _require_object(raw, "receipt")
            _require_exact_keys(
                data,
                {"schema_version", "session_id", "session_version", "run", "grant"},
                "receipt",
            )
            if (
                _require_int(data["schema_version"], "schema") != 1
                or data["session_id"] != session_id
            ):
                raise ValueError("invalid receipt schema")
            version = _require_int(data["session_version"], "session version")
            rows = await connection.fetch(
                "SELECT sequence,payload::text AS payload FROM session_events WHERE session_id=$1 AND sequence<=$2 ORDER BY sequence",
                data["session_id"],
                version,
            )
            snapshot = replay_session(
                session_id,
                tuple(
                    _decode_event(cast(str, row["payload"]), cast(int, row["sequence"]))
                    for row in rows
                ),
            )
            if snapshot.version != version:
                raise ValueError("receipt session prefix missing")
            receipt = ExecutionReceipt(
                snapshot,
                decode_record(data["run"]) if data["run"] is not None else None,
                decode_grant(data["grant"]) if data["grant"] is not None else None,
            )
            if (
                receipt.run is not None
                and receipt.run.submission.session_id != session_id
            ):
                raise ValueError("receipt run belongs to another session")
            if receipt.grant is not None and (
                receipt.grant.session_id != session_id
                or receipt.grant.run_id
                != (receipt.run.submission.run_id if receipt.run else None)
            ):
                raise ValueError("receipt grant does not match run")
            return receipt
        except (ValueError, TypeError, KeyError, SessionValidationError) as error:
            raise SessionReplayError("invalid committed execution receipt") from error

    async def _cached(
        self,
        connection: PostgresConnection,
        session_id: str,
        operation: OperationIdentity,
        kind: str,
    ) -> tuple[bool, ExecutionReceipt | None]:
        row = await connection.fetchrow(
            "SELECT request_digest,kind,receipt::text AS receipt FROM session_operations WHERE session_id=$1 AND operation_id=$2",
            session_id,
            operation.operation_id,
        )
        if row is None:
            return False, None
        if row["request_digest"] != operation.request_digest or row["kind"] != kind:
            raise SessionValidationError("operation id reused with different request")
        return True, await self._receipt(
            connection, json.loads(cast(str, row["receipt"])), session_id
        )

    @staticmethod
    async def _cache(
        connection: PostgresConnection,
        session_id: str,
        operation: OperationIdentity,
        kind: str,
        receipt: ExecutionReceipt | None,
    ) -> None:
        raw = (
            None
            if receipt is None
            else {
                "schema_version": 1,
                "session_id": receipt.session.session_id,
                "session_version": receipt.session.version,
                "run": record_data(receipt.run) if receipt.run else None,
                "grant": asdict(receipt.grant) if receipt.grant else None,
            }
        )
        await connection.execute(
            "INSERT INTO session_operations(session_id,operation_id,kind,request_digest,receipt) VALUES($1,$2,$3,$4,$5::jsonb)",
            session_id,
            operation.operation_id,
            kind,
            operation.request_digest,
            dump(raw),
        )

    async def submit_run(self, submission: RunSubmission) -> RunRecord:
        submission = deepcopy(submission)
        operation = execution_operation(
            "submission/" + sha256(submission.submission_key.encode()).hexdigest(),
            submission,
        )
        async with self._locked(submission.session_id, operation, create=True) as (
            connection,
            _,
            _,
            _,
        ):
            found, cached = await self._cached(
                connection, submission.session_id, operation, "submit"
            )
            if found:
                assert cached is not None and cached.run is not None
                return cached.run
            existing, _ = await self._run(
                connection, submission.session_id, submission.run_id, lock=True
            )
            if existing is not None:
                raise SessionValidationError(
                    "run id already belongs to another submission"
                )
            row = await connection.fetchrow(
                "UPDATE sessions SET next_run_order=next_run_order+1 WHERE session_id=$1 RETURNING next_run_order",
                submission.session_id,
            )
            assert row is not None
            record = await self._advance(
                connection,
                submission.session_id,
                submission.run_id,
                (),
                submission,
                operation.operation_id,
                queue_order=cast(int, row["next_run_order"]),
            )
            _, events = await PostgresSessionStore._state(
                connection, submission.session_id
            )
            snapshot = replay_session(submission.session_id, events)
            if submission.control_id is not None:
                if any(
                    p.control.control_id == submission.control_id
                    for p in snapshot.pending_controls
                ):
                    raise SessionValidationError("follow-up control id already pending")
                plan = prepare_session_control(
                    submission.session_id,
                    events,
                    snapshot.version,
                    "control_enqueued",
                    QueuedControl(
                        submission.control_id,
                        "follow_up",
                        submission.prompt,
                        submission.run_id,
                    ),
                    event_context=SessionEventContext(
                        run_id=submission.run_id,
                        provenance=EventProvenance("user", "execution-admission"),
                    ),
                )
                await PostgresSessionStore._save(
                    connection,
                    submission.session_id,
                    snapshot.version,
                    (plan.event,),
                    plan.snapshot,
                )
                snapshot = plan.snapshot
            await self._cache(
                connection,
                submission.session_id,
                operation,
                "submit",
                ExecutionReceipt(snapshot, record),
            )
            return record

    async def list_candidates(self, limit: int, *, exclude: tuple[str, ...] = ()) -> tuple[str, ...]:
        _integer(limit, "limit", 1)
        from ..execution_domain import _ids
        _ids(exclude, "excluded sessions")
        async with postgres_transaction(
            self._pool, self._settings, readonly=True
        ) as connection:
            rows = await connection.fetch(
                """SELECT session_id FROM sessions s WHERE execution_mode='distributed'
                AND (lease_expires_at IS NULL OR lease_expires_at<=clock_timestamp())
                AND (active_run_id IS NOT NULL OR EXISTS(SELECT 1 FROM session_runs r WHERE r.session_id=s.session_id AND r.state='queued'))
                AND NOT (s.session_id=ANY($2::text[]))
                ORDER BY updated_at,session_id LIMIT $1""",
                limit,
                list(exclude),
            )
            return tuple(cast(str, row["session_id"]) for row in rows)

    async def claim_next(
        self, lease: SessionLease, ttl: float, operation: OperationIdentity
    ) -> ExecutionReceipt | None:
        self._verify(operation, ClaimExecutionRequest(lease, ttl))
        return await self._claim(lease, ttl, operation)

    async def claim_run(
        self, lease: SessionLease, run_id: str, ttl: float, operation: OperationIdentity
    ) -> ExecutionReceipt | None:
        from ..execution_domain import ClaimRunExecutionRequest
        self._verify(operation, ClaimRunExecutionRequest(lease, ttl, run_id))
        return await self._claim(lease, ttl, operation, expected_run_id=run_id)

    async def _claim(
        self, lease: SessionLease, ttl: float, operation: OperationIdentity, *,
        expected_run_id: str | None = None,
    ) -> ExecutionReceipt | None:
        async with self._locked(lease.session_id, operation) as (
            connection,
            authority,
            now,
            exists,
        ):
            if not exists:
                return None
            found, cached = await self._cached(
                connection, lease.session_id, operation, "claim"
            )
            if found:
                return cached
            if lease.expires_at <= now:
                raise SessionConflictError("coordination lease expired")
            run_id = authority.active_run_id
            if run_id is None:
                row = await connection.fetchrow(
                    "SELECT run_id FROM session_runs WHERE session_id=$1 AND state='queued' ORDER BY queue_order LIMIT 1 FOR UPDATE",
                    lease.session_id,
                )
                run_id = cast(str, row["run_id"]) if row else None
            if run_id is None:
                await self._cache(
                    connection, lease.session_id, operation, "claim", None
                )
                return None
            if expected_run_id is not None and expected_run_id != run_id:
                raise SessionConflictError("an earlier durable Run must be resumed first")
            record, journal = await self._run(
                connection, lease.session_id, run_id, lock=True
            )
            if record is None:
                raise SessionReplayError("active run is missing")
            authority = claim_execution(
                authority,
                owner_id=lease.owner_id,
                claim_id=lease.claim_id,
                now=now,
                ttl=min(ttl, lease.expires_at - now),
                run_id=run_id,
            )
            assert authority.grant is not None
            grant = authority.grant
            command = (
                ExhaustRun(grant)
                if record.attempt >= record.submission.max_attempts
                else ClaimRun(grant)
            )
            record = await self._advance(
                connection,
                lease.session_id,
                run_id,
                journal,
                command,
                operation.operation_id,
                authority,
                now=now,
            )
            snapshot = await self._session(connection, lease.session_id)
            if isinstance(command, ExhaustRun):
                snapshot = await self._cancel_controls(
                    connection, record, snapshot, "attempt_budget_exhausted"
                )
                authority = settle_execution(authority, grant, record, now=now)
            elif (
                record.submission.control_id is not None
                and record.checkpoint.phase == "before_prompt"
            ):
                _, events = await PostgresSessionStore._state(
                    connection, lease.session_id
                )
                plan = prepare_session_control(
                    lease.session_id,
                    events,
                    snapshot.version,
                    "control_claimed",
                    ControlClaim(
                        record.submission.control_id,
                        grant.claim_id,
                        grant.owner_id,
                        grant.expires_at,
                    ),
                    event_context=SessionEventContext(
                        run_id=run_id,
                        provenance=EventProvenance("runtime", "execution-claim"),
                    ),
                )
                await PostgresSessionStore._save(
                    connection,
                    lease.session_id,
                    snapshot.version,
                    (plan.event,),
                    plan.snapshot,
                )
                snapshot = plan.snapshot
            await self._authority(connection, authority)
            receipt = ExecutionReceipt(snapshot, record, authority.grant)
            await self._cache(connection, lease.session_id, operation, "claim", receipt)
            return receipt

    async def claim_maintenance(
        self, lease: SessionLease, ttl: float, operation: OperationIdentity
    ) -> ExecutionGrant:
        self._verify(operation, ClaimExecutionRequest(lease, ttl, "maintenance"))
        async with self._locked(lease.session_id, operation) as (
            connection,
            authority,
            now,
            exists,
        ):
            if not exists:
                raise SessionValidationError("unknown session")
            found, cached = await self._cached(
                connection, lease.session_id, operation, "maintenance"
            )
            if found:
                assert cached is not None and cached.grant is not None
                return cached.grant
            if lease.expires_at <= now:
                raise SessionConflictError("coordination lease expired")
            authority = claim_execution(
                authority,
                owner_id=lease.owner_id,
                claim_id=lease.claim_id,
                now=now,
                ttl=min(ttl, lease.expires_at - now),
                purpose="maintenance",
            )
            assert authority.grant is not None
            await self._authority(connection, authority)
            await self._cache(
                connection,
                lease.session_id,
                operation,
                "maintenance",
                ExecutionReceipt(
                    await self._session(connection, lease.session_id),
                    grant=authority.grant,
                ),
            )
            return authority.grant

    async def renew_grant(self, grant: ExecutionGrant, ttl: float) -> ExecutionGrant:
        if not isinstance(grant, ExecutionGrant):
            raise SessionConflictError("execution grant required")
        async with self._locked(grant.session_id) as (connection, authority, now, _):
            authority = renew_execution(authority, grant, now=now, ttl=ttl)
            await self._authority(connection, authority)
            assert authority.grant is not None
            return authority.grant

    async def release_grant(self, grant: ExecutionGrant) -> bool:
        if not isinstance(grant, ExecutionGrant):
            raise SessionConflictError("execution grant required")
        async with self._locked(grant.session_id) as (connection, authority, _, _):
            if authority.grant is None:
                return False
            try:
                updated = release_execution(authority, grant)
            except SessionConflictError:
                return False
            await self._authority(connection, updated)
            return True

    async def load_execution(self, session_id: str, run_id: str) -> RunRecord | None:
        validate_session_id(session_id)
        _text(run_id, "run_id")
        async with postgres_transaction(
            self._pool, self._settings, isolation="repeatable_read", readonly=True
        ) as connection:
            record, _ = await self._run(connection, session_id, run_id)
            return record

    def bind_writer(self, grant: ExecutionGrant) -> ExecutionWriter:
        if not isinstance(grant, ExecutionGrant):
            raise SessionConflictError("execution grant required")
        return _BoundExecutionWriter(self, grant)

    async def commit_step(
        self,
        grant: ExecutionGrant,
        operation: OperationIdentity,
        command: ProgressCommand | None,
        mutations: tuple[ExecutionMutation, ...],
        *,
        expected_session_version: int,
        expected_revision: int,
    ) -> ExecutionReceipt:
        if not isinstance(grant, ExecutionGrant):
            raise SessionConflictError("execution grant required")
        request = deepcopy(
            ExecutionStepRequest(
                grant, command, mutations, expected_session_version, expected_revision
            )
        )
        self._verify(operation, request)
        command, mutations = request.command, request.mutations
        async with self._locked(grant.session_id, operation) as (
            connection,
            authority,
            now,
            _,
        ):
            found, cached = await self._cached(
                connection, grant.session_id, operation, "step"
            )
            if found:
                assert cached is not None
                return cached
            validate_execution(authority, grant, now=now)
            record, journal = (
                await self._run(connection, grant.session_id, grant.run_id, lock=True)
                if grant.run_id
                else (None, ())
            )
            if record is not None and record.epoch != grant.epoch:
                raise SessionConflictError(
                    "run does not belong to this execution epoch"
                )
            if expected_revision != (record.revision if record else 0):
                raise SessionConflictError("execution revision conflict")
            _, events = await PostgresSessionStore._state(connection, grant.session_id)
            before = replay_session(grant.session_id, events)
            if before.version != expected_session_version:
                raise SessionConflictError("session version conflict")
            new, snapshot = plan_execution_mutations(
                events, grant, record, command, mutations
            )
            if command is not None:
                assert grant.run_id is not None
                record = await self._advance(
                    connection,
                    grant.session_id,
                    grant.run_id,
                    journal,
                    command,
                    operation.operation_id,
                    authority,
                    now=now,
                )
            if new:
                await PostgresSessionStore._save(
                    connection, grant.session_id, before.version, new, snapshot
                )
            receipt = ExecutionReceipt(snapshot, record, authority.grant)
            await self._cache(connection, grant.session_id, operation, "step", receipt)
            return receipt

    @staticmethod
    async def _cancel_controls(
        connection: PostgresConnection,
        record: RunRecord,
        snapshot: SessionSnapshot,
        reason: str,
    ) -> SessionSnapshot:
        for pending in snapshot.pending_controls:
            if pending.control.run_id == record.submission.run_id:
                _, events = await PostgresSessionStore._state(
                    connection, snapshot.session_id
                )
                plan = prepare_session_control(
                    snapshot.session_id,
                    events,
                    snapshot.version,
                    "control_cancelled",
                    ControlCancellation(pending.control.control_id, reason),
                    event_context=SessionEventContext(
                        run_id=record.submission.run_id,
                        provenance=EventProvenance("runtime", "execution-terminal"),
                    ),
                )
                await PostgresSessionStore._save(
                    connection,
                    snapshot.session_id,
                    snapshot.version,
                    (plan.event,),
                    plan.snapshot,
                )
                snapshot = plan.snapshot
        return snapshot

    async def finish_run(
        self,
        grant: ExecutionGrant,
        operation: OperationIdentity,
        finish: FinishRun,
        *,
        expected_session_version: int,
        expected_revision: int,
    ) -> ExecutionReceipt:
        if not isinstance(grant, ExecutionGrant):
            raise SessionConflictError("execution grant required")
        self._verify(
            operation,
            FinishExecutionRequest(
                grant, finish, expected_session_version, expected_revision
            ),
        )
        async with self._locked(grant.session_id, operation) as (
            connection,
            authority,
            now,
            _,
        ):
            found, cached = await self._cached(
                connection, grant.session_id, operation, "finish"
            )
            if found:
                assert cached is not None
                return cached
            validate_execution(authority, grant, now=now)
            if grant.run_id is None:
                raise SessionConflictError("maintenance cannot finish a run")
            record, journal = await self._run(
                connection, grant.session_id, grant.run_id, lock=True
            )
            snapshot = await self._session(connection, grant.session_id)
            if (
                record is None
                or record.revision != expected_revision
                or snapshot.version != expected_session_version
            ):
                raise SessionConflictError("terminal boundary version conflict")
            if finish.state == "completed" and any(
                p.control.run_id == grant.run_id for p in snapshot.pending_controls
            ):
                raise SessionValidationError(
                    "accepted controls must be settled before completion"
                )
            record = await self._advance(
                connection,
                grant.session_id,
                grant.run_id,
                journal,
                finish,
                operation.operation_id,
                authority,
                now=now,
            )
            if finish.state != "completed":
                snapshot = await self._cancel_controls(
                    connection, record, snapshot, finish.error_code or finish.state
                )
            authority = settle_execution(authority, grant, record, now=now)
            await self._authority(connection, authority)
            receipt = ExecutionReceipt(snapshot, record)
            await self._cache(
                connection, grant.session_id, operation, "finish", receipt
            )
            return receipt

    async def cancel_queued(
        self,
        session_id: str,
        run_id: str,
        operation: OperationIdentity,
        reason: str,
        *,
        expected_revision: int,
    ) -> ExecutionReceipt:
        request = CancelQueuedRequest(session_id, run_id, reason, expected_revision)
        self._verify(operation, request)
        async with self._locked(session_id, operation) as (connection, _, _, _):
            found, cached = await self._cached(
                connection, session_id, operation, "cancel"
            )
            if found:
                assert cached is not None
                return cached
            record, journal = await self._run(connection, session_id, run_id, lock=True)
            if (
                record is None
                or record.state != "queued"
                or record.revision != expected_revision
            ):
                raise SessionConflictError("queued cancellation raced with execution")
            record = await self._advance(
                connection,
                session_id,
                run_id,
                journal,
                FinishRun("cancelled", reason),
                operation.operation_id,
            )
            snapshot = await self._cancel_controls(
                connection, record, await self._session(connection, session_id), reason
            )
            receipt = ExecutionReceipt(snapshot, record)
            await self._cache(connection, session_id, operation, "cancel", receipt)
            return receipt

    async def resolve_operation(
        self, session_id: str, operation_id: str
    ) -> ExecutionReceipt | None:
        validate_session_id(session_id)
        _text(operation_id, "operation_id")
        async with postgres_transaction(
            self._pool, self._settings, isolation="repeatable_read", readonly=True
        ) as connection:
            row = await connection.fetchrow(
                "SELECT receipt::text AS receipt FROM session_operations WHERE session_id=$1 AND operation_id=$2",
                session_id,
                operation_id,
            )
            return (
                await self._receipt(
                    connection, json.loads(cast(str, row["receipt"])), session_id
                )
                if row
                else None
            )
