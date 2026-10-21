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
import { bodyHeight, cardHeight, cardWidth, clamp, edgePath, nodeAnchor, portOffset } from "./geometry.js";
import { dragMoves } from "./positions.js";
import { generationProgress } from "./progress.js";
import { goHome, openStudioForCurrent, openStudioTab } from "../shell/shellStore.js";
import NodeCard from "./NodeCard.vue";
import CardInspector from "./CardInspector.vue";
import HeroPage from "./HeroPage.vue";
import DockBar from "./DockBar.vue";
import SidebarPanel from "./SidebarPanel.vue";
import TopBar from "./TopBar.vue";
import NodeStudio from "../components/NodeStudio.vue";
import GenerationHistoryDrawer from "../components/GenerationHistoryDrawer.vue";
import ContextMenu from "./ContextMenu.vue";
import NodeSearch from "./NodeSearch.vue";
import VideoLightbox from "./VideoLightbox.vue";
import ApprovalSheet from "./ApprovalSheet.vue";

/* 顶栏的项目管理入口由工作台外壳（CanvasWorkspace）负责，这里只转发 */
const emit = defineEmits(["open-project"]);

const TYPE_LABEL = {
  asset: "资产", image: "图片", image_gen: "图片生成",
  video: "视频", video_gen: "视频生成",
  audio: "音频", tts_gen: "旁白生成",
  text: "文本", prompt: "片段提示词", storyboard: "分镜", timeline: "时间线 / 成片",
};
/* 11 种节点 → 5 个数据家族 + 一个 glyph。形状不变，只用「2px 类型条 + 卡外 glyph」区分，
 * 不靠给每种类型换形状/换底色（那会变成彩色贴纸墙）。 */
const TYPE_GLYPH = {
  asset: "⬡", image: "▣", image_gen: "◈",
  video: "▷", video_gen: "◉",
  audio: "♪", tts_gen: "♫",
  text: "¶", prompt: "✎", storyboard: "▤",
  timeline: "▥",
};
const TYPE_FAMILY = {
  text: "string", prompt: "string", storyboard: "string",
  image: "image", image_gen: "image", asset: "image",
  video: "video", video_gen: "video",
  audio: "audio", tts_gen: "audio",
  timeline: "mixed",
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

/* 拖拽/缩放把事件频率直接打到响应式上，55 节点的画布会掉帧。
 * 这里把「最近一次目标状态」攒到下一帧再统一写入：事件再多，一帧只渲染一次。 */
let viewRaf = 0;
let pendingView = null;
function currentView() {
  return pendingView || view.value;
}
function setView(next) {
  pendingView = next;
  if (viewRaf) return;
  viewRaf = requestAnimationFrame(() => {
    viewRaf = 0;
    const apply = pendingView;
    pendingView = null;
    if (apply) view.value = apply;
  });
}

let dragRaf = 0;
let pendingDrag = null;
function setDragPreview(payload) {
  pendingDrag = payload;
  if (dragRaf) return;
  dragRaf = requestAnimationFrame(() => {
    dragRaf = 0;
    dragPreview.value = pendingDrag;
    pendingDrag = null;
  });
}
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
const edgeDraft = ref(null);    // { sourceId, handle, ptype, from:{x,y}, to:{x,y} }
// 连线交互（双模）：① 按住端口拖到目标卡；② 轻点端口进入「点击式连线」，再点目标卡。
// 两种模式共用 hoverTargetId 做目标高亮与吸附，落点不再要求像素级精确。
const connectFrom = ref(null);   // 点击式连线：{ nodeId, handle, ptype }
const connectCursor = ref(null); // 点击式连线的预览线终点（world 坐标）
const hoverTargetId = ref("");   // 当前吸附到的目标卡
const selectedEdgeId = ref("");  // 选中的连线（Delete 删除 / 双击直接删）
const PORT_LABELS = {
  prompt: "提示词", ref: "参考图", image: "图像", video: "视频", video_ref: "视频参考",
  text: "文本", desc: "描述", script: "剧本", ctx: "上下文", audio: "音频",
  subtitle: "字幕", video_in: "视频", shots: "分镜",
};
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
    return node.x + cardWidth(node) > left && node.x < right && node.y + h > top && node.y < bottom;
  });
});

const visibleIds = computed(() => new Set(visibleNodes.value.map((node) => node.id)));
const visibleEdges = computed(() => (state.canvas.edges || []).filter((edge) => {
  const source = edge.source || edge.from;
  const target = edge.target || edge.to;
  return visibleIds.value.has(source) && visibleIds.value.has(target);
}));
const selectedIds = computed(() => new Set(selection.value));

