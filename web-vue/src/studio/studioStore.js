/* 制片台状态层。
 *
 * 设计原则：**界面只读派生数据，不自己算业务规则**。
 * 完成度、片段状态、可不可以生成、参考图够不够，全部由后端 `/api/studio/*`
 * 算好返回（core/studio_rules.py），前端只负责展示和把人的操作发回去。
 * 这样规则只有一份，改规则不用同时改前后端。
 */
import { computed, reactive } from "vue";
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

  // 管线产物（阶段 1）：artifacts = 列表接口元数据按类型索引；detail = 单条 GET 全形状（含 content）。
  // 画布老项目没有导入产物 → 列表为空，剧本页零变化。canvas = 缓存归属的项目，
  // loadPipeline 换项目先清缓存（SPA 内不重建 store，防串项目）。
  pipeline: { loaded: false, canvas: "", artifacts: {}, detail: {} },

  // 管线运行（阶段 2 Task 4）：id 为空 = 没有活动运行。状态机与 SSE 消费见文件尾「管线运行」节。
  // project_id 随 startRun 落账：面板按 run.project_id === canvas 判归属，防止 A 项目的
  // 运行卡（尤其确认门）渲染在 B 项目页面上、对着 B 的大纲缓存点确认（review 修复）。
  run: { id: "", project_id: "", status: "", stage: "", stages: {}, events: [], link: "" },
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
    // 流正常结束必然以 analysis_done 收尾；不是它 = 连接中途断了，
    // 此时旧产物虽在但「这次跑没跑成」必须说出来，不能静默装作无事发生。
    const done = events[events.length - 1];
    if (done?.type !== "analysis_done") {
      throw new Error("解析流提前结束，这次可能没跑完，请重试");
    }
    studio.storyAnalysis = done.analysis;
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
  if (!res.ok) {
    const payload = await res.json().catch(() => ({}));
    // detail（FastAPI 校验）优先；管线 422 是 {message, findings} 形状，取 message
    throw new Error(payload.detail || payload.message || res.statusText);
  }
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
/** 发起制作，立即返回初始 job（job_id）——不等跑完。
 * ScriptView 用它先拿到 job，再轮询出 run_id 让 AI 助手跟单，最后才 pollProduceJob 等终态。
 */
export async function startProduce(episode, instruction = "") {
  studio.produceJob = { status: "queued", episode };
  const data = await postJSON(`/api/studio/produce`, {
    canvas: studio.canvas,
    episode,
    instruction,
  });
  studio.produceJob = data.job || { status: "running" };
  return data.job;
}

export async function produceEpisode(episode, instruction = "") {
  const job = await startProduce(episode, instruction);
  return pollProduceJob(job.job_id);
}

