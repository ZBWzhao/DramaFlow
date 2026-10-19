<script setup>
/* B 版画布：把 demo 的引擎（自己算平移/缩放、卡片外标题、卡下提示词框、收束连线）
 * 接到**真实数据**上。旧的 CanvasPanel（@vue-flow）保留，URL 带 ?legacy=1 时回退。
 *
 * 数据映射：state.canvas.nodes/edges → demo 节点形状（x/y/title/plain/img/video/attempts/prompt…）
 * 交互映射：拖动卡片 → actions.moveNode；选中 → state.selectedId；提示词 → actions.savePrompt；
 *           生成 → actions.runCanvas({only:[这张卡]})；上传 → actions.uploadMedia。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { actions, state } from "../store.js";
import { assetUrl, mediaOf } from "../utils.js";
import { CARD_W, cardHeight, clamp, edgePath, nodeAnchor } from "./geometry.js";
import { dragMoves } from "./positions.js";
import NodeCard from "./NodeCard.vue";
import HeroPage from "./HeroPage.vue";
import DockBar from "./DockBar.vue";
import SidebarPanel from "./SidebarPanel.vue";
import NodeStudio from "../components/NodeStudio.vue";
import GenerationHistoryDrawer from "../components/GenerationHistoryDrawer.vue";
import ContextMenu from "./ContextMenu.vue";
import NodeSearch from "./NodeSearch.vue";
import VideoLightbox from "./VideoLightbox.vue";

const TYPE_LABEL = {
  asset: "资产", image: "图片", image_gen: "图片生成",
  video: "视频", video_gen: "视频生成",
  audio: "音频", tts_gen: "旁白生成",
  text: "文本", prompt: "片段提示词", storyboard: "分镜", timeline: "时间线 / 成片",
};
const TYPE_GLYPH = {
  asset: "◈", image: "▣", image_gen: "▣", video: "▷", video_gen: "▷",
  audio: "♪", tts_gen: "♪", text: "¶", prompt: "✎", storyboard: "▤", timeline: "⧗",
};
const ATTEMPTS = {
  image_gen: ["生成"], video_gen: ["生成"], tts_gen: ["生成"],
  asset: ["上传"], image: ["上传"], video: ["上传"], audio: ["上传"],
  prompt: ["自己编写"], text: ["自己编写"], storyboard: ["自己编写"],
  timeline: ["合成成片"],
};
const TEXT_TYPES = new Set(["prompt", "text", "storyboard"]);
const GEN_TYPES = new Set(["image_gen", "video_gen", "tts_gen"]);

const view = ref({ x: 0, y: 0, z: 0.85 });
const tab = ref("canvas");
const keyword = ref("");
const toast = ref("");
const stageRef = ref(null);
const pan = ref(null);
const historyOpen = ref(false);
const studioOpen = ref(false);
const pendingUpload = ref(null);
const uploadInput = ref(null);
/* 多选：框选出来的 id 列表。state.selectedId 只在「恰好选中一张」时同步，
 * 这样卡下提示词框/右侧详情/历史这些「单卡」入口不会被多选搅乱。 */
const selection = ref([]);
const marquee = ref(null);      // { sx, sy, cx, cy }（stage 内坐标）
const edgeDraft = ref(null);    // { sourceId, from:{x,y}, to:{x,y} }
const dragPreview = ref(null);  // 多选拖动时主卡位移 { id, dx, dy }，供同组卡片即时跟手
const spaceDown = ref(false);
let suppressClick = false;
const menu = ref(null);          // 右键菜单 { x, y, title, items }
const searchOpen = ref(false);   // Ctrl/Cmd + K 节点搜索
const videoOpen = ref(null);     // 视频大图：{ src, title, badge, duration, download, filename }
const flashId = ref("");         // 刚从左侧列表/搜索跳过来的卡，闪一下
let flashTimer = 0;

/* 生成状态（M1/M2）：服务端状态要等第一次轮询才回来，本地 pendingGen 让卡片
 * 在点下"生成"的瞬间就有反馈；doneFlash 让刚更新的卡亮一下，提示"这张变了"。
 * 失败不靠本地猜：Worker 会写 node.status=error，卡片据此给"重试"。 */
const pendingGen = ref(new Set());
const doneFlash = ref(new Set());
let doneFlashTimer = 0;

function markPending(ids, on) {
  const next = new Set(pendingGen.value);
  ids.forEach((id) => { if (on) next.add(id); else next.delete(id); });
  pendingGen.value = next;
}

function flashDone(ids) {
  doneFlash.value = new Set(ids);
  window.clearTimeout(doneFlashTimer);
  doneFlashTimer = window.setTimeout(() => { doneFlash.value = new Set(); }, 1500);
}

