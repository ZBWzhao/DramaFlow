<script setup>
/* 单卡工作台（P0）。
 *
 * 参考 AICON 的 CanvasImageStudio/CanvasVideoStudio/CanvasTextStudio（Apache-2.0）的
 * **交互形态**——上面是这张卡能用的工具，下面是它的提示词/正文，中间是产物；
 * 代码是按我们自己的数据模型重写的：
 *   · 文案标题 → node.label（PATCH /api/canvas/nodes/{id}）
 *   · 提示词 → node.content / data.prompt（POST /api/studio/segments/{id}/prompt，会打 rendered_by=human）
 *   · 生成   → /api/canvas/run 的 only=[这张卡]（服务端早就支持，画布这层以前没给入口）
 *   · 历史   → node.data.takes（服务端 studio 模块既有字段）
 *
 * 这里刻意**不做**"和 AI 对话改这张卡"（那是 P1 的节点级 Agent），也没有做删除
 * （服务端还没有节点级 DELETE），避免造出一堆点了会报错的新按钮。
 */
import { computed, nextTick, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { actions, state } from "../store.js";
import { assetUrl, mediaOf } from "../utils.js";

const props = defineProps({
  node: { type: Object, required: true },
  spec: { type: Object, default: () => ({}) },
  upstream: { type: Array, default: () => [] },
  order: { type: Object, default: () => ({ at: 0, total: 0 }) },
});
const emit = defineEmits(["close", "history", "upload"]);

const shortType = computed(() => String(props.spec?.label || props.node.type).split("（")[0]);
const media = computed(() => mediaOf(props.node));
const mediaSrc = computed(() => assetUrl(media.value?.path));
const isGenerator = computed(() => ["image_gen", "video_gen"].includes(props.node.type));
const isPromptNode = computed(() => props.node.type === "prompt");
/* 文字类卡片才看正文/提示词；纯图/视频卡没有"内容"这一节，别硬塞一句"还没有文字内容"。 */
const showsText = computed(() => ["prompt", "text", "storyboard"].includes(props.node.type));
/* 能上传替换的只有放媒体/产物的卡；提示词卡、分镜卡放不了图。 */
const canUpload = computed(() => ["asset", "image", "video", "audio", "image_gen", "video_gen", "tts_gen"].includes(props.node.type));
const wantsImage = computed(() => ["asset", "image", "image_gen"].includes(props.node.type));
const wantsVideo = computed(() => ["video", "video_gen"].includes(props.node.type));
const wantsAudio = computed(() => ["audio", "tts_gen"].includes(props.node.type));

/* 产物/正文。content.md 的内容由后端在读画布时带回来（只对 content_path 节点）；
 * 没有文件的节点退回到 data.prompt / outputs.prompt 的内联文本。 */
const bodyText = computed(() => {
  const node = props.node;
  if (typeof node.content === "string" && node.content.trim()) return node.content;
  if (typeof node.data?.prompt === "string" && node.data.prompt.trim()) return node.data.prompt;
  const outputs = Object.values(node.outputs || {});
  return outputs.find((item) => item?.data_type === "STRING" && typeof item.value === "string")?.value || "";
});

/* 生成需要上游给一段提示词：自己就是提示词卡时用自己的，否则取最近的上游文本。 */
const upstreamPrompt = computed(() => {
  if (bodyText.value.trim() && (isPromptNode.value || props.node.type === "text")) return bodyText.value;
  for (const node of props.upstream) {
    const text = node.content
      || node.data?.prompt
      || Object.values(node.outputs || {}).find((item) => item?.data_type === "STRING")?.value
      || "";
    if (String(text).trim()) return String(text);
  }
  return "";
});
const canGenerate = computed(() => isGenerator.value && !!upstreamPrompt.value.trim());
const generating = computed(() => props.node.status === "generating" || (state.canvasBusy && isGenerator.value));

const takes = computed(() => (Array.isArray(props.node.data?.takes) ? props.node.data.takes : []));
const currentTakeId = computed(() => props.node.data?.current_take_id || "");

const lifecycleLabels = {
  draft: "创建中",
  generating: "生成中",
  ready: "待确认",
  done: "已完成",
  error: "生成失败",
};
const statusLabel = computed(() => lifecycleLabels[props.node.status] || "创建中");

const status = ref("");
const titleDraft = ref(props.node.label || "");
watch(() => props.node.id, () => {
  titleDraft.value = props.node.label || "";
  status.value = "";
  promptDraft.value = bodyText.value;
});
watch(() => props.node.label, (value) => {
  if (!titleEditing.value) titleDraft.value = value || "";
});

const titleEditing = ref(false);
async function commitTitle() {
  titleEditing.value = false;
  const next = titleDraft.value.trim();
  if (!next || next === props.node.label) {
    titleDraft.value = props.node.label || "";
    return;
  }
  status.value = "正在保存标题…";
  try {
    await actions.updateNode(props.node.id, { label: next });
    status.value = "标题已保存";
  } catch (err) {
    status.value = "";
    titleDraft.value = props.node.label || "";
    ElMessage.error(`保存标题失败：${err.message || err}`);
  }
}

const promptDraft = ref(bodyText.value);
const promptEditing = ref(false);
watch(() => props.node.id + "|" + props.node.status, () => {
  if (!promptEditing.value) promptDraft.value = bodyText.value;
});
const promptDirty = computed(() => promptEditing.value && promptDraft.value !== bodyText.value);

async function savePromptText() {
  if (!isPromptNode.value) return;
  status.value = "正在保存提示词…";
  try {
    await actions.savePrompt(props.node.id, promptDraft.value);
    promptEditing.value = false;
    status.value = "提示词已保存（人写的不会被 Agent 覆盖）";
    ElMessage.success("提示词已保存");
  } catch (err) {
    status.value = "";
    ElMessage.error(`保存失败：${err.message || err}`);
  }
}

async function generateThis() {
  if (!canGenerate.value) return;
  const provider = state.mediaProviders.find((item) => item.id === state.mediaProvider);
  const cost = provider?.mock ? "Mock 占位生成，不产生 Provider 费用。" : "真实 Provider，可能产生费用。";
  try {
    await ElMessageBox.confirm(
      `只跑「${props.node.label}」这一张卡，模型：${state.mediaModel}。${cost}`,
      "单卡重新生成",
      { confirmButtonText: "开始生成", cancelButtonText: "先不跑", type: provider?.mock ? "info" : "warning" },
    );
  } catch {
    return;
  }
  status.value = "已提交，正在生成这一张…";
  try {
    const res = await actions.runCanvas({ approved: true, only: [props.node.id] });
    // 生成链路现在会把每一次结果追加进 node.data.takes（服务端 _execute_media_job 写回），
    // 所以跑完要重新读一次画布，历史按钮上的版本数才会从 0 变成 1、2、3。
    await actions.loadCanvas();
    if (res.done < res.total) ElMessage.warning(`生成完成 ${res.done}/${res.total}`);
    else ElMessage.success("这一张生成完成，历史里多了一版");
    status.value = "已生成，可以在历史里对比版本";
  } catch (err) {
    status.value = "";
    ElMessage.error({ message: `生成失败：${err.message || err}`, duration: 5000 });
  }
}

const detailsOpen = ref(false);

/* ---- 单卡对话 ----
 * 请求带 focus_node，服务端只给「读画布/改这张卡/写文字/导出」的工具；
 * 每个节点一条独立会话（见 store.js 的 askNode），不掺进画布那条长历史。
 */
const chatKey = computed(() => `${state.canvasName}:${props.node.id}`);
const chat = computed(() => state.nodeChats[chatKey.value] || { messages: [], running: false, sessionId: "", error: "" });
const chatDraft = ref("");
const chatList = ref(null);

async function sendToAgent() {
  const text = chatDraft.value.trim();
  if (!text || chat.value.running) return;
  chatDraft.value = "";
  await actions.askNode(props.node, text);
}

watch(
  () => chat.value.messages.length,
  async () => {
    await nextTick();
    if (chatList.value) chatList.value.scrollTop = chatList.value.scrollHeight;
  },
);

const detailData = computed(() => Object.entries(props.node.data || {}).map(([key, value]) => [
  key,
  typeof value === "object" ? JSON.stringify(value).slice(0, 200) : String(value),
]));
const mediaKind = computed(() => (media.value?.port === "video" ? "video" : media.value?.port === "audio" ? "audio" : "image"));
const replaceKind = computed(() => (mediaKind.value === "audio" ? "audio" : mediaKind.value));
</script>

<template>
  <aside class="node-studio" aria-label="节点工作台">
    <header class="studio-head">
      <div class="studio-head-main">
        <span class="studio-type">{{ shortType }}</span>
        <input
          v-if="titleEditing"
          v-model="titleDraft"
          class="studio-title-input"
          autofocus
          @keyup.enter="commitTitle"
          @keyup.esc="titleEditing = false"
          @blur="commitTitle"
        />
        <button v-else type="button" class="studio-title" :title="'点击重命名：' + node.label" @click="titleEditing = true">
          {{ node.label }}
        </button>
      </div>
      <button type="button" class="studio-close" title="关闭（Esc）" aria-label="关闭工作台" @click="emit('close')">×</button>
    </header>

    <!-- 产物 + 生成中遮罩（纯文字卡不占这块地方） -->
    <div v-if="media || isGenerator || canUpload" class="studio-stage" :class="{ 'is-generating': generating }">
      <template v-if="media">
        <video v-if="media.port === 'video'" class="studio-media" :src="mediaSrc" controls preload="metadata" />
        <audio v-else-if="media.port === 'audio'" class="studio-audio" :src="mediaSrc" controls preload="metadata" />
        <img v-else class="studio-media" :src="mediaSrc" :alt="node.label" />
        <span v-if="media.mock" class="studio-mock">mock 占位</span>
      </template>
      <div v-else class="studio-stage-empty">
        <b>还没有产物</b>
        <span v-if="wantsImage">可以自己上传一张，或者点「生成」（需要上游有提示词）</span>
        <span v-else-if="wantsVideo">自己上传一段，或者点「生成」让它按提示词跑</span>
        <span v-else-if="wantsAudio">自己上传音频，或用旁白生成</span>
        <span v-else>这张卡是文字类，内容在下面</span>
      </div>

      <div v-if="generating" class="studio-overlay">
        <div class="studio-overlay-dots"><i /><i /><i /></div>
        <b>AI 正在生成这一张</b>
        <div class="studio-overlay-track"><span /></div>
        <span class="studio-overlay-hint">只跑这一个节点，其它卡片不受影响</span>
      </div>
    </div>

    <!-- 工具条：这张卡能做的事 -->
    <div class="studio-tools">
      <el-button v-if="isGenerator" size="small" type="primary" :disabled="!canGenerate" :loading="generating" @click="generateThis">
        生成
      </el-button>
      <el-button v-if="canUpload" size="small" @click="emit('upload', replaceKind)">
        {{ media ? "换一张" : "上传" }}
      </el-button>
      <el-button v-if="takes.length" size="small" @click="emit('history')">
        历史 {{ takes.length }}
      </el-button>
      <el-button v-else-if="isGenerator" size="small" disabled title="还没生成过">历史 0</el-button>
    </div>

    <p v-if="isGenerator && !canGenerate" class="studio-warn">
      还没有可用的提示词：把一张提示词卡连到这张卡的 <code>prompt</code> 输入，或直接在提示词卡里写一段。
    </p>

    <!-- 正文：编辑入口已经搬到卡片下方（选中卡片就在它下面写），
         这里只留一屏只读预览，方便卡里字被看小的时候对照。 -->
    <section v-if="bodyText" class="studio-section">
      <div class="studio-section-head">
        <b>{{ isPromptNode ? "提示词" : "正文" }}</b>
        <span class="studio-readonly-tag">在卡片下方编辑</span>
      </div>
      <pre class="studio-body">{{ bodyText }}</pre>
    </section>

    <p v-if="status" class="studio-status">{{ status }}</p>

    <!-- 各类型自己的额外操作（比如时间线的片段排序），由画布页填 -->
    <slot name="extra" />

    <!-- 对话：只服务这张卡。上面是它能用的工具，这里是跟它说话 -->
    <section class="studio-section studio-chat">
      <div class="studio-section-head">
        <b>跟 Agent 说这一张卡</b>
        <span v-if="chat.running" class="studio-chat-live">改着呢…</span>
      </div>
      <div v-if="chat.messages.length" ref="chatList" class="studio-chat-list">
        <div v-for="(msg, index) in chat.messages" :key="index" class="studio-chat-msg" :class="msg.role">
          <span class="studio-chat-who">{{ msg.role === "user" ? "我" : "Agent" }}</span>
          <p class="studio-chat-text">{{ msg.text || (msg.streaming ? "…" : "（没有正文）") }}</p>
        </div>
      </div>
      <p v-else class="studio-empty-text">
        只改这张卡：比如「景别改成中景，把光线写暖一点」「这句台词太长，压到 12 字」。
      </p>
      <div class="studio-chat-input">
        <textarea
          v-model="chatDraft"
          class="studio-textarea"
          rows="2"
          placeholder="只对这一张卡提要求，回车发送（Shift+回车换行）"
          @keydown.enter.exact.prevent="sendToAgent"
        />
        <el-button size="small" type="primary" :disabled="!chatDraft.trim() || chat.running" :loading="chat.running" @click="sendToAgent">
          发送
        </el-button>
      </div>
    </section>

    <!-- 技术详情：以前画布右下角那个面板的全部信息，收起来不占地方 -->
    <section class="studio-section studio-details">
      <button type="button" class="studio-section-head as-button" @click="detailsOpen = !detailsOpen">
        <b>技术详情</b>
        <span class="studio-dot">{{ detailsOpen ? "−" : "+" }}</span>
      </button>
      <template v-if="detailsOpen">
        <table class="studio-table">
          <tr><td>id</td><td>{{ node.id }}</td></tr>
          <tr><td>类型</td><td>{{ node.type }}</td></tr>
          <tr><td>状态</td><td>{{ statusLabel }}</td></tr>
          <tr v-if="order.total"><td>执行序</td><td>第 {{ order.at }} / {{ order.total }} 步</td></tr>
          <tr v-if="takes.length"><td>版本</td><td>{{ takes.length }} 版{{ currentTakeId ? "，已选一版" : "" }}</td></tr>
        </table>
        <template v-if="detailData.length">
          <h4>data</h4>
          <pre class="studio-data">{{ detailData.map(([k, v]) => `${k}: ${v}`).join("\n") }}</pre>
        </template>
        <template v-if="upstream.length">
          <h4>上游依赖</h4>
          <ul class="studio-upstream">
            <li v-for="item in upstream" :key="item.id">{{ item.label }}</li>
          </ul>
        </template>
      </template>
    </section>
  </aside>
</template>

<style scoped>
.node-studio {
  position: absolute;
  top: 58px;
  /* 左侧被画布目录（CanvasSidebar）占着，工作台停到右侧；
     右下角只留 Agent 悬浮球（bottom:88px），所以底部收到 78px 以上。 */
  right: 14px;
  bottom: 78px;
  width: 348px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 12px 14px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: linear-gradient(180deg, rgba(23, 28, 37, 0.97), rgba(15, 19, 26, 0.98));
  box-shadow: 0 18px 48px rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(14px);
  overflow-y: auto;
  z-index: 7;
}

.studio-head { display: flex; align-items: flex-start; gap: 8px; }
.studio-head-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.studio-type {
  align-self: flex-start;
  padding: 1px 7px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.06);
  color: var(--text-3);
  font-size: 10px;
  letter-spacing: 0.04em;
}
.studio-title {
  padding: 0;
  border: none;
  background: none;
  color: var(--text);
  font-size: 14px;
  font-weight: 600;
  text-align: left;
  cursor: text;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.studio-title-input {
  width: 100%;
  padding: 3px 6px;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  background: rgba(0, 0, 0, 0.32);
  color: var(--text);
  font-size: 13px;
}
.studio-close {
  width: 24px;
  height: 24px;
  border: none;
  border-radius: 7px;
  background: rgba(255, 255, 255, 0.06);
  color: var(--text-2);
  font-size: 15px;
  line-height: 1;
  cursor: pointer;
}
.studio-close:hover { background: rgba(255, 255, 255, 0.12); color: var(--text); }

.studio-stage {
  position: relative;
  border-radius: 10px;
  overflow: hidden;
  background: #0a0d13;
  border: 1px solid var(--line);
  min-height: 132px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.studio-media { display: block; width: 100%; max-height: 230px; object-fit: cover; }
.studio-audio { width: 100%; padding: 8px; }
.studio-mock {
  position: absolute;
  left: 8px;
  bottom: 8px;
  padding: 1px 6px;
  border-radius: 999px;
  background: rgba(8, 11, 16, 0.7);
  color: var(--text-3);
  font-size: 10px;
}
.studio-stage-empty {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 18px 16px;
  text-align: center;
  color: var(--text-3);
  font-size: 11px;
}
.studio-stage-empty b { color: var(--text-2); font-size: 12px; }

.studio-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 0 20px;
  background: rgba(8, 11, 16, 0.82);
  backdrop-filter: blur(6px);
  text-align: center;
}
.studio-overlay b { font-size: 12px; color: var(--text); }
.studio-overlay-dots { display: flex; gap: 5px; }
.studio-overlay-dots i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--primary);
  animation: studio-pulse 1.3s ease-in-out infinite;
}
.studio-overlay-dots i:nth-child(2) { animation-delay: 0.16s; }
.studio-overlay-dots i:nth-child(3) { animation-delay: 0.32s; }
@keyframes studio-pulse {
  0%, 100% { opacity: 0.3; transform: scale(0.8); }
  50% { opacity: 1; transform: scale(1.15); }
}
.studio-overlay-track {
  width: 100%;
  height: 3px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.1);
  overflow: hidden;
}
.studio-overlay-track span {
  display: block;
  width: 40%;
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(90deg, transparent, var(--primary), transparent);
  animation: studio-flow 1.6s ease-in-out infinite;
}
@keyframes studio-flow {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(260%); }
}
.studio-overlay-hint { font-size: 10px; color: var(--text-3); }

