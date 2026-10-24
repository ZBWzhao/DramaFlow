-- 管线产物表：shuohao 技能链各层 JSON 的统一落库点。
-- latest-wins：PRIMARY KEY (project_id, artifact_type)，导入即 upsert。
-- schema_version 应对上游 shuohao 技能 schema 演进。
CREATE TABLE IF NOT EXISTS pipeline_artifacts (
    project_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL CHECK (artifact_type IN
        ('outline','script','characters','art','staging','storyboard','character_refs')),
    schema_version TEXT NOT NULL DEFAULT '',
    content JSONB NOT NULL,
    source_run_id TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (project_id, artifact_type)
);

CREATE INDEX IF NOT EXISTS pipeline_artifacts_project_updated_idx
    ON pipeline_artifacts(project_id, updated_at);
