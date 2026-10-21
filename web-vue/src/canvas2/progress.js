/** 生成进度：后端在 node.data.generation_progress 上写 { stage, step, total }。
 *  这里只做展示投影，不编造后端没有的精度。 */
export const PROGRESS_TOTAL = 3;

const STAGE_LABELS = {
  queued: "排队中",
  preparing: "准备输入",
  submitting: "调用模型",
  writing: "写回画布",
};

export function generationProgress(data) {
  const raw = data && data.generation_progress;
  const total = Number(raw && raw.total) > 0 ? Number(raw.total) : PROGRESS_TOTAL;
  const rawStep = Number(raw && raw.step);
  const step = Number.isFinite(rawStep) ? Math.max(0, Math.min(total, rawStep)) : 0;
  const stage = raw && raw.stage ? String(raw.stage) : "queued";
  return {
    stage,
    step,
    total,
    ratio: total > 0 ? step / total : 0,
    label: STAGE_LABELS[stage] || "生成中",
  };
}

export function progressText(progress) {
  if (!progress) return "";
  return `${progress.step} / ${progress.total}`;
}

export function progressDash(progress, radius = 13) {
  const circumference = 2 * Math.PI * radius;
  const ratio = progress ? Math.max(0, Math.min(1, progress.ratio)) : 0;
  return { circumference, offset: circumference * (1 - ratio) };
}
