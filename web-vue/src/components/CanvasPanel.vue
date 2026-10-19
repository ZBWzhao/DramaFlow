<script setup>
/* 画布页：Vue Flow 负责视口/拖拽/连线交互，我们只提供节点组件与工具栏。
 *
 * 关键取舍：连线**交给后端校验**（POST /api/canvas/connect 会检查端口类型是否兼容），
 * 前端不做重复实现 —— 类型规则只有一份，改的时候不会两边不一致。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { VueFlow, useVueFlow } from "@vue-flow/core";
import { Background } from "@vue-flow/background";
import { MiniMap } from "@vue-flow/minimap";
import { ElMessage, ElMessageBox } from "element-plus";
import ToonNode from "./ToonNode.vue";
import NodeStudio from "./NodeStudio.vue";
import GenerationHistoryDrawer from "./GenerationHistoryDrawer.vue";
import CanvasSidebar from "./CanvasSidebar.vue";
import { actions, flowEdges, flowNodes, state, selectedNode, upstreamOfSelected } from "../store.js";
import { assetUrl, mediaOf } from "../utils.js";

/* 显式 id：父组件里的 useVueFlow 必须和 <VueFlow> 用同一个 store。
 * 注意 `viewport` 不要解构 — 它从 reactive store 上取值时是个普通快照对象，
 * 拿到的永远是初始 { zoom: 1 }，所以缩放读数和 LOD 会冻在 100%。
 * 改成订阅 viewport-change 事件，自己维护一个响应式缩放值。 */
const FLOW_ID = "canvas-flow";
const { fitView, zoomTo, zoomIn, zoomOut, setViewport, onViewportChange, onConnect, screenToFlowCoordinate } =
  useVueFlow(FLOW_ID);

const zoomNow = ref(1);
onViewportChange((vp) => { zoomNow.value = vp?.zoom ?? 1; });
/* 语义缩放：远看是缩略图墙，近看才是可读的卡片。
 * 阈值按"能不能看清卡片标题"定：<0.45 只能看清图和一行标题，0.45~0.85 能读摘要，
 * >0.85 才放开端口名、进度条、提示语这些细节。 */
const lodClass = computed(() => {
  const zoom = zoomNow.value;
  if (zoom < 0.45) return "lod-far";
  if (zoom < 0.85) return "lod-mid";
  return "lod-near";
});
const zoomPct = computed(() => `${Math.round(zoomNow.value * 100)}%`);

/* 适应窗口：默认 fitView 会把 60+ 节点的图压到 20%，字全糊成一片。
 * 所以留一个"可读下限"：真放不下就宁可按 60% 居中（能看清卡片，其余靠拖拽/小地图看），
 * 也不给一张什么都看不清的全损缩略图。 */
async function fitReadable({ min = 0.7 } = {}) {
  await fitView({ padding: 0.14, maxZoom: 0.95, duration: 180 });
  await new Promise((r) => setTimeout(r, 240));   // 等 viewport-change 回来再判断
  if (zoomNow.value < min) await zoomTo(min, { duration: 160 });
}
/* 首屏不"适应窗口"，而是**按可读比例落到第一张有内容的卡上**。
 * 原因：52 个节点的画布 fitView 之后只剩 60%~70%，卡片文字被压成 6–7px，
 * 看起来就是一片小黑板；LibTV 的默认视角是"看得清两三张卡"。
 * 「适应窗口」按钮仍然保留完整 fitView。 */
async function focusReadable() {
  const nodes = state.canvas.nodes || [];
  if (!nodes.length) return;
  const hasImage = (n) => Boolean(n.outputs?.image?.value?.path || n.outputs?.ref?.value?.path);
  const hasVideo = (n) => Boolean(n.outputs?.video?.value?.path);
  // 首屏落点优先选「真的有画面的卡」，第一眼看到的应该是图，而不是一堆空卡
  const first = nodes.find(hasImage)
    || nodes.find((n) => n.type === "asset")
    || nodes.find(hasVideo)
    || nodes[0];
  const zoom = Math.min(1, Math.max(0.9, zoomNow.value || 1));
  await setViewport(
    { x: 300 - first.position.x * zoom, y: 220 - first.position.y * zoom, zoom },
    { duration: 0 },
  );
}

