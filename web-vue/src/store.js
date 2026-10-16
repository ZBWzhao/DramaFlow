/* 全局状态与动作（一个模块级 reactive，等价于轻量版 Pinia，省一个依赖）。
 *
 * 三条设计要点：
 *   1. 画布数据保持"服务端原样"，Vue Flow 需要的 nodes/edges 由 computed 派生 ——
 *      视图只是视图，权威数据永远在服务端 JSON 里。
 *   2. 对话按 Toonflow 的做法：**一条 assistant 消息 = 有序 parts[]**
 *      （thinking / tool / text 内联堆叠，按发生顺序）。不是"一种事件一个气泡"——
 *      那样必然出现一屏空气泡，也丢掉"哪一步之后改了什么"。
 *   3. 流式文本按 block_id 归属：一次模型请求是一个 block（一次 assistant 回复）。
 */

import { computed, reactive } from "vue";
import { getJSON, postFile, postJSON, streamNDJSON } from "./api.js";

let activeRunController = null;
const settingsStorage = typeof localStorage === "undefined" ? null : localStorage;
const savedSetting = (key, fallback) => settingsStorage?.getItem(key) || fallback;
const persistSetting = (key, value) => settingsStorage?.setItem(key, value);
const sessionBindingsKey = "toonflow-session-by-canvas";
const mediaJobBindingsKey = "toonflow-media-job-by-canvas";
let sessionBindings = {};
let mediaJobBindings = {};
try {
  sessionBindings = JSON.parse(settingsStorage?.getItem(sessionBindingsKey) || "{}");
  mediaJobBindings = JSON.parse(settingsStorage?.getItem(mediaJobBindingsKey) || "{}");
} catch {
  sessionBindings = {};
  mediaJobBindings = {};
}

function rememberMediaJob(canvas, jobId) {
  if (jobId) mediaJobBindings[canvas] = jobId;
  else delete mediaJobBindings[canvas];
  settingsStorage?.setItem(mediaJobBindingsKey, JSON.stringify(mediaJobBindings));
}

function rememberSession(canvas, sessionId) {
  if (!canvas || !sessionId) return;
  sessionBindings[canvas] = sessionId;
  settingsStorage?.setItem(sessionBindingsKey, JSON.stringify(sessionBindings));
}

function forgetSession(sessionId) {
  for (const [canvas, saved] of Object.entries(sessionBindings)) {
    if (saved === sessionId) delete sessionBindings[canvas];
  }
  settingsStorage?.setItem(sessionBindingsKey, JSON.stringify(sessionBindings));
}

export const state = reactive({
  // ---------------- 视图 ----------------
  drawerOpen: false,

  // ---------------- 画布 ----------------
  canvases: [],
  canvasMeta: {},
  canvasName: "main",
  canvas: { meta: {}, nodes: [], edges: [], spec: {}, topo: [], problems: [] },
  selectedId: null,
  selectedEdgeId: null,
  sessions: [],
  sessionLoading: false,
  canvasBusy: false,
  mediaUploading: false,
  runResult: null, // { total, done, results: [] }  一键生成的结果条
  mediaJobId: null,
  generationReview: null, // 生成前人工审批卡
  compositionReview: null, // 成片合成前人工审批卡
  canvasActivity: null, // Agent 正在/刚刚对画布做了什么
  changedNodeIds: [], // 最近一次工具调用改变的节点，用于画布脉冲高亮
  canvasRevision: 0,
  canvasServerRevision: 0,
  exportData: null, // { path, segments, chars, markdown }
  exportOpen: false,

  // ---------------- 对话 ----------------
  feed: [], // { type:'user' } | { type:'assistant', parts:[], streaming } | { type:'note' }
  running: false,
  status: "idle", // idle | running | done | limited | error
  sessionId: null,
  mode: "agent", // agent | team:<name>
  mediaPolicy: savedSetting("toonflow-media-policy", "ask"),
  mediaProvider: savedSetting("toonflow-media-provider", "mock"),
  mediaModel: savedSetting("toonflow-media-model", "mock-video-v1"),
  mediaProviders: [],
  usage: null, // 最近一次请求的用量（usage 事件）
  turn: 0,
  // ---- 运行的"活着"信号 ----
  // 没有这几个字段，界面就无法回答"它还在想，还是已经死了"（这正是"不直观"的根源）
  runStartedAt: null, // 本次运行开始（Date.now）
  lastEventAt: null, // 最近一次收到事件（含心跳）
  activity: null, // 当前在做什么（"执行工具 get_canvas" 之类）
  endReason: null, // done | limited | aborted | error | unknown
  resumeInstruction: null,
  stopRequested: false,
  pings: 0, // 收到几次心跳（服务端仍在跑的证据）
  runId: null,
  lastEventId: "0-0",
  reconnectAttempts: 0,
  followupPending: 0,
  pendingQuestions: [],
  productionRun: null, // 短剧生产状态机：阶段、验收问题、重试次数
  compacting: false, // 正在压缩上下文（这个阶段也要在界面上可见）
  nowTick: Date.now(), // 由组件定时刷新，驱动所有"计时器"重绘

  // ---------------- 信息 ----------------
  teams: [],
  tools: [],
  skills: [],
  skillChars: { prompt: 0, body: 0 },
  subagents: [],
  stats: null,
  workspace: "",
});

/* ==================== 画布 ==================== */

export const flowNodes = computed(() =>
  (state.canvas.nodes || []).map((node) => ({
    id: node.id,
    type: "toon",
    position: { x: node.position?.x ?? 0, y: node.position?.y ?? 0 },
    data: {
      node,
      spec: state.canvas.spec?.[node.type] || {},
      changed: state.changedNodeIds.includes(node.id),
      revision: state.canvasRevision,
    },
  }))
);

export const flowEdges = computed(() =>
  (state.canvas.edges || []).map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    sourceHandle: edge.source_handle,
    targetHandle: edge.target_handle,
    selected: edge.id === state.selectedEdgeId,
    // 正交圆角路径比自由贝塞尔曲线更容易追踪，也能显著减少“像跨剧本串线”的错觉。
    type: "smoothstep",
  }))
);

