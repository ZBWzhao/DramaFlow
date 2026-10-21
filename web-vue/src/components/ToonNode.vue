<script setup>
/* 自定义节点卡 v5：把 react/vue demo 里的 LibTV 式卡面搬进真实画布。
 *
 * 和 v4 的差别只在外观与入口位置，数据契约完全没动（props.data = { node, spec, changed, selected, model }）：
 * - 卡外标题行：类型图标 + 标题 + 右侧尺寸/状态点；
 * - 卡体就是产物：16:9 满铺 + 左上「AI生成」/「mock」角标 + 右上 ⋯（打开工作台）；
 * - 没有产物：占位图标 + 卡内「尝试：」单一动作；
 * - 选中时：卡上方浮出工具条（生成/上传/历史/详情），卡下方浮出提示词框（参考/标记/风格 + 模型行 + ↑）。
 */
import { computed, ref, watch } from "vue";
import { Handle, Position } from "@vue-flow/core";
import { assetUrl, mediaOf } from "../utils.js";

const props = defineProps({
  data: { type: Object, required: true },
});
const emit = defineEmits(["attempt", "save-prompt", "generate", "upload", "open-studio", "history"]);

const node = computed(() => props.data.node);
const spec = computed(() => props.data.spec || {});
const inPorts = computed(() => Object.entries(spec.value.in || {}));
const outPorts = computed(() => Object.entries(spec.value.out || {}));
const shortType = computed(() => String(spec.value.label || node.value.type).split("（")[0]);
const selected = computed(() => !!props.data?.selected);

const media = computed(() => mediaOf(node.value));
const mediaSrc = computed(() => assetUrl(media.value?.path));
const bodyClass = computed(() => ({
  "is-media": !!media.value,
  "media-audio": media.value?.port === "audio",
  "is-empty": !media.value,
}));

// 角标：人上传的不该被标成「AI生成」
const badge = computed(() => {
  if (!media.value) return "";
  if (media.value.mock) return "mock";
  return node.value.data?.rendered_by === "human" ? "手传" : "AI生成";
});

const dims = computed(() => {
  const w = Number(node.value.data?.width || node.value.data?.w || 0);
  const h = Number(node.value.data?.height || node.value.data?.h || 0);
  if (w > 0 && h > 0) return `${w} × ${h}`;
  return shortType.value;
});

const textPreview = computed(() => {
  if (typeof node.value.content === "string" && node.value.content.trim()) return node.value.content.trim();
  const outputs = Object.values(node.value.outputs || {});
  const value = outputs.find((item) => item?.data_type === "STRING" && typeof item.value === "string")?.value;
  return value ? value.trim() : "";
});

const typeGlyph = computed(() => {
  const map = {
    prompt: "✎", text: "¶", storyboard: "▤", asset: "◈",
    image: "▣", image_gen: "▣", video: "▷", video_gen: "▷",
    audio: "♪", tts_gen: "♪", timeline: "⧗",
  };
  return map[node.value.type] || "◇";
});

/* 「尝试」= 这张卡能做的动作，按用户要求每类只留一个主动作。 */
const ATTEMPTS = {
  image_gen: [{ key: "generate", label: "生成" }],
  video_gen: [{ key: "generate", label: "生成" }],
  tts_gen: [{ key: "generate", label: "生成" }],
  asset: [{ key: "upload", label: "上传" }],
  image: [{ key: "upload", label: "上传" }],
  video: [{ key: "upload", label: "上传" }],
  audio: [{ key: "upload", label: "上传" }],
  prompt: [{ key: "write", label: "自己编写" }],
  text: [{ key: "write", label: "自己编写" }],
  storyboard: [{ key: "write", label: "自己编写" }],
  timeline: [{ key: "compose", label: "合成成片" }],
};
const attempts = computed(() => ATTEMPTS[node.value.type] || []);
const showAttempts = computed(() => !media.value && attempts.value.length > 0);

const PROMPT_TYPES = new Set(["prompt", "text", "storyboard"]);
const GEN_TYPES = new Set(["image_gen", "video_gen", "tts_gen"]);
const MEDIA_TYPES = new Set(["asset", "image", "video", "audio"]);
const showPromptBox = computed(
  () => node.value.type !== "timeline" && selected.value
    && (!!media.value || PROMPT_TYPES.has(node.value.type) || GEN_TYPES.has(node.value.type)),
);
const promptEditable = computed(() => PROMPT_TYPES.has(node.value.type));
const promptText = computed(() => {
  const own = typeof node.value.content === "string" ? node.value.content.trim() : "";
  if (own) return own;
  const stored = node.value.data?.prompt;
  return typeof stored === "string" ? stored.trim() : "";
});
const promptPlaceholder = computed(() => {
  if (GEN_TYPES.has(node.value.type)) return "提示词来自上游提示词卡；接好后点 ↑ 只跑这一张";
  if (MEDIA_TYPES.has(node.value.type)) return "可以直接上传，或接一路生成结果";
  return "写下这张卡要什么；Ctrl + 回车 保存";
});
const promptAction = computed(() => {
  if (promptEditable.value) return { key: "save-prompt", hint: "保存这段文字" };
  if (GEN_TYPES.has(node.value.type)) return { key: "generate", hint: "只跑这一张" };
  return { key: "upload", hint: "上传素材" };
});
const modelLabel = computed(() => {
  if (GEN_TYPES.has(node.value.type)) return String(props.data.model || "mock");
  if (MEDIA_TYPES.has(node.value.type)) return "素材";
  return "文本";
});
const sizeLabel = computed(() => {
  if (GEN_TYPES.has(node.value.type)) return "16:9 · 标准画质 · 2K · 1张";
  if (MEDIA_TYPES.has(node.value.type)) return "原图";
  return "正文";
});
const historyCount = computed(() => (Array.isArray(node.value.data?.takes) ? node.value.data.takes.length : 0));
const canGenerate = computed(() => GEN_TYPES.has(node.value.type));
const canUpload = computed(() => MEDIA_TYPES.has(node.value.type) || GEN_TYPES.has(node.value.type));