/* 语义缩放：远处只留画面，近处才铺开标题/尝试/正文，避免大画布上一屏几百个文字块 */
const lodClass = computed(() => {
  const z = view.value.z;
  if (z < 0.5) return "lod-far";
  if (z < 0.8) return "lod-mid";
  return "lod-near";
});

/* 视口剔除：只渲染屏幕里（外扩 360px 缓冲）的卡，大画布不至于一次性挂上千个节点 */
const visibleNodes = computed(() => {
  const rect = stageRect();
  const pad = 360;
  const left = (-view.value.x - pad) / view.value.z;
  const top = (-view.value.y - pad) / view.value.z;
  const right = (rect.width - view.value.x + pad) / view.value.z;
  const bottom = (rect.height - view.value.y + pad) / view.value.z;
  return boardNodes.value.filter((node) => {
    const h = cardHeight(node);
    return node.x + CARD_W > left && node.x < right && node.y + h > top && node.y < bottom;
  });
});

const visibleIds = computed(() => new Set(visibleNodes.value.map((node) => node.id)));
const visibleEdges = computed(() => (state.canvas.edges || []).filter((edge) => {
  const source = edge.source || edge.from;
  const target = edge.target || edge.to;
  return visibleIds.value.has(source) && visibleIds.value.has(target);
}));
const selectedIds = computed(() => new Set(selection.value));
let toastTimer = 0;
let fitTimer = 0;

function showToast(text) {
  toast.value = text;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { toast.value = ""; }, 2200);
}

function bodyText(node) {
  if (typeof node.content === "string" && node.content.trim()) return node.content;
  if (typeof node.data?.prompt === "string" && node.data.prompt.trim()) return node.data.prompt;
  const values = Object.values(node.outputs || {});
  return values.find((item) => item?.data_type === "STRING" && typeof item.value === "string")?.value || "";
}

const boardNodes = computed(() => (state.canvas.nodes || []).map((node) => {
  const media = mediaOf(node);
  const type = node.type;
  const human = node.data?.rendered_by === "human";
  const isVideo = media?.port === "video";
  // 视频卡上显示的时长必须来自这一版 take 的真实元数据；没有就不显示，不编 00:04
  const takes = Array.isArray(node.data?.takes) ? node.data.takes : [];
  const currentTake = takes.find((item) => item.take_id === node.data?.current_take_id) || takes[takes.length - 1] || null;
  const durationSeconds = isVideo ? Number(currentTake?.duration_s || node.data?.duration_s || 0) || 0 : 0;
  return {
    id: node.id,
    x: node.position?.x ?? 0,
    y: node.position?.y ?? 0,
    title: node.label || node.id,
    plain: TEXT_TYPES.has(type) && !media,
    glyph: TYPE_GLYPH[type] || "▣",
    tag: TYPE_LABEL[type] || type,
    dims: node.data?.width && node.data?.height ? `${node.data.width} × ${node.data.height}` : (TYPE_LABEL[type] || type),
    img: media && !isVideo ? assetUrl(media.path) : "",
    src: media ? assetUrl(media.path) : "",
    video: isVideo,
    durationSeconds,
    badge: media ? (media.mock ? "mock" : human ? "手传" : "AI生成") : "",
    // 生成状态：卡片遮罩 / 失败重试 / 完成高亮都读这几个字段（只读，不改数据）
    status: node.status || "",
    busy: pendingGen.value.has(node.id) || ["queued", "generating"].includes(node.status),
    busyLabel: node.status === "queued" ? "排队中…" : node.status === "generating" ? "生成中…" : "已提交…",
    failed: node.status === "error",
    justDone: doneFlash.value.has(node.id),
    attempts: ATTEMPTS[type] || null,
    prompt: bodyText(node),
    model: GEN_TYPES.has(type) ? (state.mediaModel || "默认模型") : TEXT_TYPES.has(type) ? "文本" : "素材",
    size: GEN_TYPES.has(type) ? "16:9 · 标准画质 · 2K · 1张" : TEXT_TYPES.has(type) ? "正文" : "原图",
    textCount: bodyText(node).length,
    canGenerate: GEN_TYPES.has(type),
    canUpload: ["asset", "image", "video", "audio"].includes(type),
    historyCount: (node.data?.takes || []).length,
    flash: node.id === flashId.value,
    raw: node,
  };
}));

const byId = computed(() => Object.fromEntries(boardNodes.value.map((node) => [node.id, node])));

/* 只给「两端都在视口里」的连线画路径：大画布上连线数量往往是节点数的 1.5 倍，
 * 全画会把 svg 撑爆，而且屏幕外的线本来也看不见。 */
const edges = computed(() => visibleEdges.value
  .map((edge) => {
    const from = byId.value[edge.source ?? edge.from];
    const to = byId.value[edge.target ?? edge.to];
    if (!from || !to) return null;
    return edgePath(nodeAnchor(from, "right"), nodeAnchor(to, "left"));
  })
  .filter(Boolean));

