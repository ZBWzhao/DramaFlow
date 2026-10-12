CREATE TABLE IF NOT EXISTS tool_invocations (
    invocation_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    args_json JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('prepared','executing','succeeded','failed','unknown')),
    attempt INTEGER NOT NULL DEFAULT 0,
    result_json JSONB,
    error TEXT,
    remote_operation_id TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS tool_invocations_session_idx ON tool_invocations(session_id, created_at);
CREATE INDEX IF NOT EXISTS tool_invocations_run_idx ON tool_invocations(run_id, created_at);

CREATE TABLE IF NOT EXISTS approval_requests (
    approval_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','consumed','expired','cancelled')),
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    used_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS approval_requests_status_idx ON approval_requests(status, expires_at);

CREATE TABLE IF NOT EXISTS media_jobs (
    job_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    approval_id TEXT NOT NULL,
    canvas TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed','unknown')),
    attempt INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT NOT NULL DEFAULT '',
    lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
    result_json JSONB,
    error TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    started_at DOUBLE PRECISION,
    finished_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS media_jobs_claim_idx ON media_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS media_jobs_lease_idx ON media_jobs(status, lease_until);

-- Tables below establish the next migration boundary. Existing JSONL/canvas
-- adapters remain active until their repository adapters are switched on.
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS canvases (
    canvas_id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(project_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    content JSONB NOT NULL DEFAULT '{}'::jsonb,
    revision BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(project_id, name)
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(project_id) ON DELETE SET NULL,
    canvas_id TEXT REFERENCES canvases(canvas_id) ON DELETE SET NULL,
    title TEXT NOT NULL DEFAULT '',
    current_message_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS session_entries (
    entry_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    parent_id TEXT,
    sequence BIGSERIAL,
    entry_type TEXT NOT NULL,
    role TEXT,
    content JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS session_entries_replay_idx ON session_entries(session_id, sequence);
CREATE INDEX IF NOT EXISTS session_entries_parent_idx ON session_entries(session_id, parent_id);
CREATE TABLE IF NOT EXISTS media_assets (
    asset_id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(project_id) ON DELETE SET NULL,
    canvas_id TEXT REFERENCES canvases(canvas_id) ON DELETE SET NULL,
    node_id TEXT,
    media_type TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    content_type TEXT,
    file_size BIGINT,
    sha256 TEXT,
    provider TEXT,
    remote_operation_id TEXT,
    generation_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS outbox_events (
    outbox_id BIGSERIAL PRIMARY KEY,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS outbox_unpublished_idx ON outbox_events(outbox_id) WHERE published_at IS NULL;
