-- 0007（failure-recovery Task 2）：失败阶段草稿 + 重试意见。
-- stage_drafts[stage] = 该失败阶段的最终草稿（失败落账/重试种子共用一列）；
-- retry_instruction = 单值 TEXT（重试唯一失败阶段 ⇒ 同一时刻只有一个意见）。
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS stage_drafts JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS retry_instruction TEXT NOT NULL DEFAULT '';