const sideItems = computed(() => boardNodes.value.map((node) => ({
  id: node.id, title: node.title, tag: node.tag, icon: node.glyph,
})));

const selectedNode = computed(() => (state.canvas.nodes || []).find((node) => node.id === state.selectedId) || null);
const selectedSpec = computed(() => state.canvas.spec?.[selectedNode.value?.type] || {});
const upstreamOfSelected = computed(() => {
  const target = state.selectedId;
  if (!target) return [];
  const ids = new Set(
    (state.canvas.edges || []).filter((edge) => (edge.target ?? edge.to) === target).map((edge) => edge.source ?? edge.from),
  );
  return (state.canvas.nodes || []).filter((node) => ids.has(node.id));
});
const selectedOrder = computed(() => {
  const index = (state.canvas.nodes || []).findIndex((node) => node.id === state.selectedId);
  return { at: index + 1, total: (state.canvas.nodes || []).length };
});

const marqueeStyle = computed(() => {
  const box = marquee.value;
  if (!box) return {};
  return {
    left: `${Math.min(box.sx, box.cx)}px`,
    top: `${Math.min(box.sy, box.cy)}px`,
    width: `${Math.abs(box.cx - box.sx)}px`,
    height: `${Math.abs(box.cy - box.sy)}px`,
  };
});

function stageRect() {
  return stageRef.value?.getBoundingClientRect() || { left: 0, top: 0, width: 1200, height: 800 };
}

function fit() {
  const rect = stageRect();
  const nodes = boardNodes.value;
  if (!nodes.length || !rect.width) return;
  const maxX = Math.max(...nodes.map((node) => node.x + CARD_W));
  const maxY = Math.max(...nodes.map((node) => node.y + cardHeight(node)));
  const z = clamp(Math.min(rect.width / (maxX + 200), rect.height / (maxY + 200)), 0.2, 1.1);
  view.value = { x: (rect.width - maxX * z) / 2, y: (rect.height - maxY * z) / 2, z };
}

function focusFirst() {
  const rect = stageRect();
  const nodes = boardNodes.value;
  if (!nodes.length || !rect.width) return;
  const first = nodes.find((node) => node.img) || nodes[0];
  const z = clamp(0.95, 0.4, 1.1);
  view.value = {
    z,
    x: rect.width / 2 - (first.x + CARD_W / 2) * z,
    y: rect.height / 3 - (first.y + 110) * z,
  };
}

function scheduleFocus() {
  nextTick(() => {
    window.clearTimeout(fitTimer);
    fitTimer = window.setTimeout(() => { fit(); }, 120);
    fitTimer = window.setTimeout(() => { focusFirst(); }, 420);
  });
}

onMounted(() => {
  requestAnimationFrame(fit);
  scheduleFocus();
});
onBeforeUnmount(() => {
  window.clearTimeout(fitTimer);
  window.clearTimeout(doneFlashTimer);
  window.removeEventListener("keydown", onKeyDown);
  window.removeEventListener("keyup", onKeyUp);
  window.removeEventListener("pointermove", onEdgeMove);
  window.clearTimeout(flashTimer);
});

// 只在「换了画布 / 画布第一次拿到节点」时自动取景；节点状态变化、轮询刷新、
// 增删节点都不许动视口——否则生成中每 800ms 一次 loadCanvas 会把镜头拽走，
// 用户刚点过的那张卡直接飞出视野（M1 之前就存在的跳视口问题）。
let lastFocusKey = "";
watch(
  () => [state.canvasName, (state.canvas.nodes || []).length],
  ([name, count]) => {
    if (!name) return;
    // 用「名字 + 有没有节点」做键：同一画布内数量变化不算换画布，不重新取景。
    const key = `${name}:${count > 0 ? "ready" : "empty"}`;
    if (key === lastFocusKey) return;
    lastFocusKey = key;
    if (count > 0) scheduleFocus();
  },
  { immediate: true },
);

function toFlow(clientX, clientY) {
  const rect = stageRect();
  return {
    x: (clientX - rect.left - view.value.x) / view.value.z,
    y: (clientY - rect.top - view.value.y) / view.value.z,
  };
}