const problemsOpen = ref(false);
const imageFileInput = ref(null);
const videoFileInput = ref(null);
const audioFileInput = ref(null);
const replaceMediaNodeId = ref("");
const mediaUploadPosition = ref(null);
const contextMenu = ref({ visible: false, x: 0, y: 0, flow: { x: 0, y: 0 } });
const edgeMenu = ref({ visible: false, x: 0, y: 0, edgeId: "" });

const cardItems = [
  { type: "text", icon: "文", label: "文本卡片", defaultLabel: "未命名文本" },
  { type: "storyboard", icon: "镜", label: "分镜卡片", defaultLabel: "未命名分镜" },
  { type: "asset", icon: "资", label: "资产卡片", defaultLabel: "未命名资产", data: { kind: "generic" } },
  { type: "prompt", icon: "词", label: "提示词卡片", defaultLabel: "未命名提示词" },
  { type: "image_gen", icon: "图", label: "图片生成卡片", defaultLabel: "图片生成" },
  { type: "video_gen", icon: "影", label: "视频生成卡片", defaultLabel: "视频生成" },
  { type: "tts_gen", icon: "声", label: "旁白生成卡片", defaultLabel: "旁白生成", data: { provider: "mock", voice: "alloy", speed: 1 } },
  { type: "timeline", icon: "片", label: "时间线 / 成片卡片", defaultLabel: "时间线·最终成片", data: { output: { width: 1280, height: 720, fps: 24 } } },
];

function closeContextMenu() {
  contextMenu.value.visible = false;
}

function closeEdgeMenu() {
  edgeMenu.value.visible = false;
}

function closeMenus() {
  closeContextMenu();
  closeEdgeMenu();
}

function onGlobalKeydown(event) {
  const target = event.target;
  const isEditing = target instanceof HTMLElement
    && (target.matches("input, textarea, select") || target.isContentEditable);
  if (event.key === "Escape") {
    closeMenus();
    if (isEditing) return;           // 正在写提示词/改标题时，Esc 只退出输入
    if (historyOpen.value) { historyOpen.value = false; return; }
    if (state.selectedId) { state.selectedId = null; }
    return;
  }
  if (["Delete", "Backspace"].includes(event.key) && state.selectedEdgeId && !isEditing) {
    event.preventDefault();
    deleteSelectedEdge();
  }
}
function onPaneContextMenu(event) {
  event.preventDefault();
  state.selectedId = null;
  state.selectedEdgeId = null;
  closeEdgeMenu();
  const menuWidth = 224;
  const menuHeight = 500;
  contextMenu.value = {
    visible: true,
    x: Math.min(event.clientX, window.innerWidth - menuWidth - 8),
    y: Math.min(event.clientY, window.innerHeight - menuHeight - 8),
    flow: screenToFlowCoordinate({ x: event.clientX, y: event.clientY }),
  };
}

function onPaneClick() {
  state.selectedId = null;
  state.selectedEdgeId = null;
  closeMenus();
}

/* 双击空白画布 → 直接弹「添加到画布」（LibTV 是双击出节点菜单，同一个意思） */
function onPanelDblClick(event) {
  const pane = event.target?.closest?.(".vue-flow__pane");
  if (!pane) return;
  onPaneContextMenu(event);
}

/* 空画布起始页的 5 个胶囊：对应 LibTV 那排「图片生成 / 视频生成 / 音频生成 / 剧本生成 / 智能剪辑」。
 * 点一下就在视口中心建一张卡，不用先右键找菜单。 */
