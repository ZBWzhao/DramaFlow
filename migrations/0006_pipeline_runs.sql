CREATE TABLE IF NOT EXISTS pipeline_runs (
  run_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  novel_text TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'running'
    CHECK (status IN ('running','gated','failed','done','cancelled')),
  stage TEXT NOT NULL DEFAULT 'outline',
  stage_status JSONB NOT NULL DEFAULT '{}'::jsonb,
  feedback TEXT NOT NULL DEFAULT '',
  findings JSONB,
  created_at DOUBLE PRECISION NOT NULL DEFAULT (EXTRACT(EPOCH FROM now())),
  updated_at DOUBLE PRECISION NOT NULL DEFAULT (EXTRACT(EPOCH FROM now()))
);
CREATE INDEX IF NOT EXISTS pipeline_runs_project_idx ON pipeline_runs(project_id, updated_at);