export const selectedNode = computed(
  () => (state.canvas.nodes || []).find((n) => n.id === state.selectedId) || null
);

/** 某个节点的上游依赖（详情面板用）。 */
export const upstreamOfSelected = computed(() => {
  if (!state.selectedId) return [];
  const byId = new Map((state.canvas.nodes || []).map((n) => [n.id, n]));
  return (state.canvas.edges || [])
    .filter((e) => e.target === state.selectedId)
    .map((e) => byId.get(e.source))
    .filter(Boolean);
});

async function loadCanvases() {
  const data = await getJSON("/api/canvases");
  state.canvases = data.canvases || [];
  state.canvasMeta = data.meta || {};
  return state.canvases;
}

async function loadCanvas(name) {
  if (name) state.canvasName = name;
  const target = state.canvasName;
  const data = await getJSON(`/api/canvas?name=${encodeURIComponent(target)}`);
  state.canvas = {
    meta: data.meta || {},
    nodes: data.nodes || [],
    edges: data.edges || [],
    spec: data.node_spec || {},
    topo: data.topo_order || [],
    problems: data.problems || [],
  };
  state.canvasName = data.name;
  state.canvasServerRevision = Number(data.revision || 0);
  if (!state.canvases.includes(data.name)) await loadCanvases();
  if (!state.canvas.nodes.some((n) => n.id === state.selectedId)) state.selectedId = null;
  if (!state.canvas.edges.some((e) => e.id === state.selectedEdgeId)) state.selectedEdgeId = null;
  state.runResult = null;
}

async function refreshAfterCanvasConflict(error) {
  if (error?.code === "canvas_revision_conflict") await loadCanvas();
  return error;
}

function nodeFingerprint(node) {
  return JSON.stringify({ label: node.label, status: node.status, data: node.data, outputs: node.outputs });
}

async function refreshCanvasFromAgent(ev) {
  const before = new Map((state.canvas.nodes || []).map((node) => [node.id, nodeFingerprint(node)]));
  if (ev.canvas_changed && ev.canvas_changed !== state.canvasName) state.canvasName = ev.canvas_changed;
  await loadCanvas(ev.canvas_changed || state.canvasName);

  const explicit = [ev.node_id, ...(Array.isArray(ev.node_ids) ? ev.node_ids : [])].filter(Boolean);
  const derived = (state.canvas.nodes || [])
    .filter((node) => !before.has(node.id) || before.get(node.id) !== nodeFingerprint(node))
    .map((node) => node.id);
  state.changedNodeIds = [...new Set([...explicit, ...derived])];
  state.canvasRevision += 1;
  state.canvasActivity = {
    phase: "done",
    tool: ev.tool,
    summary: `${ev.tool} 已更新画布 · ${state.canvas.nodes.length} 节点 / ${state.canvas.edges.length} 连线`,
    at: Date.now(),
  };
  const revision = state.canvasRevision;
  setTimeout(() => {
    if (state.canvasRevision === revision) state.changedNodeIds = [];
  }, 2200);
  setTimeout(() => {
    if (state.canvasRevision === revision && state.canvasActivity?.phase === "done") state.canvasActivity = null;
  }, 5200);
}

async function moveNode({ id, position }) {
  try {
    const result = await postJSON(`/api/canvas/nodes/${id}/position`, {
      canvas: state.canvasName,
      x: Math.round(position.x),
      y: Math.round(position.y),
      revision: state.canvasServerRevision,
    });
    state.canvasServerRevision = Number(result.revision ?? state.canvasServerRevision);
  } catch (error) {
    throw await refreshAfterCanvasConflict(error);
  }
}

async function updateNode(id, fields) {
  try {
    const result = await postJSON(`/api/canvas/nodes/${encodeURIComponent(id)}`, {
      canvas: state.canvasName,
      ...fields,
      revision: state.canvasServerRevision,
    });
    await loadCanvas();
    state.selectedId = id;
    state.changedNodeIds = [id];
    state.canvasRevision += 1;
    return result;
  } catch (error) {
    throw await refreshAfterCanvasConflict(error);
  }
}

async function connectNodes({ source, sourceHandle, target, targetHandle }) {
  try {
    const result = await postJSON("/api/canvas/connect", {
      canvas: state.canvasName,
      source,
      source_handle: sourceHandle,
      target,
      target_handle: targetHandle,
      revision: state.canvasServerRevision,
    });
    state.canvasServerRevision = Number(result.revision ?? state.canvasServerRevision);
    return result;
  } catch (error) {
    throw await refreshAfterCanvasConflict(error);
  }
}

async function deleteEdge(edgeId) {
  try {
    const result = await postJSON("/api/canvas/edges/delete", {
      canvas: state.canvasName,
      edge_id: edgeId,
      revision: state.canvasServerRevision,
    });
    if (state.selectedEdgeId === edgeId) state.selectedEdgeId = null;
    await loadCanvas();
    return result;
  } catch (error) {
    throw await refreshAfterCanvasConflict(error);
  }
}

async function arrangeCanvas() {
  try {
    const res = await postJSON("/api/canvas/arrange", {
      canvas: state.canvasName,
      revision: state.canvasServerRevision,
    });
    await loadCanvas();
    return res;
  } catch (error) {
    throw await refreshAfterCanvasConflict(error);
  }
}

async function runCanvas({ approved = false } = {}) {
  state.canvasBusy = true;
  try {
    const payload = {
      canvas: state.canvasName,
      approved,
      provider: state.mediaProvider,
      model: state.mediaModel,
      duration: 5,
      resolution: "720p",
      ratio: "16:9",
      generate_audio: false,
      revision: state.canvasServerRevision,
      production_run_id: state.productionRun?.id || null,
    };
    if (approved) {
      const approval = await postJSON("/api/canvas/approvals", payload);
      payload.approval_id = approval.approval_id;
      if (state.generationReview) {
        state.generationReview = { ...state.generationReview, approval };
      }
    }
    const accepted = await postJSON("/api/canvas/run", payload);
    state.mediaJobId = accepted.job_id;
    rememberMediaJob(state.canvasName, accepted.job_id);
    state.generationReview = null;
    const result = await waitForMediaJob(accepted.job_id, accepted.total);
    await prepareCompositionReview().catch(() => {});
    return result;
  } catch (error) {
    throw await refreshAfterCanvasConflict(error);
  } finally {
    state.canvasBusy = false;
  }
}