const startPills = [
  { type: "image_gen", icon: "▣", label: "图片生成" },
  { type: "video_gen", icon: "▷", label: "视频生成" },
  { type: "tts_gen", icon: "♪", label: "音频生成" },
  { type: "text", icon: "✎", label: "剧本生成" },
  { type: "timeline", icon: "⧗", label: "智能剪辑" },
];

async function createCardAtCenter(item) {
  const found = cardItems.find((card) => card.type === item.type) || { type: item.type, defaultLabel: item.label, data: {} };
  const point = screenToFlowCoordinate({
    x: window.innerWidth / 2,
    y: window.innerHeight / 2,
  });
  contextMenu.value = { visible: false, x: 0, y: 0, flow: { x: point.x - 150, y: point.y - 90 } };
  await createCard(found);
}

function onNodeClick({ node }) {
  state.selectedId = node.id;
  state.selectedEdgeId = null;
  closeMenus();
}

function onEdgeClick({ edge }) {
  state.selectedEdgeId = edge.id;
  state.selectedId = null;
  closeMenus();
}

function onEdgeContextMenu({ event, edge }) {
  event.preventDefault();
  state.selectedEdgeId = edge.id;
  state.selectedId = null;
  closeContextMenu();
  edgeMenu.value = {
    visible: true,
    x: Math.min(event.clientX, window.innerWidth - 184),
    y: Math.min(event.clientY, window.innerHeight - 62),
    edgeId: edge.id,
  };
}

async function deleteSelectedEdge(edgeId = state.selectedEdgeId) {
  if (!edgeId) return;
  try {
    await actions.deleteEdge(edgeId);
    closeEdgeMenu();
    ElMessage.success("连线已删除，两端卡片保持不变");
  } catch (err) {
    ElMessage.error(`删除失败：${err.message || err}`);
  }
}

onMounted(() => {
  window.addEventListener("click", closeMenus);
  window.addEventListener("keydown", onGlobalKeydown);
});
onBeforeUnmount(() => {
  window.removeEventListener("click", closeMenus);
  window.removeEventListener("keydown", onGlobalKeydown);
});

const meta = computed(() => `${state.canvas.nodes.length} 节点 · ${state.canvas.edges.length} 连线`);
const problems = computed(() => state.canvas.problems || []);
const topoIndex = computed(() => {
  const index = state.canvas.topo.indexOf(state.selectedId);
  return { at: index + 1, total: state.canvas.topo.length };
});

/* 单卡工作台（P0）：选中任意一张卡，右侧打开它自己的工具面板——
 * 上面是这张卡能做的事（自己写提示词 / 只生成这一张 / 换素材 / 翻历史版本），
 * 下面是它的文字内容。取代了以前右下那个"什么类型都长一样"的只读详情框。 */
const historyOpen = ref(false);
const selectedSpec = computed(() => state.canvas.spec?.[selectedNode.value?.type] || {});
watch(() => state.selectedId, () => { historyOpen.value = false; });

const timelineClips = computed(() => {
  if (selectedNode.value?.type !== "timeline") return [];
  const videos = upstreamOfSelected.value.filter((node) => mediaOf(node)?.port === "video");
  const requested = selectedNode.value.data?.clip_order || [];
  const rank = new Map(requested.map((id, index) => [id, index]));
  return [...videos].sort((a, b) => (rank.get(a.id) ?? 1e9) - (rank.get(b.id) ?? 1e9));
});

async function moveTimelineClip(index, delta) {
  const target = index + delta;
  if (target < 0 || target >= timelineClips.value.length) return;
  const ids = timelineClips.value.map((node) => node.id);
  [ids[index], ids[target]] = [ids[target], ids[index]];
  try {
    await actions.updateNode(selectedNode.value.id, { data: { clip_order: ids } });
    ElMessage.success("时间线顺序已更新");
  } catch (err) {
    ElMessage.error(`调整失败：${err.message || err}`);
  }
}