export async function pollProduceJob(jobId, { interval = 2000, maxMs = 45 * 60 * 1000 } = {}) {
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
      // limited 也是「有半成品」的终态：刷新片段/资产，让用户看到已产出的部分
      if (job.status === "succeeded" || job.status === "limited") {
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

/** 批量出片：一个服务端 job 排队生成 N 段，进度/取消走任务中心；单项失败不影响其余。 */
export async function generateBatch(nodeIds, { force = false } = {}) {
  if (!nodeIds.length) return null;
  const data = await postJSON(`/api/studio/segments/batch-generate${qs()}`, { node_ids: nodeIds, force });
  const jobId = data.job.job_id;
  studio.jobs[jobId] = data.job;
  const job = await pollJob(jobId);
  await Promise.all([loadSegments(), loadPreview()]);
  if (studio.activeSegmentId) await loadDetail(studio.activeSegmentId).catch(() => {});
  return job;
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

/* ── 管线产物（阶段 1）：剧本页「管线大纲」tab 的只读数据源 ─────────
 * GET artifacts 列表 → 有 outline 再 GET 详情（404/网络错都吞掉只记 console：
 * 剧本页是画布项目的家，不能因为管线查询不可用而跟着挂）。loaded 防双拉；
 * 换项目先清缓存（SPA 里 Home → 开另一个项目不会重建 store，不 reset 会把
 * A 的产物摆在 B 的名字下）；空列表/读取失败不落锁——导入后回到本页、或
 * 下次进页面，都能重新拉到。
 */
export async function loadPipeline(force = false) {
  if (!studio.canvas) return;
  if (studio.pipeline.canvas !== studio.canvas) {
    studio.pipeline = { loaded: false, canvas: studio.canvas, artifacts: {}, detail: {} };
  }
  if (studio.pipeline.loaded && !force) return;
  studio.pipeline.loaded = true;
  try {
    const base = `/api/pipeline/projects/${encodeURIComponent(studio.canvas)}/artifacts`;
    const list = (await getJSON(base)).artifacts || [];
    studio.pipeline.artifacts = Object.fromEntries(list.map((a) => [a.artifact_type, a]));
    if (studio.pipeline.artifacts.outline) {
      studio.pipeline.detail.outline = await getJSON(`${base}/outline`);
    }
    // Task 5：script 产物存在 → 详情也一并取。响应自带逐集正文视图
    // episodes[]（text = edited_text 优先，否则后端按 flow 节拍派生）。
    if (studio.pipeline.artifacts.script) {
      studio.pipeline.detail.script = await getJSON(`${base}/script`);
    }
    // Task 6：制片台四类产物（角色/美术/站位/分镜）详情同样有则取。
    // PipelineView 是唯一消费方；与上面同权吞错（面板页只读，读取失败不炸页面）。
    for (const type of ["characters", "art", "staging", "storyboard"]) {
      if (studio.pipeline.artifacts[type]) {
        studio.pipeline.detail[type] = await getJSON(`${base}/${type}`);
      }
    }
    // 空列表不算定稿：导入前打开过本页时，导入后回到本页要靠这次重拉才能
    // 看到产物（「管线大纲」tab / 制片台面板），落锁就永远出不来了。
    studio.pipeline.loaded = list.length > 0;
  } catch (err) {
    studio.pipeline.loaded = false; // 失败不占坑：下次进页面再试，而不是永久空白
    console.error("管线产物读取失败（不影响剧本页）", err);
  }
}

/* ── 管线剧本（阶段 1 Task 5）：script 产物 → 剧本页集列表/正文的数据源 ─────
 * 画布老项目没有 script 产物 → hasPipelineScript 恒 false，剧本页零变化。
 */

/** script 产物详情是否在手；配合「详情里有集列表」一起判断才走管线路径。 */
export const hasPipelineScript = computed(() => Boolean(studio.pipeline?.detail?.script));

/** 集级编辑落库：只保存当前集（PUT edited_text），不再"整本重新分集"。
 * 响应是刷新后的 script 详情（含逐集 text），直接整体替换本地缓存。 */
export async function savePipelineEpisode(no, text) {
  const data = await putJSON(
    `/api/pipeline/projects/${encodeURIComponent(studio.canvas)}/artifacts/script/episodes/${no}`,
    { text },
  );
  studio.pipeline.detail.script = data;
  return data;
}

/* ── 管线运行（阶段 2 Task 4）：粘贴小说 → SSE 直播 → 大纲确认门 ─────
 * 服务端契约：server/pipeline_runs.py 六端点 + SSE。run.status 同 0006 CHECK：
 * running / gated / failed / done / cancelled；run.id 为空 = 没有活动运行。
 * events 存 SSE 原始帧：hello / stage / tool / tool_result / stage_gate /
 * stage_done / gate（run 级确认门）/ failed / done / retry——注意执行器内部的
 * gate/done 已被服务端重标为 stage_gate/stage_done，这里的 gate/done 只属于
 * run 级语义。stages[stage] = {status, gate_ok, round} 是面板友好的阶段摘要。
 * link 是 SSE 连接态：'' / connecting / open / reconnecting / lost。EventSource
 * 断线自动重连，重连后的 hello 首帧带运行快照（broker 不回放历史），状态以
 * 快照为准——approve/retry 重开流、或开流晚于 gated，都靠它追平，否则按钮
 * 态会停在旧状态。
 */
let runStream = null; // 当前运行的 EventSource（模块级单例，不进 reactive）

export async function startRun(projectId, novel) {
  const data = await postJSON(`/api/pipeline/runs`, { project_id: projectId, novel_text: novel });
  studio.run = {
    id: data.run_id,
    project_id: projectId,
    status: data.status || "running",
    stage: data.stage || "",
    stages: {},
    events: [],
    link: "connecting",
  };
  _openRunStream();
  return data;
}

function _openRunStream() {
  closeRunStream();
  studio.run.link = "connecting";
  const es = new EventSource(`/api/pipeline/runs/${studio.run.id}/events`);
  es.onmessage = (e) => {
    let msg;
    try {
      msg = JSON.parse(e.data);
    } catch {
      return; // 坏帧跳过，不断流
    }
    studio.run.link = "open";
    studio.run.events.push(msg);
    if (msg.type === "hello") {
      // 快照同步：hello 的 {status, stage} 是晚连上/重连后的唯一事实来源
      if (msg.status) studio.run.status = msg.status;
      if (msg.stage) studio.run.stage = msg.stage;
      if (msg.status === "done" || msg.status === "cancelled") {
        // 终态运行的服务端流在 hello 后即收，客户端同步关闭；产物可能有新，强制重拉
        void loadPipeline(true);
        closeRunStream();
      }
      return;
    }
    if (msg.type === "stage") {
      studio.run.stage = msg.stage;
      studio.run.stages[msg.stage] = { ...studio.run.stages[msg.stage], status: msg.status };
    }
    if (msg.type === "stage_gate") {
      // 阶段级质量门（含自检重跑），不是确认门——别把 gated 打开
      const s = studio.run.stages[msg.stage] || (studio.run.stages[msg.stage] = {});
      s.gate_ok = Boolean(msg.ok);
      s.round = msg.round;
    }
    if (msg.type === "stage_done") {
      const s = studio.run.stages[msg.stage] || (studio.run.stages[msg.stage] = {});
      if (msg.ok) {
        s.status = "done";
        // 阶段产物落库即强制重拉：制片台面板的 done 态要立刻能渲染该阶段 content，
        // 下游面板/确认卡也要读到最新缓存（与 run 级 gate/done 的重拉同一机制）。
        void loadPipeline(true);
      }
    }
    if (msg.type === "gate") {
      // run 级确认门（红线）：停在 gated 等人工；大纲产物落库了，强制重拉供确认对话框展示
      studio.run.status = "gated";
      studio.run.stage = msg.stage;
      void loadPipeline(true);
    }
    if (msg.type === "done") {
      studio.run.status = "done";
      void loadPipeline(true);
      closeRunStream(); // 服务端已收流；不主动关会触发 onerror 重连
    }
    if (msg.type === "failed") {
      studio.run.status = "failed";
      if (msg.stage) {
        studio.run.stage = msg.stage;
        studio.run.stages[msg.stage] = { ...studio.run.stages[msg.stage], status: "failed" };
      }
      closeRunStream(); // failed 是终态事件，服务端同样收流
    }
  };
  es.onerror = () => {
    // 终态分支已主动 close，走到这里的多半是网络抖动：EventSource 自动重连，
    // 成功后 hello 快照把状态追平。readyState=CLOSED 才是真断（服务不在了）。
    studio.run.link = es.readyState === EventSource.CLOSED ? "lost" : "reconnecting";
  };
  runStream = es;
}

/** 关闭直播连接（终态收尾/组件侧显式止损用；运行本身在服务端继续）。 */
export function closeRunStream() {
  if (runStream) {
    runStream.close();
    runStream = null;
  }
  if (studio.run) studio.run.link = "";
}

/** 确认门放行：大纲过人工确认后继续后续阶段。门不可跳过——非 gated 会被
 * 服务端 409 拒绝（"确认门不可跳过"），错误文案原样透给面板。 */
export async function approveRun() {
  await postJSON(`/api/pipeline/runs/${studio.run.id}/approve`, {});
  studio.run.status = "running";
  _openRunStream(); // resume 可能在新流 hello 之前就跑完——终态由快照兜底
}

/** 重置失败阶段重跑（仅 failed；其余状态服务端 409）。注意重试确认门阶段
 * 过门后会再次停在 gated——确认卡的可见性完全跟随 status，门后依旧等人工。 */
export async function retryRun(stage) {
  await postJSON(`/api/pipeline/runs/${studio.run.id}/retry`, { stage });
  studio.run.status = "running";
  _openRunStream();
}

/** 追加一条人工意见（确认前的「带意见重写」留言；门不会因此自动放行）。 */
export async function sendRunFeedback(text) {
  const trimmed = String(text || "").trim();
  if (!trimmed) throw new Error("意见内容不能为空");
  return postJSON(`/api/pipeline/runs/${studio.run.id}/feedback`, { text: trimmed });
}