async function waitForMediaJob(jobId, total = 0) {
  state.mediaJobId = jobId;
  state.canvasBusy = true;
  for (;;) {
    const job = await getJSON(`/api/media/jobs/${encodeURIComponent(jobId)}`);
    if (["queued", "running"].includes(job.status)) {
      await loadCanvas();
      state.runResult = {
        job_id: jobId,
        status: job.status,
        total: job.total || total,
        done: 0,
        results: [],
      };
      await new Promise((resolve) => setTimeout(resolve, 800));
      continue;
    }
    await loadCanvas();
    state.mediaJobId = null;
    rememberMediaJob(state.canvasName, null);
    if (job.status === "succeeded" && job.result) {
      state.runResult = { ...job.result, job_id: jobId, status: job.status };
      await refreshProductionRun();
      return state.runResult;
    }
    state.runResult = {
      job_id: jobId,
      status: job.status,
      total,
      done: job.result?.done || 0,
      results: job.result?.results || [],
      error: job.error || job.result?.error || "媒体任务未完成",
    };
    await refreshProductionRun();
    throw new Error(state.runResult.error);
  }
}

async function restoreMediaJobForCanvas() {
  const jobId = mediaJobBindings[state.canvasName];
  if (!jobId) return null;
  try {
    return await waitForMediaJob(jobId);
  } catch (err) {
    if (String(err?.message || err).includes("不存在")) rememberMediaJob(state.canvasName, null);
    return null;
  } finally {
    state.canvasBusy = false;
  }
}

async function reconcileMediaJob(jobId = state.runResult?.job_id) {
  if (!jobId) throw new Error("没有可恢复的媒体任务");
  state.canvasBusy = true;
  try {
    const queued = await postJSON(`/api/media/jobs/${encodeURIComponent(jobId)}/reconcile`, {});
    rememberMediaJob(state.canvasName, jobId);
    return await waitForMediaJob(jobId, queued.total || state.runResult?.total || 0);
  } finally {
    state.canvasBusy = false;
  }
}

async function exportPrompts() {
  const data = await postJSON("/api/canvas/export", { canvas: state.canvasName });
  state.exportData = data;
  state.exportOpen = true;
  return data;
}

/* ==================== 对话：parts 模型 ==================== */

function feedPush(item) {
  state.feed.push(item);
  return item;
}

/** 当前正在流式的 assistant 消息（parts 容器）。 */
function currentAssistant() {
  for (let i = state.feed.length - 1; i >= 0; i -= 1) {
    const item = state.feed[i];
    if (item.type === "assistant" && item.streaming) return item;
    if (item.type === "user") break;
  }
  return null;
}

/** 新建一条 assistant 消息（每次 message_start 一次）。 */
function beginAssistant(blockId) {
  const previous = currentAssistant();
  if (previous) previous.streaming = false; // 上一段收尾：它的 parts 不再追加
  return feedPush({ type: "assistant", blockId, parts: [], streaming: true, startedAt: Date.now() });
}

/** 子 Agent 块（同一名字可能有多次，取最后一次仍在下游的那个）。 */
function subPartOf(name) {
  const message = currentAssistant() || [...state.feed].reverse().find((i) => i.type === "assistant");
  if (!message) return null;
  for (let i = message.parts.length - 1; i >= 0; i -= 1) {
    const part = message.parts[i];
    if (part.kind === "sub" && part.name === name) return part;
  }
  return null;
}

/** 事件要落到哪个 parts 列表：主 Agent → 消息本身；子 Agent → 子块内部。 */
function partsListFor(ev) {
  if (ev.sub_agent) {
    const sub = subPartOf(ev.sub_agent);
    if (sub) return sub.steps;
  }
  const message = currentAssistant();
  return message ? message.parts : null;
}

function lastPart(list, kind, { running = false } = {}) {
  if (!list || !list.length) return null;
  const part = list[list.length - 1];
  if (part.kind !== kind) return null;
  if (running && part.streaming === false) return null;
  return part;
}

/** 思考块收尾：定耗时、自动收起（Toonflow 的做法：正文/工具一来就折叠）。 */
function finishThinking(list) {
  const part = lastPart(list, "thinking", { running: true });
  if (!part) return;
  part.streaming = false;
  part.duration = (Date.now() - part.startedAt) / 1000;
  part.expanded = false;
}

function pushThinking(list, delta) {
  let part = lastPart(list, "thinking", { running: true });
  if (!part) {
    part = { kind: "thinking", text: "", startedAt: Date.now(), duration: 0, streaming: true, expanded: true };
    list.push(part);
  }
  part.text += delta;
}

function appendText(list, delta) {
  let part = lastPart(list, "text");
  if (!part || part.streaming === false) {
    part = { kind: "text", text: "", streaming: true };
    list.push(part);
  }
  part.streaming = true;
  part.text += delta;
}

function pushTool(list, ev) {
  const part = {
    kind: "tool",
    id: ev.tool_call_id || `${ev.tool}-${list.length}`,
    name: ev.tool,
    args: typeof ev.args === "string" ? ev.args : JSON.stringify(ev.args ?? {}),
    status: "running",
    result: null,
    resultChars: 0,
    startedAt: Date.now(), // 运行中要显示"已经跑了多久"（否则长工具看起来像卡死）
    durationMs: null,
    details: null,
    canvas: null,
    expanded: false,
  };
  list.push(part);
  return part;
}

/** 在整条消息里找"正在运行且与本次结束事件配对"的工具卡。
 *
 * 为什么全局找而不是只找推断出来的那个列表：`sub_agent` 字段有过歧义
 * （既表示"事件来源"，又被委托工具当普通结果字段用），按名字/列表猜会把父卡片
 * 误挂进子块。按 `tool_call_id` 全局配对最稳，同一轮同名工具调两次也不会串。
 */