/* ---------- 顶栏 / 状态栏（阶段 1）：只读投影 + 动作转发 ---------- */
const savedAt = ref(0);
watch(
  () => state.canvasServerRevision,
  () => { savedAt.value = Date.now(); },
);
const saveState = computed(() => (state.canvasBusy || state.mediaUploading ? "syncing" : "saved"));
const projectTitle = computed(() => (
  state.canvasMeta[state.canvasName]?.story_title
  || state.canvas.meta?.story_title
  || state.canvasName
  || "未命名项目"
));

function onBack() {
  goHome();
}
function onSwitchView(id) {
  if (id === "canvas") return;
  if (id === "studio") {
    openStudioForCurrent();
    return;
  }
  if (id === "assets" && !openStudioTab("assets")) showToast("先在项目里打开一集，再看素材库");
}
function onChangeModel({ provider, model }) {
  if (provider && provider !== state.mediaProvider) actions.setMediaProvider(provider);
  if (model && model !== state.mediaModel) actions.setMediaModel(model);
}
async function onExportPrompts() {
  try {
    await actions.exportPrompts();
  } catch (err) {
    ElMessage.error({ message: `导出失败：${err.message || err}`, duration: 5000 });
  }
}
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

const projectMeta = computed(() => state.canvas?.meta?.project || {});

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
    type,
    fam: TYPE_FAMILY[type] || "mixed",
    glyph: TYPE_GLYPH[type] || "▣",
    tag: TYPE_LABEL[type] || type,
    dims: node.data?.width && node.data?.height ? `${node.data.width} × ${node.data.height}` : "",
    // V2 卡片几何（宽高比固定 + 三档宽度）：NodeCard 直接用，连线/剔除/适配同一口径
    cardW: cardWidth({ type }),
    cardBodyH: bodyHeight({ type, plain: TEXT_TYPES.has(type) && !media }),
    // 标签行右对齐的元信息：真实像素优先，否则给项目画幅 + 画质
    res: node.data?.width && node.data?.height
      ? `${node.data.width} × ${node.data.height}`
      : `${projectMeta.value.aspect_ratio || "16:9"} · ${String(projectMeta.value.quality || "720P").toUpperCase()}`,
    seconds: Number(node.data?.duration_s || node.data?.duration || 0) || null,
    durationText: durationSeconds ? `${Math.floor(durationSeconds / 60)}:${String(Math.round(durationSeconds % 60)).padStart(2, "0")}` : "",
    img: media && !isVideo ? assetUrl(media.path) : "",
    src: media ? assetUrl(media.path) : "",
    video: isVideo,
    durationSeconds,
    badge: media ? (media.mock ? "mock" : human ? "手传" : "AI生成") : "",
    // 生成状态：卡片遮罩 / 失败重试 / 完成高亮都读这几个字段（只读，不改数据）
    status: node.status || "",
    busy: pendingGen.value.has(node.id) || ["queued", "generating"].includes(node.status),
    busyLabel: node.status === "queued" ? "排队中…" : node.status === "generating" ? "生成中…" : "已提交…",
    // 进度只读服务端写的 data.generation_progress；没有时按 0/3 起点画，不编百分比
    progress: pendingGen.value.has(node.id) || ["queued", "generating"].includes(node.status)
      ? generationProgress(node.data)
      : null,
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
    portsIn: portSpec(type).in,
    portsOut: portSpec(type).out,
    raw: node,
  };
}));

const byId = computed(() => Object.fromEntries(boardNodes.value.map((node) => [node.id, node])));

/* 只给「两端都在视口里」的连线画路径：大画布上连线数量往往是节点数的 1.5 倍，
 * 全画会把 svg 撑爆，而且屏幕外的线本来也看不见。 */
function portIndex(type, side, handle) {
  const list = portSpec(type)[side];
  const found = list.findIndex(([name]) => name === handle);
  return found < 0 ? 0 : found;
}

/* 一条边落在哪个口：老数据没有 handle 时退回「垂直居中」，不会跳到别的位置 */
function edgeAnchor(boardNode, side, handle) {
  const key = side === "left" ? "in" : "out";
  const list = portSpec(boardNode.type)[key];
  const index = Math.max(0, list.findIndex(([name]) => name === handle));
  return nodeAnchor(boardNode, side, index, list.length);
}

