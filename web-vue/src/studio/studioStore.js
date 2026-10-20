/* 制片台状态层。
 *
 * 设计原则：**界面只读派生数据，不自己算业务规则**。
 * 完成度、片段状态、可不可以生成、参考图够不够，全部由后端 `/api/studio/*`
 * 算好返回（core/studio_rules.py），前端只负责展示和把人的操作发回去。
 * 这样规则只有一份，改规则不用同时改前后端。
 */
import { reactive } from "vue";
import { getJSON, postJSON } from "../api.js";

const JOB_POLL_MS = 700;
const JOB_MAX_MS = 180000;

export const studio = reactive({
  open: false,
  tab: "production", // script | assets | production | preview
  canvas: "",
  loading: false,
  error: "",
  notice: "",

  project: null,
  script: null,
  assets: { character: [], scene: [], prop: [], style: [] },
  segments: [],
  preview: null,

  assetTab: "character",
  activeAssetId: "",
  activeSegmentId: "",
  detail: null,

  jobs: {}, // job_id -> {status, ...}
  generating: {}, // node_id -> true
  uploading: {},
  produceJob: null, // 让 Agent 开始制作的任务态
});

const qs = () => {
  const parts = [];
  if (studio.canvas) parts.push(`canvas=${encodeURIComponent(studio.canvas)}`);
  const ep = studio.project?.episode;
  if (ep) parts.push(`episode=${ep}`);
  return parts.length ? `?${parts.join("&")}` : "";
};

function fail(err) {
  studio.error = err?.message || String(err);
  studio.loading = false;
  throw err;
}

function flag(key, id, value) {
  if (value) studio[key][id] = true;
  else delete studio[key][id];
}

export function flattenAssets() {
  return ["character", "scene", "prop"].flatMap((kind) =>
    (studio.assets[kind] || []).map((item) => ({ ...item, kind })),
  );
}

export function assetById(nodeId) {
  return flattenAssets().find((item) => item.node_id === nodeId) || null;
}

export function segmentById(nodeId) {
  return studio.segments.find((item) => item.node_id === nodeId) || null;
}

/* ---------------------------------------------------------------- 读取 */

export async function loadProject() {
  studio.loading = true;
  try {
    studio.project = await getJSON(`/api/studio/project${qs()}`);
    studio.error = "";
  } catch (err) {
    fail(err);
  } finally {
    studio.loading = false;
  }
}

export async function loadAssets() {
  const data = await getJSON(`/api/studio/assets${qs()}`);
  studio.assets = {
    character: data.character || [],
    scene: data.scene || [],
    prop: data.prop || [],
    style: data.style || [],
  };
  if (!studio.activeAssetId) {
    const first = flattenAssets()[0];
    studio.activeAssetId = first ? first.node_id : "";
  }
  return data;
}

export async function loadSegments() {
  const data = await getJSON(`/api/studio/segments${qs()}`);
  studio.segments = data.segments || [];
  if (!studio.activeSegmentId && studio.segments.length) {
    studio.activeSegmentId = studio.segments[0].node_id;
  }
  return data;
}

export async function loadScript() {
  studio.script = await getJSON(`/api/studio/script${qs()}`);
  return studio.script;
}

export async function loadPreview() {
  studio.preview = await getJSON(`/api/studio/preview${qs()}`);
  return studio.preview;
}

export async function loadDetail(nodeId) {
  if (!nodeId) {
    studio.detail = null;
    return null;
  }
  studio.activeSegmentId = nodeId;
  studio.detail = await getJSON(`/api/studio/segments/${nodeId}${qs()}`);
  return studio.detail;
}

export async function loadAll() {
  if (!studio.canvas) return;
  studio.loading = true;
  try {
    await Promise.all([loadProject(), loadAssets(), loadSegments(), loadScript(), loadPreview()]);
    studio.error = "";
    if (studio.activeSegmentId) await loadDetail(studio.activeSegmentId);
  } catch (err) {
    fail(err);
  } finally {
    studio.loading = false;
  }
}

/* ---------------------------------------------------------------- 写入 */

export async function saveProject(patch) {
  const data = await postJSONPatch(`/api/studio/project${qs()}`, patch);
  studio.project = data;
  await Promise.all([loadSegments(), loadPreview()]);
  return data;
}