function onPointerDown(event) {
  if (event.target.closest?.(".node")) return;
  // 中键 / 按住空格 = 平移；左键拖空白 = 框选（和主流画布一致）
  if (event.button === 1 || spaceDown.value) {
    pan.value = { sx: event.clientX, sy: event.clientY, vx: view.value.x, vy: view.value.y };
    return;
  }
  if (event.button !== 0) return;
  const rect = stageRect();
  marquee.value = {
    sx: event.clientX - rect.left,
    sy: event.clientY - rect.top,
    cx: event.clientX - rect.left,
    cy: event.clientY - rect.top,
  };
}
function onPointerMove(event) {
  if (pan.value) {
    const { sx, sy, vx, vy } = pan.value;
    view.value = { ...view.value, x: vx + (event.clientX - sx), y: vy + (event.clientY - sy) };
    return;
  }
  if (marquee.value) {
    const rect = stageRect();
    marquee.value = {
      ...marquee.value,
      cx: event.clientX - rect.left,
      cy: event.clientY - rect.top,
    };
  }
}
function onPointerUp() {
  pan.value = null;
  const box = marquee.value;
  marquee.value = null;
  if (!box) return;
  const w = box.cx - box.sx;
  const h = box.cy - box.sy;
  if (Math.abs(w) < 4 && Math.abs(h) < 4) return;   // 只是点了一下，交给 click 清空
  suppressClick = true;
  selectInBox(box);
}

function selectInBox(box) {
  const x1 = Math.min(box.sx, box.cx);
  const x2 = Math.max(box.sx, box.cx);
  const y1 = Math.min(box.sy, box.cy);
  const y2 = Math.max(box.sy, box.cy);
  const z = view.value.z;
  const world = {
    x1: (x1 - view.value.x) / z,
    y1: (y1 - view.value.y) / z,
    x2: (x2 - view.value.x) / z,
    y2: (y2 - view.value.y) / z,
  };
  const hit = (boardNodes.value || []).filter((node) => {
    const nx2 = node.x + CARD_W;
    const ny2 = node.y + cardHeight(node);
    return nx2 >= world.x1 && node.x <= world.x2 && ny2 >= world.y1 && node.y <= world.y2;
  }).map((node) => node.id);
  selection.value = hit;
  state.selectedId = hit.length === 1 ? hit[0] : null;
  if (hit.length) showToast(`已选中 ${hit.length} 张卡`);
}

function onWheel(event) {
  const rect = stageRect();
  const px = event.clientX - rect.left;
  const py = event.clientY - rect.top;
  const z = clamp(view.value.z * Math.exp(-event.deltaY * 0.0016), 0.15, 2.2);
  const k = z / view.value.z;
  view.value = { z, x: px - (px - view.value.x) * k, y: py - (py - view.value.y) * k };
}

function zoomBy(factor) {
  const rect = stageRect();
  const px = rect.width / 2;
  const py = rect.height / 2;
  const z = clamp(view.value.z * factor, 0.15, 2.2);
  const k = z / view.value.z;
  view.value = { z, x: px - (px - view.value.x) * k, y: py - (py - view.value.y) * k };
}

function onStageClick(event) {
  if (event.target.closest?.(".node")) return;
  // 框选结束时浏览器还会补一个 click，不能让它把刚选中的东西清掉
  if (suppressClick) {
    suppressClick = false;
    return;
  }
  selection.value = [];
  state.selectedId = null;
}

function onStageDoubleClick(event) {
  if (event.target.closest?.(".node")) return;
  addNode(event.clientX, event.clientY);
}

const ADDABLE = {
  image_gen: "图片生成",
  video_gen: "视频生成",
  tts_gen: "音频生成",
  prompt: "提示词",
  text: "文本",
  timeline: "时间线",
};

async function addNode(clientX, clientY, type = "image_gen") {
  const rect = stageRect();
  const point = clientX
    ? toFlow(clientX, clientY)
    : { x: (rect.width / 2 - view.value.x) / view.value.z, y: (rect.height / 2 - view.value.y) / view.value.z };
  try {
    const created = await actions.createNode({
      type,
      label: ADDABLE[type] || "新节点",
      position: { x: Math.round(point.x - CARD_W / 2), y: Math.round(point.y - 100) },
    });
    if (created?.node_id || created?.id) state.selectedId = created.node_id || created.id;
    showToast(`已新建一张${ADDABLE[type] || "节点"}卡`);
  } catch (err) {
    ElMessage.error(`新建失败：${err.message || err}`);
  }
}

/* 左侧列表点一项 / 搜索结果回车 → 把那张卡挪到视口中心、选中它、并闪一下。
 * 缩放太小（远到看不清字）时顺手抬到 0.85，避免"跳过去还是看不清"。 */
function focusNode(id) {
  const node = byId.value[id];
  if (!node) return;
  state.selectedId = id;
  selection.value = [id];
  studioOpen.value = false;
  if (view.value.z < 0.85) view.value = { ...view.value, z: 0.85 };
  const rect = stageRect();
  view.value = {
    ...view.value,
    x: rect.width / 2 - (node.x + CARD_W / 2) * view.value.z,
    y: rect.height / 2 - (node.y + cardHeight(node) / 2) * view.value.z,
  };
  flashId.value = id;
  window.clearTimeout(flashTimer);
  flashTimer = window.setTimeout(() => { flashId.value = ""; }, 1600);
}

const pickSide = focusNode;