function findRunningTool(message, ev) {
  if (!message) return null;
  const pools = [message.parts, ...message.parts.filter((p) => p.kind === "sub").map((p) => p.steps)];
  for (const pool of pools) {
    for (let i = pool.length - 1; i >= 0; i -= 1) {
      const part = pool[i];
      if (part.kind !== "tool" || part.status !== "running") continue;
      if (ev.tool_call_id ? part.id === ev.tool_call_id : part.name === ev.tool) return part;
    }
  }
  return null;
}

function finishTool(list, ev) {
  const { tool, isError, result, result_chars, duration_ms, canvas_changed, ...details } = ev;
  const message = currentAssistant() || [...state.feed].reverse().find((i) => i.type === "assistant");
  let part = findRunningTool(message, ev);
  if (!part) {
    const pool = list || (message ? message.parts : null);
    if (!pool) return null;
    part = pushTool(pool, ev);
  }
  part.status = isError ? "error" : "success";
  part.result = result ?? null;
  part.resultChars = result_chars ?? (result ? result.length : 0);
  part.durationMs = duration_ms ?? null;
  part.details = details;
  if (canvas_changed) {
    part.canvas = { name: canvas_changed, nodes: ev.nodes, edges: ev.edges };
  }
  return part;
}

/** 每种事件对应的"当前活动"文案（界面要显示"现在在干嘛"）。 */
const ACTIVITY = {
  agent_start: "准备中…",
  turn_start: "等待模型",
  message_start: "等待模型输出",
  message_update: "生成正文…",
  message_end: "整理结果…",
  tool_execution_end: "等待模型继续",
  sub_agent_start: "子 Agent 执行中…",
  sub_agent_report: "子 Agent 上报…",
  team_step_start: "团队成员执行中…",
  production_stage_started: "开始新的制作阶段…",
  production_stage_validating: "正在验收阶段产物…",
  production_stage_retry: "验收未通过，正在定向返工…",
  compaction_start: "正在压缩上下文…",
};