// 新建/切换/整理之后自动适应窗口。要跑两遍：一次在节点挂载后、一次等布局与媒体占位稳定后，
// 否则第二次渲染会把我们算好的缩放又冲掉。
watch(
  () => [state.canvasName, state.canvas.nodes.length],
  () => nextTick(() => {
    setTimeout(() => focusReadable(), 80);
    setTimeout(() => focusReadable(), 620);
  })
);

onConnect(async (params) => {
  try {
    await actions.connectNodes({
      source: params.source,
      sourceHandle: params.sourceHandle,
      target: params.target,
      targetHandle: params.targetHandle,
    });
    await actions.loadCanvas();
    ElMessage.success("已连接");
  } catch (err) {
    // 后端会说清"为什么不能连"（端口不存在 / 类型不匹配 / 自环）
    ElMessage.error({ message: `连线被拒绝：${err.message}`, duration: 4000 });
  }
});

async function onReload() {
  await actions.loadCanvas();
  ElMessage.success("已刷新");
}

async function onArrange() {
  const res = await actions.arrangeCanvas();
  ElMessage.success(`按拓扑重排 ${res.nodes} 个节点`);
}

async function onRun() {
  try {
    const plan = await actions.prepareGenerationReview();
    if (!plan.total) throw new Error("没有可执行的生成节点")
    const provider = state.mediaProviders.find((item) => item.id === state.mediaProvider);
    const cost = provider?.mock ? "这是 Mock 占位生成，不产生 Provider 费用。" : "这是真实 Provider，可能产生费用。";
    await ElMessageBox.confirm(
      `将运行 ${plan.ready}/${plan.total} 个节点，模型：${state.mediaModel}。${cost}`,
      "生成前需要你拍板",
      { confirmButtonText: "批准并生成", cancelButtonText: "暂不生成", type: provider?.mock ? "info" : "warning" },
    );
    const res = await actions.runCanvas({ approved: true });
    if (res.done < res.total) {
      ElMessage.warning(`生成完成 ${res.done}/${res.total}，失败的看结果条`);
    } else {
      ElMessage.success(`按拓扑序生成 ${res.done}/${res.total} 个产物`);
    }
  } catch (err) {
    if (err === "cancel" || err === "close") {
      ElMessage.info("已取消，本次没有开始生成");
    } else {
      ElMessage.error(`生成失败：${err.message || err}`);
    }
  }
}

async function onReconcile() {
  try {
    const res = await actions.reconcileMediaJob();
    ElMessage.success(`恢复完成 ${res.done}/${res.total}`);
  } catch (err) {
    ElMessage.error({ message: `无法安全恢复：${err.message || err}`, duration: 5000 });
  }
}

/* ---------- 卡片内的「尝试」与卡下提示词框 ---------- */

// 一张卡该按哪种媒体选文件：视频卡给视频、音频卡给音频，其余按图片。
function mediaKindOf(node) {
  if (["video", "video_gen"].includes(node.type)) return "video";
  if (["audio", "tts_gen"].includes(node.type)) return "audio";
  return "image";
}

function onNodeAttempt({ node, action }) {
  if (action === "upload") requestMediaUpload(mediaKindOf(node), node.id);
  else if (action === "write") state.selectedId = node.id;
  else if (action === "generate") onNodeGenerate(node);
  else if (action === "compose") onCompose();
}

function onNodeUpload(node) {
  requestMediaUpload(mediaKindOf(node), node.id);
}

// 卡片上的「详情」/「⋯」：选中即打开右侧工作台
function onNodeOpenStudio(node) {
  state.selectedId = node.id;
}

// 卡片工具条上的「历史」：选中这张卡并直接展开历史抽屉
function onNodeHistory(node) {
  state.selectedId = node.id;
  historyOpen.value = true;
}

// 文字类卡片的提示词框：直接写回这张卡（服务端会记 rendered_by=human）
async function onNodeSavePrompt({ node, text }) {
  try {
    await actions.savePrompt(node.id, text);
    ElMessage.success("已保存这段文字");
  } catch (err) {
    ElMessage.error(`保存失败：${err.message || err}`);
  }
}