function mediaKind(node) {
  const raw = node.raw || node;
  if (["video", "video_gen"].includes(raw.type)) return "video";
  if (["audio", "tts_gen"].includes(raw.type)) return "audio";
  return "image";
}

async function generateOne(node) {
  const raw = node.raw || node;
  const board = byId.value[raw.id];
  if (board?.busy) {
    showToast("这张卡正在生成，等它完成（或到「历史」里看上一版）");
    return;
  }
  const provider = state.mediaProviders.find((item) => item.id === state.mediaProvider);
  const cost = provider?.mock ? "Mock 占位生成，不产生费用。" : "真实 Provider，可能产生费用。";
  try {
    await ElMessageBox.confirm(
      `只跑「${raw.label || raw.id}」这一张，模型：${state.mediaModel || "默认"}。${cost}`,
      "单卡重新生成",
      { confirmButtonText: "开始生成", cancelButtonText: "先不跑", type: provider?.mock ? "info" : "warning" },
    );
  } catch {
    return;
  }
  markPending([raw.id], true);
  try {
    await actions.runCanvas({ approved: true, only: [raw.id] });
    await actions.loadCanvas();
    flashDone([raw.id]);
    showToast("已生成，可在「历史」里对比版本");
  } catch (err) {
    ElMessage.error({ message: `生成失败：${err.message || err}`, duration: 5000 });
  } finally {
    markPending([raw.id], false);
  }
}

function requestUpload(node) {
  const raw = node.raw || node;
  pendingUpload.value = { id: raw.id, kind: mediaKind(node) };
  uploadInput.value?.click();
}

/* 失败卡上的「重试」：走同一条单卡生成链路，不绕过审批与参数解析 */
function onRetry(node) {
  const board = byId.value[node?.id];
  generateOne(board || node);
}

async function onUploadPicked(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file || !pendingUpload.value) return;
  const { id, kind } = pendingUpload.value;
  pendingUpload.value = null;
  try {
    await actions.uploadMedia(file, kind, id);
    showToast("已替换这张卡的内容");
  } catch (err) {
    ElMessage.error(`上传失败：${err.message || err}`);
  }
}

async function saveText(node, text) {
  const raw = node.raw || node;
  await actions.savePrompt(raw.id, text);
}

async function onSend({ node, text }) {
  const raw = node.raw || node;
  if (!text && !node.canGenerate) return;
  try {
    if (text !== node.prompt) await saveText(node, text);
  } catch (err) {
    ElMessage.error(`保存失败：${err.message || err}`);
    return;
  }
  if (node.canGenerate) {
    await generateOne(node);
    return;
  }
  showToast(TEXT_TYPES.has(raw.type) ? "已保存这段文字" : "已保存这张卡的要求");
}

function onToolbar(key, node) {
  const raw = node.raw || node;
  state.selectedId = raw.id;
  selection.value = [raw.id];
  if (key === "generate") {
    const board = byId.value[raw.id];
    if (board?.busy) {
      showToast("这张卡正在生成，等它完成（或到「历史」里看上一版）");
      return;
    }
    generateOne(node);
  }
  else if (key === "upload") requestUpload(node);
  else if (key === "history") historyOpen.value = true;
  else if (key === "detail") studioOpen.value = true;
}

/* 选中一张卡时只出「卡上工具条 + 卡下提示词框」，右侧详情面板要显式点「详情」才打开，
 * 否则它会把画布右半边整块盖住（LibTV 也没有常驻的详情面板）。 */
function onSelect(id) {
  selection.value = [id];
  state.selectedId = id;
  studioOpen.value = false;
  dragPreview.value = null;
}

/* 视频大图：卡里的播放键 / 双击画面 → 灯箱，看清表演和运镜；同时把这张卡选中，关掉后还在原位 */
function onOpenVideo({ id, src, title, badge, duration }) {
  if (!src) return;
  if (id) onSelect(id);
  const filename = String(src).split("?")[0].split("/").pop() || "video.mp4";
  videoOpen.value = { src, title, badge, duration: Number(duration) || 0, download: src, filename };
}

/* ---------- 拖线建边：从卡片右侧 ⊕ 按住在画布上拖到另一张卡 ---------- */
function onEdgeStart({ id, clientX, clientY }) {
  const from = byId.value[id];
  if (!from) return;
  edgeDraft.value = {
    sourceId: id,
    from: nodeAnchor(from, "right"),
    to: toFlow(clientX, clientY),
  };
  window.addEventListener("pointermove", onEdgeMove);
  window.addEventListener("pointerup", onEdgeUp, { once: true });
}

function onEdgeMove(event) {
  if (!edgeDraft.value) return;
  edgeDraft.value = { ...edgeDraft.value, to: toFlow(event.clientX, event.clientY) };
}