/** 事件 → feed 的归约（契约来自 core/agent.py 的 _emit）。 */
function handleEvent(ev) {
  // 心跳只用来证明"服务端还活着"，不进 feed、不改状态
  if (ev.type === "ping") {
    state.lastEventAt = Date.now();
    state.pings += 1;
    return;
  }
  state.lastEventAt = Date.now();
  const list = partsListFor(ev);
  if (ev.type.startsWith("production_") && ev.production_run) {
    state.productionRun = ev.production_run;
  }
  switch (ev.type) {
    case "session":
      state.sessionId = ev.session_id;
      rememberSession(state.canvasName, ev.session_id);
      loadSessions().catch(() => {});
      break;
    case "agent_start":
      if (!ev.sub_agent) state.status = "running";
      break;
    case "turn_start":
      state.turn = ev.turn ?? state.turn;
      break;

    // ---- 思考（推理模型才有）----
    case "thinking_update":
      if (list) pushThinking(list, ev.delta || "");
      break;
    case "thinking_end":
      if (list) finishThinking(list);
      break;

    // ---- 正文 ----
    case "message_start":
      if (!ev.sub_agent && ev.role === "assistant") beginAssistant(ev.block_id);
      break;
    case "message_update":
      if (list) {
        finishThinking(list);
        appendText(list, ev.delta || "");
      }
      break;
    case "message_end":
      if (list) {
        const text = lastPart(list, "text");
        if (text) text.streaming = false;
      }
      break;

    // ---- 工具 ----
    case "tool_execution_start":
      if (["identify_story", "add_node", "add_nodes", "update_node", "connect_nodes", "delete_nodes", "set_text", "set_storyboard", "run_canvas", "generate_media"].includes(ev.tool)) {
        const actor = ev.sub_agent ? `子 Agent ${ev.sub_agent}` : "Agent";
        state.canvasActivity = { phase: "running", tool: ev.tool, summary: `${actor} 正在操作画布 · ${ev.tool}`, at: Date.now() };
      }
      if (ev.sub_agent) {
        // 子 Agent 的工具进它自己的 steps（保持父子层次）
        const sub = subPartOf(ev.sub_agent);
        if (sub) {
          finishThinking(sub.steps);
          pushTool(sub.steps, ev);
        }
      } else if (list) {
        finishThinking(list);
        pushTool(list, ev);
      }
      break;
    case "tool_execution_end": {
      const part = list ? finishTool(list, ev) : null;
      // 画布工具改完图会带 canvas_changed —— 前端据此刷新，不需要额外推送通道
      if (part?.canvas) refreshCanvasFromAgent(ev).catch(() => {});
      break;
    }
    case "followup_queued":
      state.followupPending = ev.pending ?? state.followupPending + 1;
      feedPush({ type: "note", title: "已加入 Follow-up", text: ev.text || "补充要求将在当前步骤结束后处理" });
      break;
    case "followup_consumed":
      state.followupPending = ev.pending ?? Math.max(0, state.followupPending - 1);
      feedPush({ type: "user", text: ev.text || "", entryId: ev.entry_id, followup: true });
      break;
    case "user_message_committed": {
      if (ev.sub_agent) break;
      const user = [...state.feed].reverse().find((item) => item.type === "user" && !item.entryId);
      if (user) user.entryId = ev.entry_id;
      break;
    }
    case "assistant_message_committed": {
      if (ev.sub_agent) break;
      const assistant = currentAssistant() || [...state.feed].reverse().find((item) => item.type === "assistant" && !item.entryId);
      if (assistant) {
        assistant.entryId = ev.entry_id;
        assistant.parentId = ev.parent_id;
      }
      break;
    }
    case "input_required": {
      const question = {
        type: "question",
        questionId: ev.question_id,
        title: ev.title,
        question: ev.question,
        options: ev.options || [],
        allowCustom: ev.allow_custom !== false,
        agent: ev.agent || ev.sub_agent || "main",
        status: "pending",
        answer: "",
      };
      state.pendingQuestions = [...state.pendingQuestions.filter((item) => item.questionId !== question.questionId), question];
      feedPush(question);
      state.activity = question.agent === "main" ? "等待你的回答" : `子 Agent ${question.agent} 等待回答`;
      break;
    }
    case "input_resolved": {
      const question = state.pendingQuestions.find((item) => item.questionId === ev.question_id);
      if (question) {
        question.status = "answered";
        question.answer = ev.answer || "";
      }
      state.pendingQuestions = state.pendingQuestions.filter((item) => item.questionId !== ev.question_id);
      state.activity = "已收到回答，继续运行…";
      break;
    }
    case "input_cancelled": {
      const question = state.pendingQuestions.find((item) => item.questionId === ev.question_id);
      if (question) question.status = "cancelled";
      state.pendingQuestions = state.pendingQuestions.filter((item) => item.questionId !== ev.question_id);
      break;
    }

    // ---- 子 Agent ----
    case "sub_agent_start": {
      // 团队模式没有父 Agent 的 message_start；第一位成员启动时要主动
      // 建立承载容器，否则后续子 Agent 的思考、工具和正文事件都会被丢掉。
      const message = currentAssistant() || beginAssistant(`team-${state.runId || Date.now()}`);
      finishThinking(message.parts);
      message.parts.push({
        kind: "sub",
        name: ev.sub_agent,
        status: "running",
        tokens: null,
        reports: [],
        steps: [],
        text: "",
        expanded: true,
      });
      break;
    }
    case "sub_agent_report": {
      const sub = subPartOf(ev.sub_agent);
      if (sub) sub.reports.push(ev.summary);
      break;
    }
    case "sub_agent_usage": {
      const sub = subPartOf(ev.sub_agent);
      if (sub) sub.tokens = ev.total_tokens ?? 0;
      break;
    }
    case "sub_agent_end": {
      const sub = subPartOf(ev.sub_agent);
      if (sub) sub.status = ev.status || (ev.aborted ? "aborted" : "done");
      break;
    }
    case "sub_agent_state": {
      const sub = subPartOf(ev.sub_agent);
      if (sub && ev.status) sub.status = ev.status;
      refreshSubagents().catch(() => {});
      break;
    }

    // ---- 上下文压缩：压缩本身是一次完整的模型请求，可能几十秒，
    //      不在界面上显示就又是一次"静默长停顿"（看起来像卡死）----
    case "compaction_start":
      state.compacting = true;
      feedPush({
        type: "note",
        title: "正在压缩上下文",
        text: `当前约 ${ev.tokens ?? "?"} tokens（${ev.chars ?? "?"} 字），把较早的 ${ev.compacted ?? "?"} 条压成摘要`,
      });
      break;
    case "compaction_fallback":
      state.compacting = true;
      feedPush({
        type: "note",
        title: "摘要服务失败，启用安全降级",
        text: `原因：${ev.reason || "未知"}｜已保留用户事实、工具结果与最近进展`,
      });
      break;
    case "compaction_end":
      state.compacting = false;
      feedPush({ type: "note", title: "上下文已压缩", text: `压缩后约 ${ev.tokens ?? "?"} tokens（第 ${ev.count ?? "?"} 次${ev.fallback ? "，本地降级摘要" : ""}）` });
      break;
    case "compaction_rejected":
      state.compacting = false;
      feedPush({
        type: "note",
        title: "未压缩上下文",
        text: `原因：${ev.reason || "未知"}｜已继续运行（上下文偏大，可能更贵）`,
      });
      break;

    // ---- 用量与团队 ----
    case "usage":
      state.usage = {
        input: ev.input_tokens ?? 0,
        output: ev.output_tokens ?? 0,
        cached: ev.cached_tokens ?? 0,
        total: ev.total_tokens ?? 0,
        label: ev.label || "main",
      };
      break;
    case "team_step_start": {
      const message = currentAssistant() || beginAssistant(`team-${state.runId || Date.now()}`);
      finishThinking(message.parts);
      message.parts.push({
        kind: "team",
        member: ev.member,
        role: ev.role,
        tools: ev.tools || [],
        status: "running",
      });
      break;
    }
    case "team_step_end": {
      const message = currentAssistant() || [...state.feed].reverse().find((item) => item.type === "assistant");
      const part = message?.parts
        ?.slice()
        .reverse()
        .find((item) => item.kind === "team" && item.member === ev.member && item.role === ev.role && item.status === "running");
      if (part) {
        part.status = "done";
        part.chars = ev.chars ?? null;
      }
      break;
    }
    case "production_stage_retry":
      feedPush({
        type: "note",
        title: "阶段验收未通过，已定向返工",
        text: (ev.problems || []).slice(0, 4).join("；") || "正在让对应 Agent 修复已有产物",
      });
      break;
    case "production_stage_failed":
    case "production_run_limited":
      state.status = "limited";
      state.resumeInstruction = "只重试当前未通过阶段，不要重做已经验收成功的阶段。";
      break;
    case "production_waiting_approval":
      state.activity = "创作包已通过验收，等待生成审批";
      break;
    case "team_done": {
      if (ev.stats) state.stats = ev.stats;
      if (ev.result) {
        const message = currentAssistant() || beginAssistant(`team-${Date.now()}`);
        appendText(message.parts, ev.result);
        const text = lastPart(message.parts, "text");
        if (text) text.streaming = false;
      }
      if (ev.usage) {
        feedPush({
          type: "note",
          title: "团队用量",
          text: `增量 ${ev.usage.total_tokens} tokens｜全量 ${ev.usage_all?.total_tokens ?? "-"} tokens`,
        });
      }
      state.endReason = ["done", "completed"].includes(ev.status) ? "done" : (ev.status || "done");
      if (ev.production_run_id && state.productionRun) state.productionRun.id = ev.production_run_id;
      if (state.endReason === "error") state.status = "error";
      settleFeed();
      break;
    }

    // ---- 收尾 ----
    case "agent_end":
      if (!ev.sub_agent && !state.endReason) {
        state.endReason = ev.status === "completed" ? "done" : (ev.status || "done");
        settleFeed();
      }
      break;
    case "done":
      if (ev.stats) state.stats = ev.stats;
      if (!state.endReason || ev.status === "error") {
        state.endReason = ev.status === "completed" || ev.status === "done" ? "done" : (ev.status || "done");
      }
      settleFeed();
      refreshSubagents().catch(() => {});
      break;
    case "aborted":
      state.status = "idle";
      state.endReason = "aborted";
      settleFeed();
      if (!state.stopRequested) feedPush({ type: "note", title: "已中止", text: "运行被取消" });
      break;
    case "limited":
      state.status = "limited";
      state.endReason = "limited";
      settleFeed();
      feedPush({
        type: "note",
        title: ev.reason === "max_turns" ? "本次制作已暂停" : "输出达到长度限制",
        text: ev.message || "当前结果可能不完整，进度已保存，可以继续制作",
      });
      break;
    case "checkpoint":
      state.resumeInstruction = ev.resume_instruction || null;
      break;
    case "error":
      state.status = "error";
      state.endReason = "error";
      settleFeed();
      feedPush({ type: "note", title: "出错", text: String(ev.message || ev.error || "未知错误") });
      break;
    default:
      break;
  }

  if (ACTIVITY[ev.type]) state.activity = ACTIVITY[ev.type];
  if (ev.type === "tool_execution_start" && !ev.sub_agent) state.activity = `执行工具 ${ev.tool}`;
}

