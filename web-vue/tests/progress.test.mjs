import assert from "node:assert/strict";
import test from "node:test";

import { PROGRESS_TOTAL, generationProgress, progressDash, progressText } from "../src/canvas2/progress.js";

test("没有进度数据时按排队中 0/3 画，不编造百分比", () => {
  const progress = generationProgress(null);
  assert.equal(progress.stage, "queued");
  assert.equal(progress.step, 0);
  assert.equal(progress.total, PROGRESS_TOTAL);
  assert.equal(progress.ratio, 0);
  assert.equal(progress.label, "排队中");
});

test("后端写了 stage/step/total 时原样投影", () => {
  const progress = generationProgress({ generation_progress: { stage: "writing", step: 2, total: 3 } });
  assert.deepEqual(
    { stage: progress.stage, step: progress.step, total: progress.total, label: progress.label },
    { stage: "writing", step: 2, total: 3, label: "写回画布" },
  );
  assert.ok(Math.abs(progress.ratio - 2 / 3) < 1e-9);
});

test("越界的 step 会被夹到 0..total，坏数据不会把环画到反方向", () => {
  assert.equal(generationProgress({ generation_progress: { stage: "submitting", step: 9, total: 3 } }).step, 3);
  assert.equal(generationProgress({ generation_progress: { stage: "submitting", step: -2, total: 3 } }).step, 0);
  assert.equal(generationProgress({ generation_progress: { total: 0 } }).total, PROGRESS_TOTAL);
});

test("文案与环偏移：0/3 环为空，3/3 环闭合", () => {
  assert.equal(progressText(generationProgress(null)), "0 / 3");
  assert.equal(progressText(generationProgress({ generation_progress: { stage: "writing", step: 2, total: 3 } })), "2 / 3");
  const empty = progressDash(generationProgress(null));
  const full = progressDash(generationProgress({ generation_progress: { stage: "writing", step: 3, total: 3 } }));
  assert.ok(Math.abs(empty.offset - empty.circumference) < 1e-9);
  assert.ok(Math.abs(full.offset) < 1e-9);
});
