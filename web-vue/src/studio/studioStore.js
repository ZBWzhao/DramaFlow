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

  // 剧本理解四卡 + 资产表（草稿区）。产物归一后的全形状由后端保证。
  storyAnalysis: null,
  assetTables: null,
  analysis: { running: false, cards: {} }, // cards[key] = {status, text, error}
  parsing: { running: false, episodeNo: 0 },
  syncing: false,
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
    studio.storyAnalysis = studio.project.story_analysis || null;
    studio.assetTables = studio.project.asset_tables || null;
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

/* ---------------------------------------------------------- 剧本理解 / 资产表 pipeline */

/* pipeline 专用流式消费：不需要会话恢复，比 streamNDJSON 少一层重连机制 */
async function streamPipeline(path, body, onEvent) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) {
    const detail = (await res.json().catch(() => ({}))).detail || res.statusText;
    throw new Error(detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const events = [];
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const event = JSON.parse(line);
        events.push(event);
        onEvent?.(event);
      } catch {
        console.warn("坏事件（已跳过）", line.slice(0, 120));
      }
    }
  }
  return events;
}

export async function runStoryAnalysis({ cards, onEvent } = {}) {
  if (studio.analysis.running) throw new Error("已经在解析中");
  studio.analysis.running = true;
  studio.analysis.cards = {};
  try {
    const events = await streamPipeline(
      "/api/studio/story-analysis/run",
      { canvas: studio.canvas, cards: cards?.length ? cards : undefined },
      (event) => {
        if (event.type === "card_start") {
          studio.analysis.cards[event.key] = { status: "running", text: "" };
        } else if (event.type === "card_delta") {
          const card = studio.analysis.cards[event.key] || (studio.analysis.cards[event.key] = { status: "running", text: "" });
          card.text += event.text || "";
        } else if (event.type === "card_done") {
          studio.analysis.cards[event.key] = { status: "done", text: "" };
        } else if (event.type === "card_failed") {
          studio.analysis.cards[event.key] = { status: "failed", text: "", error: event.error };
        }
        onEvent?.(event);
      },
    );
    const done = events[events.length - 1];
    studio.storyAnalysis = done?.analysis || studio.storyAnalysis;
    return done;
  } finally {
    studio.analysis.running = false;
  }
}

export async function cancelStoryAnalysis() {
  return postJSON("/api/studio/story-analysis/cancel", { canvas: studio.canvas });
}

/* 每卡解析指令（提示词库）：custom 为空串表示用内置默认 */
export async function getStoryPrompts() {
  return getJSON(`/api/studio/story-analysis/prompts?canvas=${encodeURIComponent(studio.canvas)}`);
}

export async function saveStoryPrompt(card, prompt) {
  return postJSON("/api/studio/story-analysis/prompts", { canvas: studio.canvas, card, prompt });
}

export async function parseAssetTables(onEvent) {
  if (studio.parsing.running) throw new Error("已经在解析中");
  studio.parsing.running = true;
  studio.parsing.episodeNo = 0;
  try {
    const events = await streamPipeline("/api/studio/asset-tables/parse", { canvas: studio.canvas }, (event) => {
      if (event.type === "parse_progress") studio.parsing.episodeNo = event.episode_no;
      onEvent?.(event);
    });
    const done = events[events.length - 1];
    studio.assetTables = done?.tables || studio.assetTables;
    return done;
  } finally {
    studio.parsing.running = false;
    studio.parsing.episodeNo = 0;
  }
}

export async function saveAssetTables(tables) {
  const data = await putJSON("/api/studio/asset-tables", { canvas: studio.canvas, tables });
  studio.assetTables = data.tables;
  return data;
}

async function putJSON(path, body) {
  const res = await fetch(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

export async function syncAssetTables() {
  if (studio.syncing) throw new Error("正在同步");
  studio.syncing = true;
  try {
    const report = await postJSON("/api/studio/asset-tables/sync", { canvas: studio.canvas });
    studio.assetTables = { ...(studio.assetTables || {}), synced_at: report.synced_at, synced_state: report.synced_state || {} };
    await loadAssets();
    return report;
  } finally {
    studio.syncing = false;
  }
}

/* 生图占位：不接 API，返回提示词导出地址，浏览器直接下载 */
export function exportLookPromptUrl(nodeId, lookId) {
  return `/api/studio/looks/export-prompt?canvas=${encodeURIComponent(studio.canvas)}&asset=${encodeURIComponent(nodeId)}&look=${encodeURIComponent(lookId)}`;
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
export async function generateSegment(nodeId, { duration = 0, shotNo = 0, force = false } = {}) {
  studio.notice = "";
  flag("generating", nodeId, true);
  try {
    const body = {
      ...(duration ? { duration } : {}),
      ...(shotNo ? { shot_no: shotNo } : {}),
      ...(force ? { force: true } : {}),
    };
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

/** 首帧墙：整集静帧审阅（批量出片前过一遍构图/站位/一致性）。 */
export async function loadStoryboard(episodeNo) {
  const res = await fetch(`/api/studio/episodes/${episodeNo}/storyboard${qs()}`);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

let toastTimer = 0;
/** 顶部/底部一闪而过的提示。比 window.alert 少一次点击。 */
/* 兜底轮询：pollJob / pollMediaJob 都有 180s 截止，超时后没有谁再更新这些
 * job——任务中心不补查就会永远"运行中"。这里把仍在跑的 job 每几秒补查一次
 * （studio 与 media 两个 job 存放处都试；两处都 404 才判任务丢失）。 */
export async function sweepJobs() {
  const active = Object.entries(studio.jobs)
    .filter(([, job]) => job && ["queued", "running"].includes(job.status))
    .map(([id]) => id);
  for (const id of active) {
    let job = null;
    let gone = true;
    for (const path of [`/api/studio/jobs/${id}`, `/api/media/jobs/${id}`]) {
      try {
        job = await getJSON(path);
        gone = false;
        break;
      } catch (err) {
        if (err?.status !== 404) { gone = false; break; } // 暂时性错误：保持原状
      }
    }
    if (job) studio.jobs[id] = job;
    else if (gone) studio.jobs[id] = { ...studio.jobs[id], status: "failed", error: "查询不到该任务（可能已过期）" };
  }
}

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
