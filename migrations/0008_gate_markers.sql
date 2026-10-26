-- 0008（gate severity tiers Task 3）：产物行随存的分级门标记。
-- findings 与 StageResult.findings 同形：[{"gate","rule","severity","detail"}]，
-- run 阶段通过路径在 pipeline_repository.save(findings=...) 时写入；
-- save(findings=None) 保留行内既有值（UPDATE 不触碰该列），save(findings=[]) 显式清空。
ALTER TABLE pipeline_artifacts ADD COLUMN IF NOT EXISTS findings JSONB NOT NULL DEFAULT '[]';