.studio-tools { display: flex; flex-wrap: wrap; gap: 6px; }
.studio-warn {
  margin: 0;
  padding: 7px 9px;
  border-radius: 8px;
  background: rgba(217, 119, 6, 0.12);
  color: #e3b072;
  font-size: 11px;
  line-height: 1.5;
}

.studio-section { display: flex; flex-direction: column; gap: 6px; }
.studio-section-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.studio-section-head b { font-size: 11px; color: var(--text-2); letter-spacing: 0.03em; }
.studio-section-head.as-button {
  width: 100%;
  padding: 0;
  border: none;
  background: none;
  cursor: pointer;
}
.studio-section-actions { display: flex; gap: 2px; }
.studio-readonly-tag { font-size: 10px; color: var(--text-3); }
.studio-dot { color: var(--text-3); font-size: 13px; }

.studio-textarea {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--line-strong);
  border-radius: 9px;
  background: rgba(0, 0, 0, 0.3);
  color: var(--text);
  font-size: 12px;
  line-height: 1.65;
  resize: vertical;
}
.studio-textarea:focus { outline: none; border-color: var(--primary); }
.studio-body {
  margin: 0;
  max-height: 190px;
  overflow-y: auto;
  padding: 8px 10px;
  border-radius: 9px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--text-2);
  font-family: inherit;
  font-size: 11.5px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.studio-empty-text { margin: 0; color: var(--text-3); font-size: 11px; }
.studio-status { margin: 0; color: #7cc4ff; font-size: 11px; }

.studio-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.studio-table td { padding: 2px 0; color: var(--text-2); vertical-align: top; }
.studio-table td:first-child { width: 60px; color: var(--text-3); }
.studio-details h4 { margin: 8px 0 3px; font-size: 10px; color: var(--text-3); font-weight: 600; }
.studio-data {
  margin: 0;
  max-height: 150px;
  overflow: auto;
  padding: 7px 9px;
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.28);
  color: var(--text-2);
  font-size: 10.5px;
  line-height: 1.6;
  white-space: pre-wrap;
}
.studio-upstream { margin: 0; padding-left: 16px; color: var(--text-2); font-size: 11px; }
</style>
