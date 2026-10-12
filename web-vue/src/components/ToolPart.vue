<script setup>
/* 工具调用卡（对应 Toonflow 的 toolMessage.vue）。
 *
 * 关键：**默认折叠，但头部要能一眼看出"做了什么、成不成"** ——
 * 工具名 + 关键参数 + 状态 + 耗时 + 画布变更提示。
 * 展开才看完整 参数 / 结果（结果超长自动降级为纯文本）。
 */
import { computed } from "vue";
import { ElMessage } from "element-plus";
import { actions, state } from "../store.js";

const props = defineProps({ part: { type: Object, required: true }, compact: { type: Boolean, default: false } });

const STATUS = {
  running: { text: "调用中…", tag: "warning", cls: "running" },
  success: { text: "已完成", tag: "success", cls: "success" },
  error: { text: "调用失败", tag: "danger", cls: "error" },
  interrupted: { text: "已中断", tag: "info", cls: "interrupted" },
};

const statusMeta = computed(() => STATUS[props.part.status] || STATUS.success);

/** 头部摘要：把参数里最能说明问题的一两项挑出来（Toonflow 头部的做法）。 */
const argsSummary = computed(() => {
  const raw = props.part.args;
  if (!raw) return "";
  let obj;
  try {
    obj = JSON.parse(raw);
  } catch {
    return raw.slice(0, 48);
  }
  const entries = Object.entries(obj || {}).filter(([, value]) => value !== "" && value != null);
  if (!entries.length) return "";
  return entries
    .slice(0, 2)
    .map(([key, value]) => {
      const text = typeof value === "string" ? value : JSON.stringify(value);
      return `${key}=${text.length > 22 ? `${text.slice(0, 22)}…` : text}`;
    })
    .join(" · ");
});

const prettyArgs = computed(() => {
  try {
    return JSON.stringify(JSON.parse(props.part.args), null, 2);
  } catch {
    return props.part.args || "（无参数）";
  }
});

const resultText = computed(() => {
  const text = props.part.result;
  if (!text) return "（无返回内容）";
  return props.part.resultChars > text.length ? `${text}\n…（已截断，共 ${props.part.resultChars} 字）` : text;
});

/** 运行中显示**实时**已耗时（不然长工具看起来像卡死），结束后显示最终耗时。 */
const durationText = computed(() => {
  const part = props.part;
  if (part.status === "running" && part.startedAt) {
    return `${((state.nowTick - part.startedAt) / 1000).toFixed(1)}s`;
  }
  if (part.durationMs == null) return "";
  return part.durationMs >= 1000 ? `${(part.durationMs / 1000).toFixed(1)}s` : `${part.durationMs}ms`;
});

async function copy(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    ElMessage.success(`已复制${label}`);
  } catch {
    ElMessage.warning("复制失败，请手动选中");
  }
}

function gotoCanvas() {
  actions.switchView("canvas");
}
</script>

<template>
  <div class="part part-tool" :class="[statusMeta.cls, { compact }]">
    <button class="part-head" @click="part.expanded = !part.expanded">
      <el-icon class="chev" :class="{ open: part.expanded }"><ArrowRight /></el-icon>
      <el-icon class="icon" :class="{ spin: part.status === 'running' }">
        <Loading v-if="part.status === 'running'" />
        <Tools v-else />
      </el-icon>
      <b class="title">{{ part.name }}</b>
      <span class="args">{{ argsSummary }}</span>
      <span v-if="durationText" class="dim mono">{{ durationText }}</span>
      <span class="status" :class="statusMeta.cls">{{ statusMeta.text }}</span>
    </button>

    <!-- 画布变更：一眼看出"AI 到底改成功没有"，并可直接跳过去看 -->
    <div v-if="part.canvas" class="canvas-chip">
      <el-icon><Grid /></el-icon>
      <span>画布「{{ part.canvas.name }}」已更新</span>
      <span class="dim">{{ part.canvas.nodes }} 节点 / {{ part.canvas.edges }} 连线</span>
      <el-button link type="primary" size="small" @click="gotoCanvas">去画布看看</el-button>
    </div>

    <div v-if="part.expanded" class="part-body">
      <div class="io-head">
        <span>参数</span>
        <el-button link size="small" @click="copy(prettyArgs, '参数')">复制</el-button>
      </div>
      <pre class="io-block">{{ prettyArgs }}</pre>

      <div class="io-head">
        <span>结果</span>
        <span class="dim">{{ part.resultChars }} 字</span>
        <el-button link size="small" @click="copy(part.result || '', '结果')">复制</el-button>
      </div>
      <pre class="io-block" :class="{ error: part.status === 'error' }">{{ resultText }}</pre>
    </div>
  </div>
</template>
