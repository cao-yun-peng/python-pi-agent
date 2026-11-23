ALTER TABLE sessions ADD COLUMN next_run_order BIGINT NOT NULL DEFAULT 0 CHECK (next_run_order >= 0);

CREATE TABLE session_runs (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    run_id TEXT NOT NULL,
    record JSONB NOT NULL CHECK (jsonb_typeof(record) = 'object'),
    submission_key TEXT GENERATED ALWAYS AS (record->'submission'->>'submission_key') STORED NOT NULL,
    queue_order BIGINT GENERATED ALWAYS AS ((record->>'queue_order')::bigint) STORED NOT NULL CHECK (queue_order > 0),
    state TEXT GENERATED ALWAYS AS (record->>'state') STORED NOT NULL CHECK (state IN ('queued','running','completed','failed','cancelled')),
    revision BIGINT GENERATED ALWAYS AS ((record->>'revision')::bigint) STORED NOT NULL CHECK (revision > 0),
    checkpoint JSONB GENERATED ALWAYS AS (record->'checkpoint') STORED NOT NULL,
    attempt INTEGER GENERATED ALWAYS AS ((record->>'attempt')::integer) STORED NOT NULL CHECK (attempt >= 0),
    epoch BIGINT GENERATED ALWAYS AS ((record->>'epoch')::bigint) STORED NOT NULL CHECK (epoch >= 0),
    recovery_spec_ref TEXT GENERATED ALWAYS AS (record->'submission'->>'recovery_spec_ref') STORED NOT NULL,
    recovery_spec_hash TEXT GENERATED ALWAYS AS (record->'submission'->>'recovery_spec_hash') STORED NOT NULL,
    result_event_id TEXT GENERATED ALWAYS AS (CASE WHEN record->>'state'='completed'
        THEN record->'checkpoint'->'assistant_event'->>'event_id' ELSE NULL END) STORED,
    PRIMARY KEY (session_id, run_id),
    UNIQUE (session_id, submission_key),
    UNIQUE (session_id, queue_order),
    CHECK (record->'submission'->>'session_id' IS NOT NULL AND record->'submission'->>'session_id' = session_id),
    CHECK (record->'submission'->>'run_id' IS NOT NULL AND record->'submission'->>'run_id' = run_id)
);
CREATE UNIQUE INDEX idx_one_running_run ON session_runs(session_id) WHERE state='running';
CREATE INDEX idx_queued_runs ON session_runs(session_id,queue_order) WHERE state='queued';
ALTER TABLE sessions ADD CONSTRAINT sessions_active_run_fk FOREIGN KEY(session_id,active_run_id)
    REFERENCES session_runs(session_id,run_id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE run_events (
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    revision BIGINT NOT NULL CHECK (revision > 0),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    operation_id TEXT GENERATED ALWAYS AS (payload->'operation'->>'operation_id') STORED NOT NULL,
    kind TEXT GENERATED ALWAYS AS (payload->'command'->>'kind') STORED NOT NULL,
    PRIMARY KEY(session_id,run_id,revision),
    UNIQUE(session_id,operation_id),
    FOREIGN KEY(session_id,run_id) REFERENCES session_runs(session_id,run_id),
    CHECK ((payload->>'revision')::bigint = revision),
    CHECK (payload->>'session_id' = session_id AND payload->>'run_id' = run_id)
);

CREATE TABLE session_operations (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    operation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    request_digest TEXT NOT NULL CHECK (length(request_digest)=64),
    receipt JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(session_id,operation_id)
);