async function onEdgeUp(event) {
  window.removeEventListener("pointermove", onEdgeMove);
  const draft = edgeDraft.value;
  edgeDraft.value = null;
  if (!draft) return;
  const el = document.elementFromPoint(event.clientX, event.clientY)?.closest?.(".node");
  const targetId = el?.getAttribute("data-id");
  if (!targetId || targetId === draft.sourceId) return;
  await connectNodes(draft.sourceId, targetId);
}

function portSpec(type) {
  const spec = state.canvas.spec?.[type] || {};
  return { out: Object.entries(spec.out || {}), in: Object.entries(spec.in || {}) };
}

/* 端口要按**数据类型**配对：asset 出 IMAGE，video_gen 的输入里有 prompt(STRING)/ref(IMAGE)，
 * 只取「第一个输入口」会撞上 STRING，被服务端 400。这里按类型找第一个能匹配的组合。 */
function matchPorts(sourceType, targetType) {
  const from = portSpec(sourceType);
  const to = portSpec(targetType);
  for (const [outName, outType] of from.out) {
    const hit = to.in.find(([, inType]) => inType === outType);
    if (hit) return { sourceHandle: outName, targetHandle: hit[0] };
  }
  return null;
}

async function connectNodes(sourceId, targetId) {
  const source = (state.canvas.nodes || []).find((node) => node.id === sourceId);
  const target = (state.canvas.nodes || []).find((node) => node.id === targetId);
  if (!source || !target) return;
  const from = portSpec(source.type);
  const to = portSpec(target.type);
  if (!from.out.length) {
    showToast("这张卡没有输出口，连不出去");
    return;
  }
  if (!to.in.length) {
    showToast(`「${target.label || target.id}」没有输入口，接不上`);
    return;
  }
  const ports = matchPorts(source.type, target.type);
  if (!ports) {
    const types = from.out.map(([, type]) => type).join("/");
    showToast(`这条线接不上：目标没有能收 ${types} 的口`);
    return;
  }
  const edgeCountBefore = (state.canvas.edges || []).length;
  try {
    await actions.connectNodes({
      source: sourceId,
      sourceHandle: ports.sourceHandle,
      target: targetId,
      targetHandle: ports.targetHandle,
    });
    await actions.loadCanvas();
    const added = (state.canvas.edges || []).length - edgeCountBefore;
    showToast(added > 0
      ? `已连线：${source.label || sourceId} → ${target.label || targetId}`
      : "这两个节点已经连过了");
  } catch (err) {
    ElMessage.error(`连线失败：${err.message || err}`);
  }
}

/* ---------- 多选：批量拖动 / 批量生成 ---------- */
function onDragPreview(payload) {
  dragPreview.value = payload;
}

function previewFor(nodeId) {
  if (!dragPreview.value || dragPreview.value.id === nodeId) return null;
  if (!selectedIds.value.has(nodeId)) return null;
  return { x: dragPreview.value.dx, y: dragPreview.value.dy };
}

async function onMove(payload) {
  const { id, dx = 0, dy = 0 } = payload;
  // 选区（含主卡）一起移动；单卡时就是它自己
  const ids = selection.value.length > 1 && selectedIds.value.has(id)
    ? [...selection.value]
    : [id];
  const nodes = ids.map((item) => byId.value[item]).filter(Boolean);
  const moves = dragMoves(nodes, id, dx, dy);
  dragPreview.value = null;
  if (!moves.length) return;
  try {
    await actions.moveNodes({ moves });
  } catch (err) {
    if (err?.code === "canvas_revision_conflict") {
      ElMessage.warning("画布刚被其他操作更新，位置已刷新到最新状态；请确认后再拖一次");
    } else {
      ElMessage.error({ message: `位置保存失败：${err?.message || err}`, duration: 4000 });
    }
  }
}

const selectedGenerators = computed(() => (state.canvas.nodes || [])
  .filter((node) => selectedIds.value.has(node.id) && ["image_gen", "video_gen"].includes(node.type)));

async function batchGenerate() {
  const ids = selectedGenerators.value.map((node) => node.id);
  if (!ids.length) {
    showToast("选中的卡片里没有可生成的卡（图片生成 / 视频生成）");
    return;
  }
  const runnable = ids.filter((id) => !byId.value[id]?.busy);
  if (!runnable.length) {
    showToast("选中的卡都在生成中，等它们完成");
    return;
  }
  const provider = state.mediaProviders.find((item) => item.id === state.mediaProvider);
  try {
    await ElMessageBox.confirm(
      `只跑选中的 ${runnable.length} 张生成卡，模型：${state.mediaModel || "默认"}。${provider?.mock ? "Mock 占位生成，不产生费用。" : "真实 Provider，可能产生费用。"}`,
      "批量生成选中卡片",
      { confirmButtonText: "开始生成", cancelButtonText: "先不跑", type: provider?.mock ? "info" : "warning" },
    );
  } catch {
    return;
  }
  markPending(runnable, true);
  showToast(`已提交 ${runnable.length} 张，生成中…`);
  try {
    await actions.runCanvas({ approved: true, only: runnable });
    await actions.loadCanvas();
    flashDone(runnable);
    showToast(`已生成 ${runnable.length} 张`);
  } catch (err) {
    ElMessage.error({ message: `批量生成失败：${err.message || err}`, duration: 5000 });
  } finally {
    markPending(runnable, false);
  }
}