// 生成类卡片的 ↑：只跑这一张，别的卡不动
async function onNodeGenerate(node) {
  try {
    await ElMessageBox.confirm(
      `只跑「${node.label}」这一张卡，模型 ${state.mediaModel}；其它卡片不受影响。`,
      "单卡生成",
      { confirmButtonText: "开始生成", cancelButtonText: "先不跑", type: "info" },
    );
  } catch {
    return;
  }
  try {
    const res = await actions.runCanvas({ approved: true, only: [node.id] });
    if (res.done < res.total) ElMessage.warning(`生成完成 ${res.done}/${res.total}`);
    else ElMessage.success("这一张生成完成，历史里多了一版");
    await actions.loadCanvas();
  } catch (err) {
    ElMessage.error({ message: `生成失败：${err.message || err}`, duration: 5000 });
  }
}

async function onCompose() {
  try {
    const plan = await actions.prepareCompositionReview();
    if (!plan.total) throw new Error("没有时间线节点，请先添加时间线并连接视频片段");
    if (!plan.ffmpeg?.available) throw new Error(plan.ffmpeg?.reason || "FFmpeg 不可用");
    const clips = (plan.jobs || []).reduce((sum, item) => sum + (item.clips?.length || 0), 0);
    const details = (plan.jobs || []).map((item) => {
      const output = item.output || {};
      const extras = [item.audio || item.tts ? "含音频" : "无音频", item.subtitle_text ? "含字幕" : "无字幕"].join("、");
      return `${item.label}：${item.clips?.length || 0} 段，${output.width || 1280}×${output.height || 720}/${output.fps || 24}fps，${extras}`;
    }).join("\n");
    await ElMessageBox.confirm(
      `将把 ${clips} 个视频片段合成为 ${plan.ready}/${plan.total} 个成片。\n${details}\n片段顺序、字幕、音频和输出规格会在本次审批中冻结。`,
      "合成成片前需要你拍板",
      { confirmButtonText: "批准并合成", cancelButtonText: "暂不合成", type: "warning" },
    );
    const result = await actions.composeCanvas();
    if (result.done < result.total) ElMessage.warning(`合成完成 ${result.done}/${result.total}`);
    else ElMessage.success(`已生成 ${result.done} 个可播放成片`);
  } catch (err) {
    if (err === "cancel" || err === "close") ElMessage.info("已取消，本次没有开始合成");
    else ElMessage.error({ message: `合成失败：${err.message || err}`, duration: 5000 });
  }
}

async function onExport() {
  try {
    const data = await actions.exportPrompts();
    ElMessage.success(`已导出 ${data.segments} 个片段（${data.chars} 字）`);
  } catch (err) {
    ElMessage.error(`导出失败：${err.message}`);
  }
}

function requestMediaUpload(kind, nodeId = "", position = null) {
  replaceMediaNodeId.value = nodeId;
  mediaUploadPosition.value = position;
  const input = kind === "image" ? imageFileInput.value : (kind === "video" ? videoFileInput.value : audioFileInput.value);
  input?.click();
}

async function createCard(item) {
  const position = { ...contextMenu.value.flow };
  closeContextMenu();
  try {
    await actions.createNode({
      type: item.type,
      label: item.defaultLabel,
      position,
      data: item.data || {},
    });
    ElMessage.success(`已添加${item.label}`);
  } catch (err) {
    ElMessage.error(`添加失败：${err.message || err}`);
  }
}

function uploadFromContext(kind) {
  const position = { ...contextMenu.value.flow };
  closeContextMenu();
  requestMediaUpload(kind, "", position);
}

