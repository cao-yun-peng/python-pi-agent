-- Local SessionStore foundation; execution journal tables arrive in Step 4.
CREATE TABLE sessions (
    session_id TEXT CONSTRAINT sessions_pkey PRIMARY KEY CHECK (btrim(session_id) <> ''),
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    last_compaction_seq BIGINT NOT NULL DEFAULT 0 CHECK (last_compaction_seq BETWEEN 0 AND version),
    execution_mode TEXT NOT NULL DEFAULT 'local' CHECK (execution_mode IN ('local', 'distributed')),
    epoch BIGINT NOT NULL DEFAULT 0 CHECK (epoch >= 0),
    lease_purpose TEXT CHECK (lease_purpose IN ('run', 'maintenance')),
    owner_id TEXT,
    claim_id TEXT,
    active_run_id TEXT,
    lease_expires_at TIMESTAMPTZ,
    parent_session_id TEXT REFERENCES sessions(session_id),
    forked_at_version BIGINT CHECK (forked_at_version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((lease_purpose IS NULL AND owner_id IS NULL AND claim_id IS NULL AND lease_expires_at IS NULL)
        OR (lease_purpose IS NOT NULL AND owner_id IS NOT NULL AND claim_id IS NOT NULL AND lease_expires_at IS NOT NULL)),
    CHECK (lease_purpose IS NULL OR (lease_purpose = 'run' AND active_run_id IS NOT NULL)
        OR (lease_purpose = 'maintenance' AND active_run_id IS NULL))
);

CREATE TABLE session_events (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    sequence BIGINT NOT NULL CHECK (sequence > 0),
    -- Complete shared schema v6 envelope; generated columns cannot drift from JSON.
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    event_id TEXT GENERATED ALWAYS AS (payload->>'event_id') STORED NOT NULL,
    kind TEXT GENERATED ALWAYS AS (payload->>'kind') STORED NOT NULL
        CHECK (kind IN ('message_committed', 'context_compacted', 'control_enqueued',
            'control_claimed', 'control_delivered', 'control_cancelled')),
    run_id TEXT GENERATED ALWAYS AS (payload->>'run_id') STORED,
    turn_id TEXT GENERATED ALWAYS AS (payload->>'turn_id') STORED,
    schema_version INTEGER GENERATED ALWAYS AS ((payload->>'schema_version')::integer) STORED NOT NULL CHECK (schema_version = 6),
    provenance JSONB GENERATED ALWAYS AS (payload->'provenance') STORED NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT session_events_pkey PRIMARY KEY (session_id, sequence),
    CONSTRAINT session_events_event_id_key UNIQUE (event_id),
    CHECK (payload->>'session_id' IS NOT NULL AND payload->>'session_id' = session_id),
    CHECK (payload->>'sequence' IS NOT NULL AND (payload->>'sequence')::bigint = sequence)
);
CREATE INDEX idx_session_events_run ON session_events(session_id, run_id, sequence) WHERE run_id IS NOT NULL;

CREATE TABLE session_controls (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    control_id TEXT NOT NULL,
    delivery TEXT NOT NULL CHECK (delivery IN ('steer', 'follow_up')),
    run_id TEXT NOT NULL,
    target_run_id TEXT,
    cause_run_id TEXT,
    prompt TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued', 'claimed', 'delivered', 'cancelled')),
    claim_id TEXT,
    owner_id TEXT,
    lease_expires_at DOUBLE PRECISION,
    last_sequence BIGINT NOT NULL,
    cancel_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, control_id),
    FOREIGN KEY (session_id, last_sequence) REFERENCES session_events(session_id, sequence)
);
CREATE INDEX idx_session_controls_pending ON session_controls(session_id, last_sequence) WHERE state IN ('queued', 'claimed');
CREATE INDEX idx_session_execution_expiry ON sessions(lease_expires_at) WHERE lease_purpose IS NOT NULL;