/* ---------- 删除节点 ---------- */
function nodeIdOf(nodeLike) {
  return nodeLike?.raw?.id || nodeLike?.id || nodeLike?.node?.id || null;
}

async function deleteNodes(ids) {
  const unique = [...new Set(ids.filter(Boolean))];
  if (!unique.length) return;
  const generating = unique.filter((id) => {
    const node = byId.value[id];
    return node && ["generating", "queued"].includes(node.status);
  });
  try {
    await ElMessageBox.confirm(
      unique.length === 1
        ? `删除「${byId.value[unique[0]]?.label || unique[0]}」？它的连线会一起删除，这一步不可撤销。`
        : `删除选中的 ${unique.length} 个节点？它们的连线会一起删除，这一步不可撤销。`,
      "删除节点",
      {
        confirmButtonText: "删除",
        cancelButtonText: "取消",
        type: "warning",
        ...(generating.length
          ? { message: `其中 ${generating.length} 个节点正在生成，删除后本次结果会被丢弃。` }
          : {}),
      },
    );
  } catch {
    return;
  }
  let removed = 0;
  for (const id of unique) {
    try {
      await actions.deleteNode(id);
      removed += 1;
      selection.value = selection.value.filter((item) => item !== id);
      if (state.selectedId === id) state.selectedId = null;
    } catch (err) {
      ElMessage.error({ message: `删除失败：${err.message || err}`, duration: 5000 });
      break;
    }
  }
  if (removed) showToast(`已删除 ${removed} 个节点`);
}

/* ---------- 右键菜单 ---------- */
function onNodeContextMenu({ node, clientX, clientY }) {
  const raw = node.raw || node;
  state.selectedId = raw.id;
  selection.value = [raw.id];
  menu.value = {
    x: clientX,
    y: clientY,
    title: raw.label || raw.id,
    node,
    items: [
      { key: "focus", label: "聚焦到这张卡" },
      ...(node.canGenerate ? [{ key: "generate", label: "生成这一张" }] : []),
      ...(node.canUpload ? [{ key: "upload", label: "上传 / 换一张" }] : []),
      ...(node.historyCount > 0 ? [{ key: "history", label: `历史版本（${node.historyCount}）` }] : []),
      { key: "detail", label: "打开详情面板" },
      { key: "copy-label", label: "复制标题" },
      { key: "delete", label: "删除节点", hint: "会连带删除连线" },
    ],
  };
}

function onStageContextMenu(event) {
  if (event.target.closest?.(".node")) return;
  menu.value = {
    x: event.clientX,
    y: event.clientY,
    title: "画布",
    items: [
      { key: "add-image_gen", label: "新建图片生成卡" },
      { key: "add-video_gen", label: "新建视频生成卡" },
      { key: "add-prompt", label: "新建提示词卡" },
      { key: "search", label: "搜索节点", hint: "Ctrl/⌘ K" },
      { key: "fit", label: "适应窗口" },
    ],
  };
}

async function handleMenu(key) {
  const node = menu.value?.node;
  menu.value = null;
  const current = node || selectedNode.value;
  if (key === "focus" && current) focusNode(current.id || current.raw?.id);
  else if (key === "generate" && current) generateOne(current);
  else if (key === "upload" && current) requestUpload(current);
  else if (key === "history" && current) { studioOpen.value = false; historyOpen.value = true; }
  else if (key === "detail" && current) { studioOpen.value = true; historyOpen.value = false; }
  else if (key === "copy-label") {
    const text = (current?.label || current?.raw?.label || "").trim();
    if (text && navigator.clipboard) {
      navigator.clipboard.writeText(text).then(() => showToast(`已复制：${text}`)).catch(() => {});
    }
  } else if (key === "delete" && current) {
    await deleteNodes([nodeIdOf(current)]);
  } else if (key.startsWith("add-")) {
    await addNode(undefined, undefined, key.slice(4));
  } else if (key === "search") searchOpen.value = true;
  else if (key === "fit") fit();
}

function clearSelection() {
  selection.value = [];
  state.selectedId = null;
}

