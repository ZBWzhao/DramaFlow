-- project_id is always non-null for repository-managed canvases, so this
-- constraint is the optimistic repository's stable natural key.
ALTER TABLE canvases ALTER COLUMN project_id SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS canvases_project_name_uidx ON canvases(project_id, name);