const lifecycleSteps = [
  { key: "draft", label: "创建" },
  { key: "generating", label: "生成" },
  { key: "ready", label: "待确认" },
  { key: "done", label: "完成" },
];
const lifecycleMap = {
  draft: { label: "创建中", index: 0, hint: "节点已创建，等待 Agent 写入内容" },
  generating: { label: "生成中", index: 1, hint: "Agent 或媒体 Provider 正在生成" },
  ready: { label: "待确认", index: 2, hint: "内容已生成，等待检查或继续制作" },
  done: { label: "已完成", index: 3, hint: "该节点的产物已经完成" },
  error: { label: "失败", index: -1, hint: "生成失败，可打开详情检查并重试" },
};
const lifecycle = computed(() => lifecycleMap[node.value.status] || lifecycleMap.draft);
const lifecycleClass = computed(() => node.value.status || "draft");
const lifecycleMessage = computed(() => {
  if (node.value.status === "error") return node.value.data?.error || "生成失败，可打开详情检查并重试";
  if (node.value.status === "generating") return textPreview.value ? "正在继续生成，当前内容已保留" : "Agent 正在写入内容";
  return "";
});

const emptyHint = computed(() => {
  const type = node.value.type;
  if (type === "image_gen") return "等待生成图片";
  if (type === "video_gen") return "等待生成视频";
  if (type === "tts_gen") return "等待生成旁白";
  if (type === "timeline") return "连接视频后合成成片";
  if (type === "asset") return "点开卡片上传素材";
  if (type === "image") return "上传参考图或接一路生成结果";
  if (type === "video") return "上传视频或接一路生成结果";
  if (type === "audio") return "上传音频或用旁白生成";
  return "还没有内容";
});

const portTop = (index) => `${22 + index * 30}px`;
const labelTop = (index) => `${19 + index * 30}px`;

const draft = ref(promptText.value);
const focused = ref(false);
watch(
  () => `${node.value.id}|${promptText.value}`,
  () => {
    if (!focused.value) draft.value = promptText.value;
  },
);

function sendPrompt() {
  if (promptEditable.value) {
    const text = draft.value.trim();
    if (!text) return;
    emit("save-prompt", { node: node.value, text });
    return;
  }
  if (GEN_TYPES.has(node.value.type)) emit("generate", node.value);
  else emit("upload", node.value);
}

function onPromptKeydown(event) {
  if (!promptEditable.value) return;
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    sendPrompt();
  }
}

function runAttempt(attempt) {
  emit("attempt", { node: node.value, action: attempt.key });
}
</script>