function onKeyDown(event) {
  // 视频大图开着时键盘交给灯箱（Esc 关闭），别在背后删节点或清掉选区
  if (videoOpen.value) return;
  if (event.code === "Space" && !event.target.closest?.("textarea, input")) {
    spaceDown.value = true;
  }
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    menu.value = null;
    searchOpen.value = true;
    return;
  }
  const typing = event.target.closest?.("textarea, input, [contenteditable='true']");
  if (!typing && (event.key === "Delete" || event.key === "Backspace")) {
    const ids = selection.value.length ? [...selection.value] : [state.selectedId].filter(Boolean);
    if (ids.length) {
      event.preventDefault();
      deleteNodes(ids);
      return;
    }
  }
  if (event.key === "Escape") {
    menu.value = null;
    searchOpen.value = false;
    clearSelection();
  }
}
function onKeyUp(event) {
  if (event.code === "Space") spaceDown.value = false;
}
onMounted(() => {
  window.addEventListener("keydown", onKeyDown);
  window.addEventListener("keyup", onKeyUp);
});
</script>

<template>
  <div class="cv2 board">
    <aside class="board-side">
      <SidebarPanel
        v-model:tab="tab"
        v-model:keyword="keyword"
        :selected-id="state.selectedId"
        :total="boardNodes.length"
        :hidden="!boardNodes.length"
        :nodes="sideItems"
        @pick="pickSide"
      />
    </aside>

    <main
      ref="stageRef"
      class="board-stage"
      :class="lodClass"
      @contextmenu.prevent="onStageContextMenu"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointerleave="onPointerUp"
      @wheel.prevent="onWheel"
      @click="onStageClick"
      @dblclick="onStageDoubleClick"
    >
      <div class="stage">
        <div
          class="grid-bg"
          :style="{
            backgroundSize: `${22 * view.z}px ${22 * view.z}px`,
            backgroundPosition: `${view.x}px ${view.y}px`,
          }"
        ></div>

        <div
          v-if="boardNodes.length"
          class="world"
          :style="{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.z})` }"
        >
          <svg class="edges" width="6000" height="4000">
            <path v-for="(d, index) in edges" :key="index" :d="d" />
            <path
              v-if="edgeDraft"
              class="temp"
              :d="edgePath(edgeDraft.from, edgeDraft.to)"
            />
          </svg>
          <NodeCard
            v-for="node in visibleNodes"
            :key="node.id"
            :node="node"
            :selected="selection.length === 1 && selection[0] === node.id"
            :in-selection="selectedIds.has(node.id)"
            :zoom="view.z"
            :preview="previewFor(node.id)"
            @select="onSelect"
            @move="onMove"
            @drag="onDragPreview"
            @send="onSend"
            @edge-start="onEdgeStart"
            @context-menu="onNodeContextMenu"
            @generate="(n) => onToolbar('generate', n)"
            @upload="(n) => onToolbar('upload', n)"
            @history="(n) => onToolbar('history', n)"
            @detail="(n) => onToolbar('detail', n)"
            @retry="onRetry"
            @open-video="onOpenVideo"
          />
        </div>

        <div v-if="marquee" class="marquee" :style="marqueeStyle"></div>

        <div v-if="selection.length > 1" class="multi-bar">
          <span class="multi-count">已选中 {{ selection.length }} 张</span>
          <button type="button" @click="batchGenerate">批量生成</button>
          <button type="button" class="ghost" @click="clearSelection">取消选择</button>
        </div>

        <HeroPage v-if="!boardNodes.length" @pick="addNode()" />

        <div class="zoom-pill">
          <button type="button" @click="zoomBy(1.2)">＋</button>
          <button type="button" @click="zoomBy(1 / 1.2)">−</button>
          <span>{{ Math.round(view.z * 100) }}%</span>
          <button class="fit" type="button" title="适应窗口" @click="fit">⤢</button>
        </div>

        <DockBar :empty="!boardNodes.length" @add="addNode()" />
        <div class="count-pill">共 {{ boardNodes.length }} 节点</div>
      </div>

      <ContextMenu
        v-if="menu"
        :menu="menu"
        @select="handleMenu"
        @close="menu = null"
      />
      <NodeSearch
        :open="searchOpen"
        :nodes="sideItems"
        @close="searchOpen = false"
        @pick="focusNode"
      />

      <NodeStudio
        v-if="selectedNode && studioOpen"
        :node="selectedNode"
        :spec="selectedSpec"
        :upstream="upstreamOfSelected"
        :order="selectedOrder"
        @close="studioOpen = false"
        @history="historyOpen = true"
        @upload="(kind) => requestUpload({ raw: selectedNode })"
      />
      <GenerationHistoryDrawer
        :visible="historyOpen && !!selectedNode"
        :node="selectedNode"
        @close="historyOpen = false"
      />

      <VideoLightbox v-if="videoOpen" :video="videoOpen" @close="videoOpen = null" />

      <input ref="uploadInput" type="file" accept="image/*,video/*,audio/*" class="hidden-upload" @change="onUploadPicked" />
    </main>

    <div v-if="toast" class="toast">{{ toast }}</div>
  </div>
</template>

<style scoped>
.hidden-upload { display: none; }
</style>
