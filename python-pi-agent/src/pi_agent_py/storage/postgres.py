"""Transactional six-method SessionStore using an injected asyncpg-compatible pool."""

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
import json

from ..domain import (
    ControlCancellation,
    ControlClaim,
    ControlDelivery,
    ControlTransition,
    DurableSessionEvent,
    Message,
    QueuedControl,
    SessionCompaction,
    SessionEventContext,
    SessionEventKind,
    SessionSnapshot,
)
from ..errors import SessionConflictError, SessionReplayError, SessionValidationError
from ._codec import _decode_event, _encode_event
from ._postgres_driver import (
    PostgresConnection,
    PostgresPool,
    PostgresSettings,
    postgres_transaction,
)
from .postgres_migrations import apply_migrations
from .transitions import (
    fork_session_events,
    prepare_session_append,
    prepare_session_compaction,
    prepare_session_control,
    replay_session,
    validate_session_id,
)


class PostgresSessionStore:
    """Borrowed pool, no constructor I/O, explicit migrations, no automatic retry.

    Unfenced writes authorize local mode only. Distributed rows remain readable;
    execution mutations use the separate SessionExecutionStore writer.
    """

    supports_distributed_execution = True

    def __init__(
        self, pool: PostgresPool, *, settings: PostgresSettings = PostgresSettings()
    ) -> None:
        self._pool = pool
        self._settings = settings

    async def ensure_schema(self) -> tuple[int, ...]:
        """Explicit application/development setup; never implicitly called by I/O."""
        return await apply_migrations(self._pool, settings=self._settings)

    async def load(self, session_id: str) -> SessionSnapshot:
        return replay_session(session_id, await self.read_events(session_id))

    async def read_events(self, session_id: str) -> tuple[DurableSessionEvent, ...]:
        validate_session_id(session_id)
        async with postgres_transaction(
            self._pool, self._settings, isolation="repeatable_read", readonly=True
        ) as connection:
            _, events = await self._state(connection, session_id)
            return events

    @staticmethod
    async def _state(
        connection: PostgresConnection, session_id: str, *, lock: bool = False
    ) -> tuple[Mapping[str, object] | None, tuple[DurableSessionEvent, ...]]:
        row = await connection.fetchrow(
            "SELECT version, last_compaction_seq, execution_mode FROM sessions WHERE session_id=$1"
            + (" FOR UPDATE" if lock else ""),
            session_id,
        )
        if row is None:
            return None, ()
        rows = await connection.fetch(
            "SELECT sequence, payload::text AS payload FROM session_events WHERE session_id=$1 ORDER BY sequence",
            session_id,
        )
        events = []
        for stored in rows:
            sequence, payload = stored["sequence"], stored["payload"]
            if type(sequence) is not int or not isinstance(payload, str):
                raise SessionReplayError("invalid PostgreSQL event record")
            event = _decode_event(payload, sequence)
            if event.sequence != sequence:
                raise SessionReplayError(
                    "PostgreSQL event sequence disagrees with envelope"
                )
            events.append(event)
        snapshot = replay_session(session_id, events)
        compaction = max(
            (event.sequence for event in events if event.kind == "context_compacted"),
            default=0,
        )
        if (
            row["version"] != snapshot.version
            or row["last_compaction_seq"] != compaction
        ):
            raise SessionReplayError(
                "PostgreSQL session metadata disagrees with durable events"
            )
        return row, tuple(events)

    @asynccontextmanager
    async def _locked(
        self, session_id: str, expected_version: int
    ) -> AsyncIterator[tuple[PostgresConnection, tuple[DurableSessionEvent, ...]]]:
        validate_session_id(session_id)
        async with postgres_transaction(self._pool, self._settings) as connection:
            await connection.execute(
                "INSERT INTO sessions(session_id) VALUES($1) ON CONFLICT(session_id) DO NOTHING",
                session_id,
            )
            row, events = await self._state(connection, session_id, lock=True)
            assert row is not None
            if row["execution_mode"] != "local":
                raise SessionConflictError(
                    "distributed session requires execution-authorized writer"
                )
            # prepare_* retains validation/version precedence of existing stores.
            yield connection, events

    async def append(
        self,
        session_id: str,
        expected_version: int,
        messages: Sequence[Message],
        *,
        event_context: SessionEventContext | None = None,
    ) -> SessionSnapshot:
        async with self._locked(session_id, expected_version) as (connection, events):
            plan = prepare_session_append(
                session_id,
                events,
                expected_version,
                messages,
                event_context=event_context,
            )
            await self._save(
                connection, session_id, expected_version, plan.new_events, plan.snapshot
            )
            return plan.snapshot

    async def compact(
        self,
        session_id: str,
        expected_version: int,
        compaction: SessionCompaction,
        *,
        event_context: SessionEventContext | None = None,
    ) -> SessionSnapshot:
        async with self._locked(session_id, expected_version) as (connection, events):
            plan = prepare_session_compaction(
                session_id,
                events,
                expected_version,
                compaction,
                event_context=event_context,
            )
            await self._save(
                connection, session_id, expected_version, (plan.event,), plan.snapshot
            )
            return plan.snapshot

    async def record_control(
        self,
        session_id: str,
        expected_version: int,
        kind: SessionEventKind,
        control: ControlTransition,
        *,
        event_context: SessionEventContext | None = None,
    ) -> SessionSnapshot:
        async with self._locked(session_id, expected_version) as (connection, events):
            plan = prepare_session_control(
                session_id,
                events,
                expected_version,
                kind,
                control,
                event_context=event_context,
            )
            await self._save(
                connection, session_id, expected_version, (plan.event,), plan.snapshot
            )
            return plan.snapshot

    async def fork(
        self,
        source_session_id: str,
        target_session_id: str,
        at_version: int | None = None,
    ) -> SessionSnapshot:
        validate_session_id(source_session_id)
        validate_session_id(target_session_id)
        if source_session_id == target_session_id:
            raise SessionValidationError(
                "target_session_id must differ from the source session id"
            )
        async with postgres_transaction(
            self._pool, self._settings, isolation="repeatable_read"
        ) as connection:
            source, events = await self._state(connection, source_session_id)
            if source is not None and source["execution_mode"] != "local":
                raise SessionConflictError(
                    "distributed fork requires an explicit branch policy"
                )
            copied = fork_session_events(events, target_session_id, at_version)
            target = await connection.fetchrow(
                "SELECT session_id FROM sessions WHERE session_id=$1", target_session_id
            )
            if target is not None:
                raise SessionConflictError("target session already exists")
            if not copied:
                raise SessionValidationError(
                    "a persisted fork must contain at least one event"
                )
            await connection.execute(
                "INSERT INTO sessions(session_id,parent_session_id,forked_at_version) VALUES($1,$2,$3)",
                target_session_id,
                source_session_id,
                len(copied),
            )
            snapshot = replay_session(target_session_id, copied)
            await self._save(connection, target_session_id, 0, copied, snapshot)
            return snapshot

    @staticmethod
    async def _save(
        connection: PostgresConnection,
        session_id: str,
        expected_version: int,
        events: Sequence[DurableSessionEvent],
        snapshot: SessionSnapshot,
    ) -> None:
        encoded: list[tuple[object, ...]] = []
        for event in events:
            line = _encode_event(event)
            try:
                # JSONL's existing behavior is unchanged; JSONB requires finite JSON numbers.
                json.dumps(json.loads(line), allow_nan=False)
            except ValueError as error:
                raise SessionValidationError(
                    "PostgreSQL JSONB requires finite JSON numbers"
                ) from error
            encoded.append((session_id, event.sequence, line))
        await connection.executemany(
            "INSERT INTO session_events(session_id,sequence,payload) VALUES($1,$2,$3::jsonb)",
            encoded,
        )
        for event in events:
            await PostgresSessionStore._project(connection, event)
        compacted = max(
            (e.sequence for e in events if e.kind == "context_compacted"), default=0
        )
        row = await connection.fetchrow(
            "UPDATE sessions SET version=$2, last_compaction_seq=GREATEST(last_compaction_seq,$3), updated_at=CURRENT_TIMESTAMP WHERE session_id=$1 AND version=$4 RETURNING version",
            session_id,
            snapshot.version,
            compacted,
            expected_version,
        )
        if row is None:
            raise SessionConflictError("PostgreSQL session version conflict")

    @staticmethod
    async def _project(
        connection: PostgresConnection, event: DurableSessionEvent
    ) -> None:
        control = event.control
        if control is None:
            return
        if isinstance(control, QueuedControl):
            await connection.execute(
                """INSERT INTO session_controls
                (session_id,control_id,delivery,run_id,target_run_id,cause_run_id,prompt,state,last_sequence)
                VALUES($1,$2,$3,$4,$5,$6,$7,'queued',$8)
                ON CONFLICT(session_id,control_id) DO UPDATE SET
                delivery=EXCLUDED.delivery,run_id=EXCLUDED.run_id,target_run_id=EXCLUDED.target_run_id,
                cause_run_id=EXCLUDED.cause_run_id,prompt=EXCLUDED.prompt,state='queued',
                claim_id=NULL,owner_id=NULL,lease_expires_at=NULL,cancel_reason=NULL,
                last_sequence=EXCLUDED.last_sequence,updated_at=CURRENT_TIMESTAMP""",
                event.session_id,
                control.control_id,
                control.delivery,
                control.run_id,
                control.target_run_id,
                control.cause_run_id,
                control.prompt,
                event.sequence,
            )
            return
        if isinstance(control, ControlClaim):
            row = await connection.fetchrow(
                "UPDATE session_controls SET state='claimed',claim_id=$3,owner_id=$4,lease_expires_at=$5,last_sequence=$6,updated_at=CURRENT_TIMESTAMP WHERE session_id=$1 AND control_id=$2 RETURNING control_id",
                event.session_id,
                control.control_id,
                control.claim_id,
                control.owner_id,
                control.lease_expires_at,
                event.sequence,
            )
        elif isinstance(control, ControlDelivery):
            row = await connection.fetchrow(
                "UPDATE session_controls SET state='delivered',claim_id=$3,last_sequence=$4,updated_at=CURRENT_TIMESTAMP WHERE session_id=$1 AND control_id=$2 RETURNING control_id",
                event.session_id,
                control.control_id,
                control.claim_id,
                event.sequence,
            )
        elif isinstance(control, ControlCancellation):
            row = await connection.fetchrow(
                "UPDATE session_controls SET state='cancelled',cancel_reason=$3,last_sequence=$4,updated_at=CURRENT_TIMESTAMP WHERE session_id=$1 AND control_id=$2 RETURNING control_id",
                event.session_id,
                control.control_id,
                control.reason,
                event.sequence,
            )
        else:
            raise SessionValidationError("unsupported control projection")
        if row is None:
            raise SessionReplayError("PostgreSQL control projection is missing")