async function postJSONPatch(path, body) {
  const res = await fetch(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

export async function fetchTtsVoices() {
  const res = await fetch("/api/tts/voices");
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function patchAsset(nodeId, patch) {
  return postJSONPatch(`/api/studio/assets/${nodeId}`, patch);
}

export async function importScript(text, split = true) {
  const data = await postJSON(`/api/studio/import-script${qs()}`, { text, split });
  await loadAll();
  return data;
}

/* ── 让 Agent 开始制作：剧本 → 分镜 / 资产 / 提示词 ─────────────
 * 这是唯一允许 Agent 在制片台里主动写节点的入口。
 * 拆分镜本身不花钱，费用发生在之后逐段生成，所以这里不需要审批卡。
 */
export async function produceEpisode(episode, instruction = "") {
  studio.produceJob = { status: "queued", episode };
  const data = await postJSON(`/api/studio/produce`, {
    canvas: studio.canvas,
    episode,
    instruction,
  });
  studio.produceJob = data.job || { status: "running" };
  return pollProduceJob(data.job.job_id);
}

export async function pollProduceJob(jobId, { interval = 2000, maxMs = 15 * 60 * 1000 } = {}) {
  const started = Date.now();
  for (;;) {
    await new Promise((r) => setTimeout(r, interval));
    let job;
    try {
      const res = await fetch(`/api/studio/jobs/${jobId}`);
      if (!res.ok) throw new Error(`读取任务失败（${res.status}）`);
      job = await res.json();
    } catch (err) {
      studio.produceJob = { status: "failed", error: err?.message || String(err) };
      throw err;
    }
    studio.produceJob = { ...job, elapsed: Math.round((Date.now() - started) / 1000) };
    if (job.status !== "queued" && job.status !== "running") {
      if (job.status === "succeeded") {
        await loadAll();
        if (!studio.activeSegmentId) {
          const first = (studio.segments || [])[0];
          if (first) studio.activeSegmentId = first.node_id;
        }
      }
      return job;
    }
    if (Date.now() - started > maxMs) {
      studio.produceJob = { ...job, status: "timeout" };
      throw new Error("Agent 跑了太久还没有结果，可以先去画布页看它卡在哪一步");
    }
  }
}

export async function generateLook(nodeId, lookId = "", prompt = "", basedOn = "") {
  flag("generating", nodeId, true);
  try {
    const data = await postJSON(
      `/api/studio/assets/${nodeId}/generate${qs()}`,
      { look_id: lookId, prompt, based_on: basedOn || undefined },
    );
    await loadAssets();
    return data;
  } finally {
    flag("generating", nodeId, false);
  }
}

export async function patchLook(nodeId, look) {
  const data = await postJSON(`/api/studio/assets/${nodeId}/looks${qs()}`, look);
  await loadAssets();
  return data;
}

export async function createLook(nodeId, payload) {
  flag("generating", nodeId, true);
  try {
    const data = await postJSON(`/api/studio/assets/${nodeId}/looks/new${qs()}`, payload);
    await loadAssets();
    return data;
  } finally {
    flag("generating", nodeId, false);
  }
}

export async function assembleEpisode() {
  return postJSON(`/api/studio/assemble${qs()}`, {});
}

/* ── 成片：拼接 → 人工确认 → 合成 ───────────────── */
export async function prepareComposition() {
  const assembled = await assembleEpisode();
  const approval = await postJSON("/api/canvas/composition-approvals", {
    canvas: studio.canvas,
    revision: assembled.revision,
  });
  return { assembled, approval };
}

export async function runComposition(approvalId, revision) {
  return postJSON("/api/canvas/compose", {
    canvas: studio.canvas,
    revision,
    approved: true,
    approval_id: approvalId,
  });
}

export async function pollMediaJob(jobId) {
  const deadline = Date.now() + JOB_MAX_MS;
  for (;;) {
    const job = await getJSON(`/api/media/jobs/${jobId}`);
    studio.jobs[jobId] = job;
    if (job.status !== "queued" && job.status !== "running") {
      await loadPreview();
      return job;
    }
    if (Date.now() > deadline) return job;
    await new Promise((resolve) => setTimeout(resolve, 1200));
  }
}

export async function savePrompt(nodeId, prompt) {
  await postJSON(`/api/studio/segments/${nodeId}/prompt${qs()}`, { prompt });
  await Promise.all([loadDetail(nodeId), loadSegments()]);
}

export async function saveTags(nodeId, tags) {
  await postJSON(`/api/studio/segments/${nodeId}/tags${qs()}`, { tags });
  await loadDetail(nodeId);
}

export async function selectTake(nodeId, takeId) {
  await postJSON(`/api/studio/segments/${nodeId}/take${qs()}`, { take_id: takeId });
  await Promise.all([loadDetail(nodeId), loadSegments(), loadPreview()]);
}

/** 生成一条片段并轮询到终态。
 * 后端约定：硬校验不通过时，必须显式带上 duration 才放行（等于人工确认「我知道它有问题」）。
 * 所以这里**只有调用方明确要强制时才发 duration**，否则交给后端按校验结果决定放不放行。
 */
export async function generateSegment(nodeId, { duration = 0, shotNo = 0 } = {}) {
  studio.notice = "";
  flag("generating", nodeId, true);
  try {
    const body = { ...(duration ? { duration } : {}), ...(shotNo ? { shot_no: shotNo } : {}) };
    const data = await postJSON(`/api/studio/segments/${nodeId}/generate${qs()}`, body);
    const jobId = data.job.job_id;
    studio.jobs[jobId] = data.job;
    await pollJob(jobId);
    await Promise.all([loadDetail(nodeId), loadSegments(), loadPreview()]);
    return studio.jobs[jobId];
  } finally {
    flag("generating", nodeId, false);
  }
}

/** 生成「这一镜的第一帧」。之后出片会以它为准走首帧模式，构图与人物位置就归我们控制。 */
export async function generateKeyframe(nodeId, { shotNo = 1 } = {}) {
  studio.notice = "";
  flag("generating", nodeId, true);
  try {
    const data = await postJSON(`/api/studio/segments/${nodeId}/keyframe${qs()}`, { shot_no: shotNo });
    await Promise.all([loadDetail(nodeId), loadSegments(), loadPreview()]);
    return data.keyframe;
  } finally {
    flag("generating", nodeId, false);
  }
}

let toastTimer = 0;
/** 顶部/底部一闪而过的提示。比 window.alert 少一次点击。 */
export function toast(message, ms = 2200) {
  studio.notice = message;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    studio.notice = "";
  }, ms);
}

async function pollJob(jobId) {
  const started = Date.now();
  for (;;) {
    const job = await getJSON(`/api/studio/jobs/${jobId}`);
    studio.jobs[jobId] = job;
    if (job.status !== "queued" && job.status !== "running") return job;
    if (Date.now() - started > JOB_MAX_MS) {
      studio.notice = "生成超时，可稍后刷新查看结果";
      return job;
    }
    await new Promise((resolve) => setTimeout(resolve, JOB_POLL_MS));
  }
}

export async function cancelJob(jobId) {
  await postJSON(`/api/studio/jobs/${jobId}/cancel`);
}
