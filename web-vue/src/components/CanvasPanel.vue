<script setup>
/* 画布页：Vue Flow 负责视口/拖拽/连线交互，我们只提供节点组件与工具栏。
 *
 * 关键取舍：连线**交给后端校验**（POST /api/canvas/connect 会检查端口类型是否兼容），
 * 前端不做重复实现 —— 类型规则只有一份，改的时候不会两边不一致。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { VueFlow, useVueFlow } from "@vue-flow/core";
import { Background } from "@vue-flow/background";
import { Controls } from "@vue-flow/controls";
import { MiniMap } from "@vue-flow/minimap";
import { ElMessage, ElMessageBox } from "element-plus";
import ToonNode from "./ToonNode.vue";
import { actions, flowEdges, flowNodes, state, selectedNode, upstreamOfSelected } from "../store.js";
import { assetUrl, mediaOf } from "../utils.js";

const { fitView, onConnect, screenToFlowCoordinate } = useVueFlow();
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
  if (event.key === "Escape") {
    closeMenus();
    return;
  }
  const target = event.target;
  const isEditing = target instanceof HTMLElement
    && (target.matches("input, textarea, select") || target.isContentEditable);
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

const detailMedia = computed(() => mediaOf(selectedNode.value));
const detailMediaSrc = computed(() => assetUrl(detailMedia.value?.path));
const detailText = computed(() => {
  if (typeof selectedNode.value?.content === "string" && selectedNode.value.content) return selectedNode.value.content;
  const outputs = Object.values(selectedNode.value?.outputs || {});
  return outputs.find((item) => item?.data_type === "STRING" && typeof item.value === "string")?.value || "";
});

const detailData = computed(() => {
  const data = selectedNode.value?.data || {};
  return Object.entries(data).map(([key, value]) => [
    key,
    typeof value === "object" ? JSON.stringify(value) : String(value),
  ]);
});

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

// 新建/切换/整理之后自动适应窗口（布局未稳定时等一帧）
watch(
  () => [state.canvasName, state.canvas.nodes.length],
  () => nextTick(() => setTimeout(() => fitView({ padding: 0.18 }), 60))
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
  <div class="canvas-panel">
    <VueFlow
      :nodes="flowNodes"
      :edges="flowEdges"
      :min-zoom="0.2"
      :max-zoom="2.5"
      :snap-to-grid="true"
      :snap-grid="[16, 16]"
      :delete-key-code="null"
      :zoom-on-double-click="false"
      :only-render-visible-elements="false"
      :default-edge-options="{ type: 'smoothstep', animated: false }"
      @node-click="onNodeClick"
      @edge-click="onEdgeClick"
      @edge-context-menu="onEdgeContextMenu"
      @pane-click="onPaneClick"
      @pane-context-menu="onPaneContextMenu"
      @node-drag-stop="({ node }) => actions.moveNode({ id: node.id, position: node.position }).catch(() => {})"
    >
      <Background :gap="16" pattern-color="var(--grid-dot)" />
      <Controls position="bottom-right" />
      <!-- 缩略图放左下、抬到工具条上方（Toonflow 也是这个位置：bottom:64px） -->
      <MiniMap
        pannable
        zoomable
        position="bottom-left"
        :style="{ bottom: '78px' }"
        :node-color="'rgba(255,255,255,0.22)'"
        :mask-color="'rgba(8,12,24,0.55)'"
      />
      <template #node-toon="nodeProps">
        <ToonNode :data="nodeProps.data" />
      </template>
    </VueFlow>

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
      <el-button title="适应窗口" @click="fitView({ padding: 0.18 })">
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

    <!-- 体检问题 -->
    <div v-if="problems.length" class="problems-bar" :style="state.runResult ? 'bottom: 200px' : ''">
      <b>体检发现 {{ problems.length }} 个问题</b>
      <ul>
        <li v-for="(item, index) in problems.slice(0, 5)" :key="index">{{ item }}</li>
      </ul>
    </div>

    <!-- 右下：节点详情 -->
    <aside v-if="selectedNode" class="node-detail">
      <h3>{{ selectedNode.label }}</h3>
      <table class="detail-table">
        <tr>
          <td>id</td>
          <td>{{ selectedNode.id }}</td>
        </tr>
        <tr>
          <td>类型</td>
          <td>{{ selectedNode.type }}</td>
        </tr>
        <tr>
          <td>状态</td>
          <td>{{ selectedNode.status }}</td>
        </tr>
        <tr v-if="topoIndex.total">
          <td>执行序</td>
          <td>第 {{ topoIndex.at }} / {{ topoIndex.total }} 步</td>
        </tr>
      </table>

      <template v-if="selectedNode.type === 'timeline'">
        <h4>片段顺序</h4>
        <p v-if="!timelineClips.length" class="dim">请把视频素材或视频生成节点连接到 timeline 的 video 输入。</p>
        <ol v-else class="timeline-list">
          <li v-for="(clip, index) in timelineClips" :key="clip.id">
            <span>{{ clip.label }}</span>
            <span class="timeline-actions">
              <el-button link :disabled="index === 0" @click="moveTimelineClip(index, -1)">上移</el-button>
              <el-button link :disabled="index === timelineClips.length - 1" @click="moveTimelineClip(index, 1)">下移</el-button>
            </span>
          </li>
        </ol>
        <p class="dim">输出：{{ selectedNode.data?.output?.width || 1280 }}×{{ selectedNode.data?.output?.height || 720 }} / {{ selectedNode.data?.output?.fps || 24 }}fps</p>
      </template>

      <template v-if="detailMedia">
        <h4>产物{{ detailMedia.mock ? "（mock 占位）" : "" }}</h4>
        <video
          v-if="detailMedia.port === 'video'"
          class="detail-media"
          :src="detailMediaSrc"
          controls
          preload="metadata"
        />
        <audio
          v-else-if="detailMedia.port === 'audio'"
          class="detail-audio"
          :src="detailMediaSrc"
          controls
          preload="metadata"
        />
        <a v-else :href="detailMediaSrc" target="_blank">
          <img class="detail-media" :src="detailMediaSrc" :alt="selectedNode.label" />
        </a>
        <p class="dim">{{ detailMedia.path }}</p>
        <el-button
          v-if="['image', 'video', 'audio'].includes(selectedNode.type)"
          size="small"
          :loading="state.mediaUploading"
          @click="requestMediaUpload(selectedNode.type, selectedNode.id)"
        >替换{{ { image: "图片", video: "视频", audio: "音频" }[selectedNode.type] }}</el-button>
        <div v-if="selectedNode.type === 'image'" class="connection-guide">
          <b>怎么连接</b>
          从图片卡右侧橙色圆点拖出，连接到资产卡的 <code>image</code>，或提示词/生成卡的 <code>ref</code>。
        </div>
        <div v-else-if="selectedNode.type === 'video'" class="connection-guide">
          <b>怎么连接</b>
          从视频卡右侧紫色圆点拖出，连接到提示词或视频生成卡的 <code>video_ref</code>。
        </div>
      </template>

      <template v-if="selectedNode.data?.content_path">
        <h4>正文</h4>
        <p class="dim">{{ selectedNode.data.content_path }}</p>
        <pre v-if="detailText" class="detail-content">{{ detailText }}</pre>
      </template>

      <template v-if="detailData.length">
        <h4>data</h4>
        <pre>{{ detailData.map(([k, v]) => `${k}: ${v}`).join("\n") }}</pre>
      </template>

      <template v-if="upstreamOfSelected.length">
        <h4>上游依赖</h4>
        <ul>
          <li v-for="node in upstreamOfSelected" :key="node.id">{{ node.label }}</li>
        </ul>
      </template>
    </aside>
  </div>
</template>

<style scoped>
.run-bar { color: var(--text-2); }
.run-bar b { color: var(--text); }
.timeline-list { margin: 0; padding-left: 20px; }
.timeline-list li { margin: 5px 0; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.timeline-actions { display: flex; flex-shrink: 0; }
</style>