async function onMediaFile(event, kind) {
  const input = event.target;
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  if (file.size > 100 * 1024 * 1024) {
    ElMessage.error("文件不能超过 100 MB");
    return;
  }
  try {
    const result = await actions.uploadMedia(file, kind, replaceMediaNodeId.value, mediaUploadPosition.value);
    const label = { image: "图片", video: "视频", audio: "音频" }[kind] || kind;
    ElMessage.success(`${result.replaced ? "已替换" : "已添加"}${label}素材`);
    await nextTick();
    fitView({ padding: 0.18 });
  } catch (err) {
    ElMessage.error(`上传失败：${err.message || err}`);
  } finally {
    replaceMediaNodeId.value = "";
    mediaUploadPosition.value = null;
  }
}
</script>

<template>
  <div class="canvas-panel" :class="lodClass" @dblclick="onPanelDblClick">
    <VueFlow
      :id="FLOW_ID"
      :nodes="flowNodes"
      :edges="flowEdges"
      :min-zoom="0.12"
      :max-zoom="2.5"
      :snap-to-grid="true"
      :snap-grid="[16, 16]"
      :delete-key-code="null"
      :zoom-on-double-click="false"
      :fit-view-on-init="false"
      :only-render-visible-elements="true"
      :default-edge-options="{ type: 'default', animated: false, pathOptions: { curvature: 0.32 } }"
      @node-click="onNodeClick"
      @edge-click="onEdgeClick"
      @edge-context-menu="onEdgeContextMenu"
      @pane-click="onPaneClick"
      @pane-context-menu="onPaneContextMenu"
      @node-drag-stop="({ node }) => actions.moveNode({ id: node.id, position: node.position }).catch(() => {})"
    >
      <Background :gap="22" pattern-color="var(--grid-dot)" />
      <!-- 缩略图放右下、抬到工具条与 Agent 悬浮球上方：左侧现在被画布目录占着 -->
      <MiniMap
        v-if="state.canvas.nodes.length"
        pannable
        zoomable
        position="bottom-right"
        :style="{ bottom: '152px', right: '20px' }"
        :node-color="'rgba(150,185,240,0.34)'"
        :mask-color="'rgba(6,9,14,0.62)'"
      />
      <template #node-toon="nodeProps">
        <ToonNode
          :data="{ ...nodeProps.data, model: state.mediaModel, selected: nodeProps.selected }"
          @attempt="onNodeAttempt"
          @save-prompt="onNodeSavePrompt"
          @generate="onNodeGenerate"
          @upload="onNodeUpload"
          @open-studio="onNodeOpenStudio"
          @history="onNodeHistory"
        />
      </template>
    </VueFlow>

    <!-- 左侧目录：画布 / 资产（LibTV 式） -->
    <CanvasSidebar />

    <!-- 空画布：照着 LibTV 的起始页给「双击画布」+ 五个胶囊，而不是一整块提示文字 -->
    <div v-if="!state.canvas.nodes.length" class="canvas-hero">
      <p class="canvas-hero-hint">
        <span class="canvas-hero-cursor" aria-hidden="true">▷</span>双击画布 自由生成节点
      </p>
      <div class="canvas-hero-pills">
        <button
          v-for="pill in startPills"
          :key="pill.type"
          type="button"
          @click.stop="createCardAtCenter(pill)"
        >
          <span class="hero-pill-glyph" aria-hidden="true">{{ pill.icon }}</span>
          {{ pill.label }}
        </button>
      </div>
    </div>

    <div
      v-if="contextMenu.visible"
      class="canvas-context-menu"
      :style="{ left: `${contextMenu.x}px`, top: `${contextMenu.y}px` }"
      role="menu"
      aria-label="添加画布卡片"
      @click.stop
      @contextmenu.prevent
    >
      <div class="context-menu-title">添加到画布</div>
      <button v-for="item in cardItems" :key="item.type" type="button" role="menuitem" @click="createCard(item)">
        <span class="context-icon">{{ item.icon }}</span>
        <span>{{ item.label }}</span>
      </button>
      <div class="context-menu-separator" />
      <button type="button" role="menuitem" @click="uploadFromContext('image')">
        <span class="context-icon media">图</span><span>上传图片素材</span>
      </button>
      <button type="button" role="menuitem" @click="uploadFromContext('video')">
        <span class="context-icon video">影</span><span>上传视频素材</span>
      </button>
      <button type="button" role="menuitem" @click="uploadFromContext('audio')">
        <span class="context-icon media">声</span><span>上传音频素材</span>
      </button>
    </div>

    <div
      v-if="edgeMenu.visible"
      class="canvas-context-menu edge-context-menu"
      :style="{ left: `${edgeMenu.x}px`, top: `${edgeMenu.y}px` }"
      role="menu"
      aria-label="连线操作"
      @click.stop
      @contextmenu.prevent
    >
      <button type="button" class="danger-menu-item" role="menuitem" @click="deleteSelectedEdge(edgeMenu.edgeId)">
        <span class="context-icon danger">删</span><span>删除这条连线</span>
      </button>
    </div>

    <div v-if="state.canvasActivity" class="canvas-agent-activity" :class="state.canvasActivity.phase">
      <span class="pulse" />
      <div>
        <b>{{ state.canvasActivity.phase === "running" ? "Agent 正在修改画布" : "画布已同步" }}</b>
        <span>{{ state.canvasActivity.summary }}</span>
      </div>
    </div>

    <!-- 左下：画布操作 -->
    <div class="canvas-controls">
      <el-button
        :loading="state.canvasBusy"
        title="一键生成：按拓扑序跑所有生成节点"
        @click="onRun"
      >
        <el-icon><VideoPlay /></el-icon>
      </el-button>
      <el-button title="导出全片提示词" @click="onExport">
        <el-icon><Download /></el-icon>
      </el-button>
      <el-button :loading="state.mediaUploading" title="上传图片素材" @click="requestMediaUpload('image')">
        <el-icon><Picture /></el-icon>
      </el-button>
      <el-button :loading="state.mediaUploading" title="上传视频素材" @click="requestMediaUpload('video')">
        <el-icon><VideoCamera /></el-icon>
      </el-button>
      <el-button :loading="state.canvasBusy" title="把时间线合成为最终 MP4" @click="onCompose">
        <span>成片</span>
      </el-button>
      <el-button title="按依赖整理成流程图" @click="onArrange">
        <el-icon><Grid /></el-icon>
      </el-button>
      <el-button title="适应窗口" @click="fitReadable()">
        <el-icon><FullScreen /></el-icon>
      </el-button>
      <el-button title="刷新画布" @click="onReload">
        <el-icon><Refresh /></el-icon>
      </el-button>
      <el-button
        v-if="state.selectedEdgeId"
        type="danger"
        plain
        title="删除选中的连线（Delete / Backspace）"
        aria-label="删除选中的连线"
        @click="deleteSelectedEdge()"
      >
        <el-icon><Delete /></el-icon>
      </el-button>
      <span class="ctl-divider" aria-hidden="true" />
      <button type="button" class="ctl-zoom" title="缩小（Ctrl + 滚轮也行）" aria-label="缩小" @click="zoomOut({ duration: 120 })">−</button>
      <span class="ctl-zoom-val" title="当前缩放">{{ zoomPct }}</span>
      <button type="button" class="ctl-zoom" title="放大（Ctrl + 滚轮也行）" aria-label="放大" @click="zoomIn({ duration: 120 })">+</button>
      <span class="ctl-meta">{{ meta }}</span>
      <input
        ref="imageFileInput"
        class="media-file-input"
        type="file"
        accept="image/*,.avif"
        aria-label="选择图片素材"
        @change="onMediaFile($event, 'image')"
      />
      <input
        ref="videoFileInput"
        class="media-file-input"
        type="file"
        accept="video/*,.mkv"
        aria-label="选择视频素材"
        @change="onMediaFile($event, 'video')"
      />
      <input
        ref="audioFileInput"
        class="media-file-input"
        type="file"
        accept="audio/*,.m4a,.aac,.flac"
        aria-label="选择音频素材"
        @change="onMediaFile($event, 'audio')"
      />
    </div>

    <!-- 一键生成的结果 -->
    <div v-if="state.runResult" class="problems-bar run-bar">
      <div>
        <template v-if="['queued', 'running'].includes(state.runResult.status)">
          {{ state.runResult.status === 'queued' ? '等待 Worker…' : '后台生成中…' }}
        </template>
        <template v-else>
          生成完成 <b>{{ state.runResult.done }}/{{ state.runResult.total }}</b>
        </template>
        <el-button link type="primary" @click="state.runResult = null">关闭</el-button>
        <el-button
          v-if="state.runResult.status === 'unknown'"
          link
          type="warning"
          :loading="state.canvasBusy"
          @click="onReconcile"
        >核对并恢复</el-button>
      </div>
      <div v-if="state.runResult.error" class="err">{{ state.runResult.error }}</div>
      <ul>
        <li v-for="item in state.runResult.results" :key="item.node_id">
          {{ item.ok ? "✓" : "✗" }} {{ item.label }} {{ item.path || item.error }}
        </li>
      </ul>
    </div>

    <!-- 体检问题：默认收成一颗胶囊，点开才铺开（以前是一整块盖在画布上） -->
    <div v-if="problems.length" class="problems-chip" :class="{ open: problemsOpen }" :style="state.runResult ? 'bottom: 190px' : ''">
      <button type="button" class="problems-chip-head" :aria-expanded="problemsOpen" @click="problemsOpen = !problemsOpen">
        <span class="dot" aria-hidden="true" />
        体检 {{ problems.length }} 项待修
        <el-icon><ArrowUp v-if="problemsOpen" /><ArrowDown v-else /></el-icon>
      </button>
      <ul v-if="problemsOpen">
        <li v-for="(item, index) in problems" :key="index">{{ item }}</li>
      </ul>
    </div>

    <!-- 右侧：单卡工作台（P0）——每张卡自己的工具、正文、历史版本 -->
    <NodeStudio
      v-if="selectedNode"
      :node="selectedNode"
      :spec="selectedSpec"
      :upstream="upstreamOfSelected"
      :order="topoIndex"
      @close="state.selectedId = null"
      @history="historyOpen = true"
      @upload="(kind) => requestMediaUpload(kind, selectedNode.id)"
    >
      <template v-if="selectedNode.type === 'timeline'" #extra>
        <section class="studio-extra">
          <b>片段顺序</b>
          <p v-if="!timelineClips.length" class="studio-extra-dim">
            请把视频素材或视频生成节点连接到 timeline 的 video 输入。
          </p>
          <ol v-else class="timeline-list">
            <li v-for="(clip, index) in timelineClips" :key="clip.id">
              <span>{{ clip.label }}</span>
              <span class="timeline-actions">
                <el-button link :disabled="index === 0" @click="moveTimelineClip(index, -1)">上移</el-button>
                <el-button link :disabled="index === timelineClips.length - 1" @click="moveTimelineClip(index, 1)">下移</el-button>
              </span>
            </li>
          </ol>
          <p class="studio-extra-dim">
            输出：{{ selectedNode.data?.output?.width || 1280 }}×{{ selectedNode.data?.output?.height || 720 }} /
            {{ selectedNode.data?.output?.fps || 24 }}fps
          </p>
        </section>
      </template>
    </NodeStudio>

    <GenerationHistoryDrawer :visible="historyOpen" :node="selectedNode" @close="historyOpen = false" />
  </div>
</template>

<style scoped>
.studio-extra { display: flex; flex-direction: column; gap: 6px; }
.studio-extra b { font-size: 11px; color: var(--text-2); letter-spacing: 0.03em; }
.studio-extra-dim { margin: 0; color: var(--text-3); font-size: 11px; }

.run-bar { color: var(--text-2); }
.run-bar b { color: var(--text); }
.timeline-list { margin: 0; padding-left: 20px; }
.timeline-list li { margin: 5px 0; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.timeline-actions { display: flex; flex-shrink: 0; }
</style>