/** 收尾：把仍在"运行中"的东西标成中断，并给最后一条消息盖上"结束章"。
 *
 * 两件事缺一不可：
 *   · 不标中断 → 界面上会有工具卡永远显示"调用中"（看起来像没完成）
 *   · 不盖结束章 → 用户看不出这次运行到底结束了没有、以什么方式结束
 */
function settleFeed() {
  for (const item of state.feed) {
    if (item.type !== "assistant") continue;
    item.streaming = false;
    for (const part of item.parts) {
      if (part.kind === "thinking" && part.streaming) {
        part.streaming = false;
        part.duration = (Date.now() - part.startedAt) / 1000;
      }
      if (part.kind === "text") part.streaming = false;
      if (part.kind === "tool" && part.status === "running") part.status = "interrupted";
      if (part.kind === "sub") {
        if (part.status === "running") part.status = "done";
        for (const step of part.steps) if (step.status === "running") step.status = "interrupted";
      }
    }
  }

  state.compacting = false;
  const last = [...state.feed].reverse().find((item) => item.type === "assistant");
  if (last) {
    last.ended = {
      reason: state.endReason || "unknown",
      elapsedMs: state.runStartedAt ? Date.now() - state.runStartedAt : null,
      usage: state.usage,
      pings: state.pings,
    };
  }
}

async function send(prompt, options = {}) {
  const text = String(prompt || "").trim();
  if (!text) return;
  if (state.running) return queueFollowup(text);

  const canvasRevisionBefore = state.canvasRevision;
  state.feed.push({ type: "user", text });
  state.running = true;
  state.status = "running";
  state.usage = null;
  state.runStartedAt = Date.now();
  state.lastEventAt = Date.now();
  state.activity = "连接中…";
  state.endReason = null;
  state.resumeInstruction = null;
  state.stopRequested = false;
  state.pings = 0;
  state.runId = null;
  state.lastEventId = "0-0";
  state.reconnectAttempts = 0;
  state.followupPending = 0;

  const runController = new AbortController();
  activeRunController = runController;

  const teamName = state.mode.startsWith("team:") ? state.mode.slice(5) : null;
  const path = teamName ? "/api/team" : "/api/agent";
  const body = teamName
    ? {
        task: text,
        team: teamName,
        session_id: state.sessionId,
        canvas: state.canvasName,
        production_run_id: options.productionRunId || null,
      }
    : { prompt: text, session_id: state.sessionId, canvas: state.canvasName, media_policy: state.mediaPolicy };

  await streamNDJSON(
    path,
    body,
    handleEvent,
    (err) => {
      state.status = "error";
      feedPush({ type: "note", title: "请求失败", text: err.message });
    },
    runController.signal,
    {
      onConnection: ({ sessionId, runId }) => {
        if (sessionId) {
          state.sessionId = sessionId;
          rememberSession(state.canvasName, sessionId);
        }
        if (runId) state.runId = runId;
      },
      onCursor: (eventId) => { state.lastEventId = eventId; },
      onReconnect: ({ attempt }) => {
        state.reconnectAttempts = attempt;
        state.activity = `连接中断，正在第 ${attempt} 次续传…`;
      },
    },
  );

  if (activeRunController === runController) activeRunController = null;

  if (!state.endReason && !state.stopRequested) {
    // 流结束了却没有终态事件：服务端明明一定会发 done，真出现就是异常
    //（进程被杀 / 连接断 / 代理超时）。必须**明确告诉用户**，
    // 而不是留一个永远"看起来还在跑"的界面。
    state.endReason = "unknown";
    feedPush({
      type: "note",
      title: "运行未正常结束",
      text: "没有收到完成事件（连接断开或服务端中断）。未完成的工具已标记为中断，可重新发起。",
    });
  }

  settleFeed();
  state.running = false;
  state.activity = null;
  if (state.status !== "error") {
    state.status = state.endReason === "limited"
      ? "limited"
      : (["done", "aborted", "waiting_approval"].includes(state.endReason) ? "idle" : "error");
  }
  if (state.sessionId) await Promise.all([refreshStats(), refreshSubagents()]);
  if (
    state.mediaPolicy === "auto_review"
    && state.canvasRevision > canvasRevisionBefore
    && ["done", "waiting_approval"].includes(state.endReason)
  ) {
    await prepareGenerationReview().catch(() => {});
  }
}

async function resumeProduction() {
  const run = state.productionRun;
  if (!run?.id || state.running) return null;
  state.mode = `team:${run.team || "dramaTeam"}`;
  return send(run.user_request || "继续完成当前短剧生产任务", { productionRunId: run.id });
}

