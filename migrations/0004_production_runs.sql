CREATE TABLE IF NOT EXISTS production_runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    canvas_name TEXT NOT NULL,
    status TEXT NOT NULL,
    current_stage TEXT NOT NULL,
    content JSONB NOT NULL,
    revision BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS production_runs_session_idx
    ON production_runs(session_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS production_runs_status_idx
    ON production_runs(status, updated_at DESC);