<template>
  <div class="tnode" :class="[`type-${node.type}`, `status-${node.status}`, { 'agent-changed': data.changed, selected }]">
    <!-- 卡上方：标题行（LibTV 式） -->
    <div class="tnode-caption" :title="node.label">
      <span class="tnode-caption-icon" aria-hidden="true">{{ typeGlyph }}</span>
      <span class="tnode-caption-label">{{ node.label }}</span>
      <span class="tnode-caption-meta">
        <span class="tnode-caption-dims">{{ dims }}</span>
        <span
          class="tnode-caption-dot"
          :class="`lifecycle-${lifecycleClass}`"
          :title="lifecycle.hint"
          role="img"
          :aria-label="lifecycle.label"
        />
      </span>
    </div>

    <!-- 选中时卡上方浮出工具条 -->
    <div v-if="selected" class="tnode-toolbar nodrag" @click.stop>
      <button v-if="canGenerate" type="button" title="只跑这一张" @click.stop="emit('generate', node)">生成</button>
      <button v-if="canUpload" type="button" title="上传/替换素材" @click.stop="emit('upload', node)">上传</button>
      <button type="button" title="历史版本" @click.stop="emit('history', node)">
        历史<template v-if="historyCount"> {{ historyCount }}</template>
      </button>
      <button type="button" title="打开详情面板" @click.stop="emit('open-studio', node)">详情</button>
    </div>

    <div class="tnode-body" :class="bodyClass">
      <!-- 有产物：画面就是卡片 -->
      <template v-if="media">
        <video
          v-if="media.port === 'video'"
          class="tnode-media-video nowheel nodrag"
          :src="mediaSrc"
          controls
          preload="metadata"
          @click.stop
        />
        <audio
          v-else-if="media.port === 'audio'"
          class="tnode-media-audio nowheel nodrag"
          :src="mediaSrc"
          controls
          preload="metadata"
          @click.stop
        />
        <img v-else class="tnode-media-img" :src="mediaSrc" :alt="node.label" loading="lazy" draggable="false" />
        <span v-if="badge" class="tnode-badge">{{ badge }}</span>
        <button type="button" class="tnode-more nodrag" title="打开详情面板" @click.stop="emit('open-studio', node)">⋯</button>
      </template>

      <!-- 没有产物：占位框 + 尝试动作 -->
      <div v-else class="tnode-placeholder">
        <span class="tnode-placeholder-icon" aria-hidden="true">{{ typeGlyph }}</span>
        <div v-if="showAttempts" class="tnode-attempts">
          <span class="tnode-attempts-title">尝试：</span>
          <ul>
            <li v-for="attempt in attempts" :key="attempt.key">
              <button type="button" class="tnode-attempt nodrag" @click.stop="runAttempt(attempt)">
                <span class="tnode-attempt-icon" aria-hidden="true">↳</span>{{ attempt.label }}
              </button>
            </li>
          </ul>
        </div>
        <p v-else class="tnode-placeholder-hint">{{ emptyHint }}</p>
      </div>

      <div v-if="lifecycleMessage" class="tnode-lifecycle-message" :class="`lifecycle-${lifecycleClass}`">
        <span v-if="lifecycleClass === 'generating'" class="tnode-progress-dot" aria-hidden="true" />
        {{ lifecycleMessage }}
      </div>

      <div
        class="tnode-lifecycle"
        :class="`lifecycle-${lifecycleClass}`"
        role="status"
        aria-live="polite"
        :title="lifecycle.hint"
      >
        <div v-if="lifecycleClass !== 'error'" class="tnode-lifecycle-track">
          <span
            v-for="(step, index) in lifecycleSteps"
            :key="step.key"
            class="tnode-lifecycle-segment"
            :class="{ active: index <= lifecycle.index, current: index === lifecycle.index }"
            :title="step.label"
          />
        </div>
        <span v-else class="tnode-lifecycle-error-mark" aria-hidden="true">!</span>
        <span class="tnode-lifecycle-label">{{ lifecycle.label }}</span>
      </div>

      <Handle
        v-for="([name, type], index) in inPorts"
        :key="`in-${name}`"
        :id="name"
        type="target"
        :position="Position.Left"
        :class="`port-${type}`"
        :style="{ top: portTop(index) }"
        :title="`输入：${name}（${type}）`"
        :aria-label="`输入 ${name} ${type}`"
      />
      <span
        v-for="([name], index) in inPorts"
        :key="`in-label-${name}`"
        class="tnode-portlabel in"
        :style="{ top: labelTop(index) }"
        >{{ name }}</span
      >

      <Handle
        v-for="([name, type], index) in outPorts"
        :key="`out-${name}`"
        :id="name"
        type="source"
        :position="Position.Right"
        :class="`port-${type}`"
        :style="{ top: portTop(index) }"
        :title="`输出：${name}（${type}）`"
        :aria-label="`输出 ${name} ${type}`"
      />
      <span
        v-for="([name], index) in outPorts"
        :key="`out-label-${name}`"
        class="tnode-portlabel out"
        :style="{ top: labelTop(index) }"
        >{{ name }}</span
      >
    </div>

    <!-- 卡下方提示词框（LibTV 式），选中才出现 -->
    <div v-if="showPromptBox" class="tnode-prompt nodrag">
      <div class="tnode-prompt-chips">
        <span class="tnode-prompt-chip">＋参考</span>
        <span class="tnode-prompt-chip">◷ 标记</span>
        <span class="tnode-prompt-chip">❖ 风格</span>
      </div>
      <textarea
        v-model="draft"
        class="tnode-prompt-input"
        rows="3"
        :readonly="!promptEditable"
        :placeholder="promptPlaceholder"
        @focus="focused = true"
        @blur="focused = false"
        @keydown="onPromptKeydown"
        @click.stop
      />
      <div class="tnode-prompt-foot">
        <span class="tnode-prompt-meta">
          <span class="tnode-prompt-model">❖ {{ modelLabel }}</span>
          <span class="tnode-prompt-sep">｜</span>
          <span class="tnode-prompt-size">{{ sizeLabel }}</span>
        </span>
        <button
          type="button"
          class="tnode-prompt-send"
          :title="promptAction.hint"
          :aria-label="promptAction.hint"
          @click.stop="sendPrompt"
        >
          ↑
        </button>
      </div>
    </div>
  </div>
</template>