async function queueFollowup(text) {
  const value = String(text || "").trim();
  if (!value || !state.running || !state.sessionId) return null;
  return postJSON("/api/agent/followup", { session_id: state.sessionId, text: value });
}

async function answerQuestion(questionId, answer) {
  if (!state.sessionId) throw new Error("当前没有会话");
  return postJSON(
    `/api/sessions/${encodeURIComponent(state.sessionId)}/questions/${encodeURIComponent(questionId)}/answer`,
    { answer },
  );
}

async function createNode({ type, label, position, data = {} }) {
  try {
    const result = await postJSON("/api/canvas/nodes", {
      canvas: state.canvasName,
      type,
      label,
      x: Math.round(position.x),
      y: Math.round(position.y),
      data,
      revision: state.canvasServerRevision,
    });
    await loadCanvas();
    state.selectedId = result.node_id;
    state.changedNodeIds = [result.node_id];
    state.canvasRevision += 1;
    return result;
  } catch (error) {
    throw await refreshAfterCanvasConflict(error);
  }
}

async function uploadMedia(file, kind, nodeId = "", position = null) {
  state.mediaUploading = true;
  try {
    const query = new URLSearchParams({
      canvas: state.canvasName,
      kind,
      filename: file.name,
    });
    if (nodeId) query.set("node_id", nodeId);
    query.set("revision", String(state.canvasServerRevision));
    if (position && !nodeId) {
      query.set("x", String(Math.round(position.x)));
      query.set("y", String(Math.round(position.y)));
    }
    const result = await postFile(`/api/canvas/upload-media?${query}`, file);
    await loadCanvas();
    state.selectedId = result.node_id;
    state.canvasRevision += 1;
    state.changedNodeIds = [result.node_id];
    return result;
  } catch (error) {
    throw await refreshAfterCanvasConflict(error);
  } finally {
    state.mediaUploading = false;
  }
}

async function stop() {
  if (!state.running || state.stopRequested) return;
  state.stopRequested = true;
  state.activity = "正在停止…";
  state.endReason = "aborted";
  feedPush({ type: "note", title: "正在停止", text: "已请求终止模型、工具和子 Agent" });

  try {
    if (state.sessionId) await postJSON("/api/agent/cancel", { session_id: state.sessionId });
  } catch {
    // 请求可能尚未拿到 session，或当前是 ?demo=live；关闭流仍能触发服务端断开取消。
  } finally {
    activeRunController?.abort();
  }

  // 演示态没有真实的 send() 在等待流结束，需要在这里直接收尾。
  if (!activeRunController) {
    settleFeed();
    state.running = false;
    state.status = "idle";
    state.activity = null;
  }
}

async function prepareGenerationReview() {
  const plan = await getJSON(`/api/canvas/generation-plan?name=${encodeURIComponent(state.canvasName)}`);
  state.generationReview = plan.total ? {
    ...plan,
    provider: state.mediaProvider,
    model: state.mediaModel,
    requestedAt: Date.now(),
  } : null;
  return plan;
}

async function approveGeneration() {
  return runCanvas({ approved: true });
}

async function prepareCompositionReview() {
  const plan = await getJSON(`/api/canvas/composition-plan?name=${encodeURIComponent(state.canvasName)}`);
  state.compositionReview = plan.total ? { ...plan, requestedAt: Date.now() } : null;
  return plan;
}

async function composeCanvas() {
  state.canvasBusy = true;
  try {
    const payload = {
      canvas: state.canvasName,
      approved: true,
      revision: state.canvasServerRevision,
      production_run_id: state.productionRun?.id || null,
    };
    const approval = await postJSON("/api/canvas/composition-approvals", payload);
    payload.approval_id = approval.approval_id;
    const accepted = await postJSON("/api/canvas/compose", payload);
    state.mediaJobId = accepted.job_id;
    rememberMediaJob(state.canvasName, accepted.job_id);
    state.compositionReview = null;
    return await waitForMediaJob(accepted.job_id, accepted.total);
  } catch (error) {
    throw await refreshAfterCanvasConflict(error);
  } finally {
    state.canvasBusy = false;
  }
}

function dismissGenerationReview() {
  state.generationReview = null;
}

/** 清空对话区（仅前端视图，不动会话历史）。 */
function clearFeed() {
  state.feed = [];
}

async function loadSessions(canvas = state.canvasName) {
  state.sessionLoading = true;
  try {
    const data = await getJSON(`/api/sessions?canvas=${encodeURIComponent(canvas)}`);
    state.sessions = data.sessions || [];
    return state.sessions;
  } finally {
    state.sessionLoading = false;
  }
}

async function openSession(sessionId) {
  if (!sessionId || state.running) return null;
  state.sessionLoading = true;
  try {
    await postJSON(`/api/sessions/${encodeURIComponent(sessionId)}/bind`, { canvas: state.canvasName });
    const data = await getJSON(`/api/sessions/${encodeURIComponent(sessionId)}`);
    state.sessionId = data.id;
    state.feed = data.feed || [];
    state.stats = data.stats || null;
    state.subagents = data.sub_agents || [];
    state.pendingQuestions = (data.pending_questions || []).map((item) => ({
      type: "question",
      questionId: item.question_id,
      title: item.title,
      question: item.question,
      options: item.options || [],
      allowCustom: item.allow_custom !== false,
      agent: item.agent || "main",
      status: "pending",
      answer: "",
    }));
    state.feed.push(...state.pendingQuestions);
    state.productionRun = data.production_run || null;
    const lastRun = data.last_run || null;
    const productionStatus = state.productionRun?.status || null;
    const limited = productionStatus === "limited" || lastRun?.status === "limited";
    state.status = limited ? "limited" : "idle";
    state.endReason = limited ? "limited" : (productionStatus === "waiting_approval" ? "waiting_approval" : null);
    state.resumeInstruction = limited
      ? (lastRun?.resume_instruction || "只重试当前未通过阶段，不要重做已经验收成功的阶段。")
      : null;
    state.usage = null;
    rememberSession(state.canvasName, data.id);
    return data;
  } finally {
    state.sessionLoading = false;
  }
}