const edges = computed(() => visibleEdges.value
  .map((edge) => {
    const from = byId.value[edge.source ?? edge.from];
    const to = byId.value[edge.target ?? edge.to];
    if (!from || !to) return null;
    const sourceHandle = edge.sourceHandle ?? edge.source_handle;
    const targetHandle = edge.targetHandle ?? edge.target_handle;
    // 选中节点时，与它直接相连的线算「链路」：提亮但不抢被点中那条的注意力
    const chain = Boolean(state.selectedId)
      && ((edge.source ?? edge.from) === state.selectedId || (edge.target ?? edge.to) === state.selectedId);
    return {
      id: edge.id,
      d: edgePath(edgeAnchor(from, "right", sourceHandle), edgeAnchor(to, "left", targetHandle)),
      chain,
    };
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

/* ---------- 卡片检查器（HUD / 屏幕坐标系） ----------
 * 为什么不放进 .world：world 上有 transform: scale()，放进去会随缩放变形。
 * 定位一律用选中节点元素的 getBoundingClientRect()（已含平移+缩放），
 * 所以只需要在「选中 / 平移 / 缩放 / 拖动 / 窗口变化」时重算。 */
const INSPECTOR_W = 620;
const inspectorWrap = ref(null);
const inspectorStyle = ref({ left: "-9999px", top: "0px" });
let inspectorRaf = 0;

// 多选不出检查器（出多选浮条）
const inspectorNode = computed(() => (
  selection.value.length === 1 && state.selectedId === selection.value[0]
    ? byId.value[state.selectedId] || null
    : null
));

const inspectorRefs = computed(() => {
  const node = inspectorNode.value;
  if (!node) return [];
  const target = node.id;
  const sourceIds = new Set(
    (state.canvas.edges || [])
      .filter((edge) => (edge.target ?? edge.to) === target)
      .map((edge) => edge.source ?? edge.from),
  );
  return (state.canvas.nodes || [])
    .filter((raw) => sourceIds.has(raw.id))
    .map((raw) => {
      const board = byId.value[raw.id];
      const media = mediaOf(raw);
      return {
        id: raw.id,
        title: raw.label || raw.id,
        src: media ? assetUrl(media.path) : "",
        kind: media?.port === "video" ? "video" : "image",
        desc: bodyText(raw),
        duration: board?.durationText || "",
      };
    })
    .filter((item) => item.src || item.desc);
});

const providerLabel = computed(() => (
  state.mediaProvider === "mock" ? "mock · 不产生费用" : (state.mediaProvider || "占位")
));
const modelLabel = computed(() => state.mediaModel || "默认模型");

function positionInspector() {
  inspectorRaf = 0;
  const node = inspectorNode.value;
  const wrap = inspectorWrap.value?.$el || inspectorWrap.value;
  if (!node || !wrap) return;
  const w = INSPECTOR_W;
  // 自然高度 = 头部 32 + 底部 44 + 正文内容高度；不能拿被 max-height 夹过的 offsetHeight，
  // 否则检查器会越缩越小。
  const bodyEl = wrap.querySelector?.(".ins-body");
  const natural = 32 + 44 + (bodyEl?.scrollHeight || 220);
  const desired = Math.min(natural, Math.round(window.innerHeight * 0.7), 560);
  if (view.value.z < 0.4) {
    // 缩得太小：节点本身已经承载不了信息，检查器贴视口右侧固定
    const left = Math.max(16, window.innerWidth - w - 20);
    inspectorStyle.value = { left: `${left}px`, top: "88px", maxHeight: `${desired}px` };
    return;
  }
  const el = stageRef.value?.querySelector(`.node[data-id="${node.id}"]`);
  if (!el) return;
  const r = el.getBoundingClientRect();
  const pad = 12;
  const minY = 72;
  const stage = stageRect();
  // 不允许压住左侧项目栏：检查器是画布 HUD，不是全屏弹层
  const minX = Math.max(16, stage.left + 8);
  const maxX = window.innerWidth - 16 - w;
  const maxY = window.innerHeight - 16;
  const fitH = 196; // 低于这个高度就不值得放在这个方向了
  let left = 0;
  let top = 0;
  let maxH = desired;
  const belowTop = r.bottom + pad;
  const aboveTop = r.top - pad - desired;
  const rightLeft = r.right + pad;
  const leftLeft = r.left - pad - w;
  if (belowTop + fitH <= maxY) {
    // 首选卡片正下方：阅读顺序最自然
    top = belowTop;
    maxH = Math.min(desired, maxY - top);
    left = clampRect(r.left, minX, Math.max(minX, maxX));
  } else if (aboveTop >= minY) {
    top = Math.max(minY, aboveTop);
    maxH = Math.min(desired, r.top - pad - top);
    left = clampRect(r.left, minX, Math.max(minX, maxX));
  } else if (rightLeft <= maxX) {
    left = rightLeft;
    top = clampRect(r.top, minY, Math.max(minY, maxY - 120));
    maxH = Math.min(desired, maxY - top);
  } else if (leftLeft >= minX) {
    left = leftLeft;
    top = clampRect(r.top, minY, Math.max(minY, maxY - 120));
    maxH = Math.min(desired, maxY - top);
  } else {
    // 四个方向都放不下（卡片比视口还大）：贴右侧下部，允许压住卡片边缘
    left = Math.max(minX, maxX);
    top = Math.max(minY, maxY - desired);
    maxH = desired;
  }
  // 底部 dock / AI 球是常驻操作区：检查器宁可缩高度，也不要压住它们
  const dockEl = document.querySelector(".cv2 .dock");
  const orbEl = document.querySelector(".cv2-agent-launcher");
  const obstacles = [dockEl, orbEl].filter(Boolean).map((el) => el.getBoundingClientRect());
  for (const ob of obstacles) {
    const overlapX = left + w > ob.left - 8 && left < ob.right + 8;
    const overlapY = top + maxH > ob.top + 4 && top < ob.bottom - 4;
    if (!overlapX || !overlapY) continue;
    const shifted = ob.left - 12 - w;
    if (shifted >= minX) left = shifted;
    else maxH = Math.min(maxH, Math.max(120, ob.top - 8 - top));
  }
  inspectorStyle.value = { left: `${Math.round(left)}px`, top: `${Math.round(top)}px`, maxHeight: `${Math.round(maxH)}px` };
}

function clampRect(value, low, high) {
  return Math.min(Math.max(low, value), Math.max(low, high));
}

function scheduleInspector() {
  if (inspectorRaf) return;
  inspectorRaf = requestAnimationFrame(positionInspector);
}

watch(
  () => [
    inspectorNode.value?.id,
    view.value.x,
    view.value.y,
    view.value.z,
    dragPreview.value?.dx,
    dragPreview.value?.dy,
  ],
  () => scheduleInspector(),
  { flush: "post" },
);
watch(inspectorNode, () => nextTick(scheduleInspector));
onMounted(() => {
  window.addEventListener("resize", scheduleInspector);
});
onBeforeUnmount(() => {
  window.removeEventListener("resize", scheduleInspector);
  if (inspectorRaf) cancelAnimationFrame(inspectorRaf);
});

function stageRect() {
  return stageRef.value?.getBoundingClientRect() || { left: 0, top: 0, width: 1200, height: 800 };
}

function fit() {
  const rect = stageRect();
  const nodes = boardNodes.value;
  if (!nodes.length || !rect.width) return;
  const maxX = Math.max(...nodes.map((node) => node.x + cardWidth(node)));
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
    x: rect.width / 2 - (first.x + cardWidth(first) / 2) * z,
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
  // 点击式连线时点空白 = 取消连线，不要顺手开始框选
  if (connectFrom.value && !event.target.closest?.(".node")) {
    event.preventDefault();
    cancelConnect();
    return;
  }
  if (event.target.closest?.(".node")) return;
  // 中键 / 按住空格 = 平移；左键拖空白 = 框选（和主流画布一致）
  if (event.button === 1 || spaceDown.value) {
    const base = currentView();
    pan.value = { sx: event.clientX, sy: event.clientY, vx: base.x, vy: base.y };
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
  // 拖拽连线由 window 上的 onEdgeMove 负责，这里不要重复处理
  if (edgeDraft.value) return;
  // 点击式连线：预览线跟着光标走，并吸附高亮合法目标
  if (connectFrom.value && !pan.value) {
    const point = toFlow(event.clientX, event.clientY);
    const candidate = candidateFor(point);
    hoverTargetId.value = candidate ? candidate.id : "";
    connectCursor.value = candidate
      ? targetAnchor(candidate.id, candidate.ports.targetHandle) || point
      : point;
    return;
  }
  if (pan.value) {
    const { sx, sy, vx, vy } = pan.value;
    setView({ ...currentView(), x: vx + (event.clientX - sx), y: vy + (event.clientY - sy) });
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
    const nx2 = node.x + cardWidth(node);
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
  const base = currentView();
  const z = clamp(base.z * Math.exp(-event.deltaY * 0.0016), 0.15, 2.2);
  const k = z / base.z;
  setView({ z, x: px - (px - base.x) * k, y: py - (py - base.y) * k });
}

function zoomBy(factor) {
  const rect = stageRect();
  const px = rect.width / 2;
  const py = rect.height / 2;
  const base = currentView();
  const z = clamp(base.z * factor, 0.15, 2.2);
  const k = z / base.z;
  setView({ z, x: px - (px - base.x) * k, y: py - (py - base.y) * k });
}

function onStageClick(event) {
  if (connectFrom.value) {
    cancelConnect();
    return;
  }
  if (event.target.closest?.(".node")) return;
  // 框选结束时浏览器还会补一个 click，不能让它把刚选中的东西清掉
  if (suppressClick) {
    suppressClick = false;
    return;
  }
  selection.value = [];
  state.selectedId = null;
  selectedEdgeId.value = "";
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
      position: { x: Math.round(point.x - 180), y: Math.round(point.y - 100) },
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
    x: rect.width / 2 - (node.x + cardWidth(node) / 2) * view.value.z,
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
  await openApproval([raw.id]);
}

/* 生成前一律走审批面板：参数、参考、可生成性都由后端 generation-plan 决定，
 * 面板只负责把这一次要花什么摊开，确认后才 approvals → run。 */
async function openApproval(ids) {
  try {
    await actions.prepareGenerationReview({ only: ids });
  } catch (err) {
    ElMessage.error({ message: `读取生成计划失败：${err.message || err}`, duration: 5000 });
    return;
  }
  if (!state.generationReview) showToast("没有可生成的卡片");
}

async function approveReview() {
  const review = state.generationReview;
  if (!review || state.canvasBusy) return;
  const scoped = Array.isArray(review.only) ? review.only : [];
  const scope = scoped.length
    ? scoped
    : boardNodes.value.filter((node) => GEN_TYPES.has(node.type)).map((node) => node.id);
  if (!scope.length) {
    actions.dismissGenerationReview();
    return;
  }
  markPending(scope, true);
  showToast(`已提交 ${scope.length} 张，生成中…`);
  try {
    await actions.approveGeneration();
    await actions.loadCanvas();
    flashDone(scope);
    showToast(scope.length > 1 ? `已生成 ${scope.length} 张` : "已生成，可在「历史」里对比版本");
  } catch (err) {
    ElMessage.error({ message: `生成失败：${err.message || err}`, duration: 5000 });
  } finally {
    markPending(scope, false);
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

/* 底栏「历史」：只在恰好选中一张卡时有意义；没选中就告诉人去选，不弹空抽屉 */
function openHistory() {
  const id = selection.value.length === 1 ? selection.value[0] : state.selectedId;
  const node = id ? byId.value[id] : null;
  if (!node) {
    showToast("先选中一张卡，再看它的历史版本");
    return;
  }
  onToolbar("history", node);
}

/* 选中一张卡时只出「卡上工具条 + 卡下提示词框」，右侧详情面板要显式点「详情」才打开，
 * 否则它会把画布右半边整块盖住（LibTV 也没有常驻的详情面板）。 */
function onSelect(id) {
  // 点击式连线进行中：这一下点击是「选落点」，不是换选中
  if (connectFrom.value) {
    const source = connectFrom.value;
    if (id !== source.nodeId) {
      cancelConnect();
      connectNodes(source.nodeId, id, source.handle);
    }
    return;
  }
  selectedEdgeId.value = "";
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

/* ---------- 连线：拖拽 + 点击 双模 ---------- */
/* 端口配对：从某个具体输出口出发时，只接受能收这个类型的输入口；
 * 否则按「第一个同类型组合」兜底（旧行为）。 */
function portMatch(sourceType, fromHandle, targetType) {
  const from = portSpec(sourceType);
  const to = portSpec(targetType);
  if (!from.out.length || !to.in.length) return null;
  const draggedType = fromHandle ? (from.out.find(([name]) => name === fromHandle) || [])[1] : "";
  if (draggedType) {
    const hit = to.in.find(([, inType]) => inType === draggedType);
    return hit ? { sourceHandle: fromHandle, targetHandle: hit[0] } : null;
  }
  return matchPorts(sourceType, targetType);
}

function sourceAnchor(sourceId, handle) {
  const node = byId.value[sourceId];
  if (!node) return null;
  const list = portSpec(node.type).out;
  const index = Math.max(0, list.findIndex(([name]) => name === handle));
  return nodeAnchor(node, "right", index, list.length);
}

function targetAnchor(targetId, targetHandle) {
  const node = byId.value[targetId];
  if (!node) return null;
  const list = portSpec(node.type).in;
  const index = Math.max(0, list.findIndex(([name]) => name === targetHandle));
  return nodeAnchor(node, "left", index, list.length);
}

/* 光标准星：先看指针是否落在「类型兼容」的卡附近（外扩 36px），取最近的一张。
 * 这样松手偏出卡片 20px 也能连上，不用像素级对准。 */
function candidateFor(worldPoint) {
  const sourceId = edgeDraft.value?.sourceId || connectFrom.value?.nodeId;
  const handle = edgeDraft.value?.handle || connectFrom.value?.handle || "";
  const sourceNode = byId.value[sourceId];
  if (!sourceNode) return null;
  const pad = 36 / view.value.z;
  let best = null;
  let bestDistance = Infinity;
  for (const node of visibleNodes.value) {
    if (node.id === sourceId) continue;
    const ports = portMatch(sourceNode.type, handle, node.type);
    if (!ports) continue;
    const height = cardHeight(node);
    const width = cardWidth(node);
    if (
      worldPoint.x < node.x - pad || worldPoint.x > node.x + width + pad
      || worldPoint.y < node.y - pad || worldPoint.y > node.y + height + pad
    ) continue;
    const dx = worldPoint.x - (node.x + width / 2);
    const dy = worldPoint.y - (node.y + height / 2);
    const distance = dx * dx + dy * dy;
    if (distance < bestDistance) {
      bestDistance = distance;
      best = { id: node.id, ports };
    }
  }
  return best;
}

/* 点击式连线时，预览线要跟着光标走，并且同样吸附到目标端口 */
const connectLine = computed(() => {
  if (edgeDraft.value) return { from: edgeDraft.value.from, to: edgeDraft.value.to };
  if (!connectFrom.value || !connectCursor.value) return null;
  const from = sourceAnchor(connectFrom.value.nodeId, connectFrom.value.handle);
  return from ? { from, to: connectCursor.value } : null;
});

const connectSourceLabel = computed(() => {
  const node = byId.value[connectFrom.value?.nodeId];
  return node?.title || "";
});

function isConnectable(nodeId) {
  const sourceId = edgeDraft.value?.sourceId || connectFrom.value?.nodeId;
  if (!sourceId || nodeId === sourceId) return false;
  const sourceNode = byId.value[sourceId];
  const node = byId.value[nodeId];
  if (!sourceNode || !node) return false;
  const handle = edgeDraft.value?.handle || connectFrom.value?.handle || "";
  return !!portMatch(sourceNode.type, handle, node.type);
}

function enterConnectMode(sourceId, handle, ptype) {
  if (!byId.value[sourceId]) return;
  connectFrom.value = { nodeId: sourceId, handle: handle || "", ptype: ptype || "" };
  connectCursor.value = null;
  hoverTargetId.value = "";
  showToast("已选起点：点击目标卡片完成连线（Esc 取消）");
}

function cancelConnect() {
  if (!connectFrom.value) return;
  connectFrom.value = null;
  connectCursor.value = null;
  hoverTargetId.value = "";
}

function onEdgeStart({ id, handle, ptype, clientX, clientY }) {
  const from = byId.value[id];
  if (!from) return;
  // 上一次「点击式连线」还开着：从端口重新拖，视为换起点
  cancelConnect();
  const anchor = sourceAnchor(id, handle);
  if (!anchor) return;
  edgeDraft.value = {
    sourceId: id,
    handle: handle || "",
    ptype: ptype || "",
    from: anchor,
    to: toFlow(clientX, clientY),
    startClient: { x: clientX, y: clientY },
    moved: false,
  };
  window.addEventListener("pointermove", onEdgeMove);
  window.addEventListener("pointerup", onEdgeUp, { once: true });
}

function onEdgeMove(event) {
  if (!edgeDraft.value) return;
  const point = toFlow(event.clientX, event.clientY);
  const candidate = candidateFor(point);
  hoverTargetId.value = candidate ? candidate.id : "";
  const end = candidate ? targetAnchor(candidate.id, candidate.ports.targetHandle) || point : point;
  const dx = event.clientX - edgeDraft.value.startClient.x;
  const dy = event.clientY - edgeDraft.value.startClient.y;
  edgeDraft.value = {
    ...edgeDraft.value,
    to: end,
    moved: edgeDraft.value.moved || Math.hypot(dx, dy) > 4,
  };
}

async function onEdgeUp(event) {
  window.removeEventListener("pointermove", onEdgeMove);
  const draft = edgeDraft.value;
  edgeDraft.value = null;
  const snapped = hoverTargetId.value;
  hoverTargetId.value = "";
  if (!draft) return;
  // 没拖动 = 轻点了一下端口：进入点击式连线，不用精确拖拽
  if (!draft.moved) {
    enterConnectMode(draft.sourceId, draft.handle, draft.ptype);
    return;
  }
  let targetId = snapped;
  if (!targetId) {
    const el = document.elementFromPoint(event.clientX, event.clientY)?.closest?.(".node");
    targetId = el?.getAttribute("data-id") || "";
  }
  if (!targetId) {
    showToast("松手的位置没有可接的卡，连线已取消");
    return;
  }
  if (targetId === draft.sourceId) {
    showToast("不能连到自己");
    return;
  }
  await connectNodes(draft.sourceId, targetId, draft.handle);
}

function portsFor(type) {
  return portSpec(type);
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

async function connectNodes(sourceId, targetId, fromHandle) {
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
  // 从某个具体输出口拖出来的线：目标必须能收这个类型的口，否则明确告诉用户
  const draggedType = fromHandle
    ? (from.out.find(([name]) => name === fromHandle) || [])[1]
    : "";
  const exact = draggedType
    ? (() => {
        const hit = to.in.find(([, inType]) => inType === draggedType);
        return hit ? { sourceHandle: fromHandle, targetHandle: hit[0] } : null;
      })()
    : null;
  const ports = exact || matchPorts(source.type, target.type);
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
    const edgesNow = state.canvas.edges || [];
    const added = edgesNow.length - edgeCountBefore;
    const created = edgesNow.find((edge) => {
      const source = edge.source ?? edge.from;
      const target = edge.target ?? edge.to;
      return source === sourceId && target === targetId
        && (edge.sourceHandle ?? edge.source_handle) === ports.sourceHandle
        && (edge.targetHandle ?? edge.target_handle) === ports.targetHandle;
    });
    // 刚连上的线默认选中：连错了直接 Delete，不用去猜「哪条是新的」
    selectedEdgeId.value = created?.id || "";
    const portLabel = PORT_LABELS[ports.targetHandle] || ports.targetHandle;
    showToast(added > 0
      ? `已连线：${source.label || sourceId} → ${target.label || targetId}（${portLabel}）`
      : "这两个节点已经连过了");
  } catch (err) {
    ElMessage.error(`连线失败：${err.message || err}`);
  }
}

/* ---------- 已有连线：可点选、可删除（连线命中区 16px，不用瞄准 1px 线条） ---------- */
function pickEdge(edgeId) {
  selectedEdgeId.value = selectedEdgeId.value === edgeId ? "" : edgeId;
  if (selectedEdgeId.value) showToast("已选中这条连线：Delete 删除，双击直接删");
}

async function removeEdge(edgeId) {
  if (!edgeId) return;
  selectedEdgeId.value = "";
  try {
    await actions.deleteEdge(edgeId);
    showToast("已删除这条连线");
  } catch (err) {
    ElMessage.error(`删除连线失败：${err.message || err}`);
  }
}

/* ---------- 多选：批量拖动 / 批量生成 ---------- */
function onDragPreview(payload) {
  setDragPreview(payload);
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
  pendingDrag = null;
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
  await openApproval(runnable);
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
      { key: "arrange", label: "整理布局", hint: "按执行顺序重排" },
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
  } else if (key === "arrange") {
    try {
      const res = await actions.arrangeCanvas();
      fit();
      showToast(res?.moved ? `已重排 ${res.moved} 个节点` : "布局已经是整齐的");
    } catch (err) {
      ElMessage.error({ message: `整理失败：${err?.message || err}`, duration: 4000 });
    }
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
    if (selectedEdgeId.value) {
      event.preventDefault();
      removeEdge(selectedEdgeId.value);
      return;
    }
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
    cancelConnect();
    selectedEdgeId.value = "";
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
    <TopBar
      :title="projectTitle"
      :save-state="saveState"
      :saved-at="savedAt"
      view="canvas"
      :selected-count="selection.length"
      :providers="state.mediaProviders"
      :provider="state.mediaProvider"
      :model="state.mediaModel"
      :problems="state.canvas.problems || []"
      :busy="state.canvasBusy"
      @back="onBack"
      @open-project="emit('open-project')"
      @switch-view="onSwitchView"
      @change-model="onChangeModel"
      @generate-selected="batchGenerate"
      @export="onExportPrompts"
      @open-info="state.drawerOpen = !state.drawerOpen"
    />

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
      :class="[lodClass, { 'connect-mode': !!connectFrom || !!edgeDraft }]"
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
            backgroundSize: `${24 * view.z}px ${24 * view.z}px`,
            backgroundPosition: `${view.x}px ${view.y}px`,
          }"
        ></div>

        <div
          v-if="boardNodes.length"
          class="world"
          :style="{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.z})` }"
        >
          <svg class="edges" width="6000" height="4000">
            <g
              v-for="edge in edges"
              :key="edge.id || edge.d"
              :class="{ 'is-on': selectedEdgeId === edge.id }"
            >
              <path
                class="hit"
                :d="edge.d"
                @pointerdown.stop="pickEdge(edge.id)"
                @dblclick.stop="removeEdge(edge.id)"
              />
              <path class="line" :class="{ on: selectedEdgeId === edge.id, 'on-chain': edge.chain && selectedEdgeId !== edge.id }" :d="edge.d" />
            </g>
            <path
              v-if="connectLine"
              class="temp"
              :d="edgePath(connectLine.from, connectLine.to)"
            />
          </svg>
          <NodeCard
            v-for="node in visibleNodes"
            :key="node.id"
            :node="node"
            :selected="selection.length === 1 && selection[0] === node.id"
            :in-selection="selectedIds.has(node.id)"
            :in-ports="node.portsIn"
            :out-ports="node.portsOut"
            :connectable="isConnectable(node.id)"
            :connect-mode="!!connectFrom"
            :zoom="view.z"
            :preview="previewFor(node.id)"
            @select="onSelect"
            @move="onMove"
            @drag="onDragPreview"
            @edge-start="onEdgeStart"
            @context-menu="onNodeContextMenu"
            @upload="(n) => onToolbar('upload', n)"
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

        <div v-if="connectFrom" class="connect-hint">
          <span class="ch-dot" aria-hidden="true"></span>
          <span>连线中：点目标卡片{{ connectSourceLabel ? `（来自「${connectSourceLabel}」）` : "" }}</span>
          <em>{{ hoverTargetId ? "松手 / 点击即连接" : "Esc 取消" }}</em>
        </div>

        <HeroPage v-if="!boardNodes.length" @pick="addNode()" />

        <div class="zoom-pill">
          <button type="button" @click="zoomBy(1.2)">＋</button>
          <button type="button" @click="zoomBy(1 / 1.2)">−</button>
          <span>{{ Math.round(view.z * 100) }}%</span>
          <button class="fit" type="button" title="适应窗口" @click="fit">⤢</button>
        </div>

        <DockBar :empty="!boardNodes.length" @add="addNode()" @fit="fit()" @search="searchOpen = true" @history="openHistory()" />
        <div class="count-pill">← 共 {{ boardNodes.length }} 节点</div>
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

      <ApprovalSheet
        :review="state.generationReview"
        :busy="state.canvasBusy"
        @approve="approveReview"
        @cancel="actions.dismissGenerationReview()"
      />

      <input ref="uploadInput" type="file" accept="image/*,video/*,audio/*" class="hidden-upload" @change="onUploadPicked" />
    </main>

    <!-- 卡片检查器：fixed 在视口层，pointer-events 只在面板本身打开，画布仍可拖动/缩放 -->
    <div class="cv2-hud">
      <CardInspector
        v-if="inspectorNode"
        ref="inspectorWrap"
        :style="inspectorStyle"
        :node="inspectorNode"
        :refs="inspectorRefs"
        :project="projectMeta"
        :provider-label="providerLabel"
        :model-label="modelLabel"
        @close="clearSelection"
        @focus="(n) => focusNode(n.id)"
        @send="onSend"
        @upload="(n) => onToolbar('upload', n)"
        @history="(n) => onToolbar('history', n)"
        @detail="(n) => onToolbar('detail', n)"
        @retry="onRetry"
      />
    </div>

    <div v-if="toast" class="toast">{{ toast }}</div>
  </div>
</template>

<style scoped>
.hidden-upload { display: none; }
</style>