async function renameSession(title) {
  if (!state.sessionId || state.running) return null;
  const data = await postJSON(`/api/sessions/${encodeURIComponent(state.sessionId)}/rename`, { title });
  await loadSessions();
  return data;
}

async function deleteSession() {
  if (!state.sessionId || state.running) return null;
  const id = state.sessionId;
  const data = await postJSON(`/api/sessions/${encodeURIComponent(id)}/delete`, {});
  forgetSession(id);
  state.sessionId = null;
  state.feed = [];
  state.stats = null;
  state.subagents = [];
  state.pendingQuestions = [];
  state.productionRun = null;
  await loadSessions();
  return data;
}

async function mutateMessage(entryId, action) {
  if (!state.sessionId || state.running || !entryId) return null;
  const data = await postJSON(
    `/api/sessions/${encodeURIComponent(state.sessionId)}/messages/${encodeURIComponent(entryId)}`,
    { action },
  );
  state.feed = data.feed || [];
  state.stats = data.stats || state.stats;
  if (action === "resend" && data.prompt) await send(data.prompt);
  return data;
}

async function createSession(title = "") {
  if (state.running) return null;
  const data = await postJSON("/api/sessions", { canvas: state.canvasName, title });
  await loadSessions();
  await openSession(data.id);
  return data;
}

async function restoreSessionForCanvas() {
  await loadSessions();
  const remembered = sessionBindings[state.canvasName];
  const exact = state.sessions.find((item) => item.id === remembered)
    || state.sessions.find((item) => item.canvas === state.canvasName);
  if (exact) return openSession(exact.id);
  state.sessionId = null;
  state.feed = [];
  state.stats = null;
  state.subagents = [];
  state.pendingQuestions = [];
  state.productionRun = null;
  return null;
}

async function switchCanvas(name) {
  if (state.running || state.canvasBusy) return;
  await loadCanvas(name);
  await restoreSessionForCanvas();
  restoreMediaJobForCanvas();
}

/* ==================== 信息 ==================== */

async function refreshStats() {
  if (!state.sessionId) return;
  try {
    state.stats = await getJSON(`/api/sessions/${state.sessionId}/stats`);
  } catch {
    /* 会话刚建、统计还没落盘时忽略 */
  }
}

async function refreshProductionRun() {
  if (!state.sessionId) return null;
  try {
    const data = await getJSON(`/api/sessions/${encodeURIComponent(state.sessionId)}/production-run`);
    state.productionRun = data.production_run || null;
    if (state.productionRun?.status === "limited") {
      state.status = "limited";
      state.endReason = "limited";
    }
    return state.productionRun;
  } catch {
    return null;
  }
}

async function refreshSubagents() {
  if (!state.sessionId) return;
  try {
    const data = await getJSON(`/api/sessions/${state.sessionId}/subagents`);
    state.subagents = data.sub_agents || [];
  } catch {
    /* 同上 */
  }
}

async function loadTools() {
  const data = await getJSON("/api/tools");
  state.tools = data.tools || [];
  state.workspace = String(data.workspace || "").split(/[\\/]/).filter(Boolean).pop() || "";
}

async function loadMediaProviders() {
  const data = await getJSON("/api/media/providers");
  state.mediaProviders = data.providers || [];
  const provider = state.mediaProviders.find((item) => item.id === state.mediaProvider && item.configured)
    || state.mediaProviders.find((item) => item.id === "mock");
  if (provider) state.mediaProvider = provider.id;
  const models = provider?.models || [];
  if (!models.some((item) => item.id === state.mediaModel)) {
    state.mediaModel = models.find((item) => item.kind === "video")?.id || models[0]?.id || "";
  }
}

function setMediaPolicy(value) {
  state.mediaPolicy = value;
  persistSetting("toonflow-media-policy", value);
}

function setMediaProvider(value) {
  state.mediaProvider = value;
  const provider = state.mediaProviders.find((item) => item.id === value);
  state.mediaModel = provider?.models?.find((item) => item.kind === "video")?.id || provider?.models?.[0]?.id || "";
  persistSetting("toonflow-media-provider", state.mediaProvider);
  persistSetting("toonflow-media-model", state.mediaModel);
}

function setMediaModel(value) {
  state.mediaModel = value;
  persistSetting("toonflow-media-model", value);
}

async function loadSkills() {
  const data = await getJSON("/api/skills");
  state.skills = data.skills || [];
  state.skillChars = { prompt: data.prompt_chars || 0, body: data.body_chars || 0 };
}

async function loadTeams() {
  try {
    const data = await getJSON("/api/teams");
    state.teams = data.teams || [];
  } catch {
    state.teams = [];
  }
}

/* ==================== 启动 ==================== */

export async function bootstrap() {
  await Promise.all([loadTools(), loadSkills(), loadTeams(), loadMediaProviders()]);
  await loadCanvases();
  // 默认视图就是画布，所以必须主动拉一次数据（不是等切换才加载）
  await loadCanvas(state.canvases[0] || "main");
  await restoreSessionForCanvas();
  restoreMediaJobForCanvas();
}

export const actions = {
  loadCanvases,
  loadCanvas,
  moveNode,
  updateNode,
  connectNodes,
  deleteEdge,
  createNode,
  uploadMedia,
  arrangeCanvas,
  runCanvas,
  reconcileMediaJob,
  prepareGenerationReview,
  prepareCompositionReview,
  composeCanvas,
  approveGeneration,
  dismissGenerationReview,
  exportPrompts,
  send,
  queueFollowup,
  answerQuestion,
  resumeProduction,
  stop,
  clearFeed,
  loadSessions,
  openSession,
  createSession,
  renameSession,
  deleteSession,
  mutateMessage,
  restoreSessionForCanvas,
  switchCanvas,
  refreshStats,
  refreshProductionRun,
  refreshSubagents,
  loadTools,
  loadSkills,
  loadTeams,
  loadMediaProviders,
  setMediaPolicy,
  setMediaProvider,
  setMediaModel,
  switchView(name) {
    if (name === "canvas") loadCanvas().catch(() => {});
  },
};
