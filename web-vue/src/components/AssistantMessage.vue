<script setup>
/* 一条 assistant 消息 = 一个 parts 容器（照 Toonflow 的 conversation.vue）。
 *
 * 三处"活着/结束"的信号，缺一个就会让人觉得"不直观"：
 *   1. 头部：运行中显示**实时计时 + 当前在做什么**
 *   2. 正文末尾：运行中常驻一行"执行中"提示（不只是没有内容时才显示）
 *   3. 结尾章：结束时明确标出 完成 / 已中断 / 未正常结束 + 用时 + 用量
 */
import { computed } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import MarkdownText from "./MarkdownText.vue";
import ThinkingPart from "./ThinkingPart.vue";
import ToolPart from "./ToolPart.vue";
import SubAgentPart from "./SubAgentPart.vue";
import { actions, state } from "../store.js";

const props = defineProps({ item: { type: Object, required: true } });

const parts = computed(() => props.item.parts || []);
const tools = computed(() => parts.value.filter((p) => p.kind === "tool"));
const failed = computed(() => tools.value.filter((p) => p.status === "error").length);
const subs = computed(() => parts.value.filter((p) => p.kind === "sub").length);
const thinkingSeconds = computed(() =>
  parts.value.filter((p) => p.kind === "thinking").reduce((sum, p) => sum + (p.duration || 0), 0)
);
const canBind = computed(() =>
  new Map([
    ["thinking", ThinkingPart],
    ["tool", ToolPart],
    ["sub", SubAgentPart],
  ])
);

const hasContent = computed(() => parts.value.some((p) => p.kind !== "text" || p.text.trim().length));
const waiting = computed(() => props.item.streaming && !hasContent.value);
/** 跑完了却什么都没产出：明确说一句，别留一个空壳气泡让人猜。 */
const empty = computed(() => !props.item.streaming && !parts.value.length);
const canvasCount = computed(() => tools.value.filter((p) => p.canvas).length);

/** 这一条是不是"当前正在跑的那条"（决定要不要显示活动提示）。 */
const isLive = computed(() => props.item.streaming && state.running);

const elapsed = computed(() => {
  if (!isLive.value || !state.runStartedAt) return 0;
  return (state.nowTick - state.runStartedAt) / 1000;
});

/** 当前那一步跑了多久（活动提示里的秒数）。 */
const stepElapsed = computed(() => {
  if (!isLive.value) return 0;
  const running = [...tools.value].reverse().find((p) => p.status === "running");
  const since = running?.startedAt || state.lastEventAt || state.runStartedAt;
  return since ? (state.nowTick - since) / 1000 : 0;
});

/** 服务端静默太久：心跳 5s 一次，超过 ~12s 就该提醒（可能卡住/断线）。 */
const silent = computed(() => {
  if (!isLive.value || !state.lastEventAt) return 0;
  const gap = (state.nowTick - state.lastEventAt) / 1000;
  return gap >= 12 ? Math.round(gap) : 0;
});

const STAMP = {
  done: { cls: "ok", icon: "CircleCheck", text: "完成" },
  waiting_approval: { cls: "warn", icon: "WarningFilled", text: "创作包已验收，等待审批" },
  aborted: { cls: "warn", icon: "WarningFilled", text: "已中断" },
  error: { cls: "danger", icon: "WarningFilled", text: "出错了" },
  unknown: { cls: "danger", icon: "WarningFilled", text: "未正常结束" },
};
const stamp = computed(() => (props.item.ended ? STAMP[props.item.ended.reason] || STAMP.unknown : null));

function num(value) {
  return value == null ? "-" : Number(value).toLocaleString("en-US");
}

const fullText = computed(() =>
  parts.value
    .filter((p) => p.kind === "text")
    .map((p) => p.text)
    .join("\n\n")
);

async function copyAll() {
  try {
    await navigator.clipboard.writeText(fullText.value);
    ElMessage.success("已复制整条回答");
  } catch {
    ElMessage.warning("复制失败，请手动选中");
  }
}

async function retryAnswer() {
  if (!props.item.parentId) return;
  try {
    await actions.mutateMessage(props.item.parentId, "resend");
  } catch (err) {
    ElMessage.error(`重新生成失败：${err.message || err}`);
  }
}

async function deleteAnswer() {
  try {
    await ElMessageBox.confirm("将隐藏这条回答以及它后面的内容。", "删除回答", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
    await actions.mutateMessage(props.item.entryId, "delete");
  } catch (err) {
    if (err !== "cancel" && err !== "close") ElMessage.error(`删除失败：${err.message || err}`);
  }
}
</script>

<template>
  <div class="msg assistant">
    <div class="msg-head">
      <span class="who">Agent</span>
      <span v-if="isLive" class="live-timer">
        <i class="pulse" />
        {{ state.activity || "运行中" }} · {{ elapsed.toFixed(1) }}s
      </span>
      <template v-else>
        <span v-if="thinkingSeconds > 0" class="dim">{{ thinkingSeconds.toFixed(1) }}s 思考</span>
        <span v-if="tools.length" class="dim">{{ tools.length }} 次工具</span>
        <span v-if="subs" class="dim">{{ subs }} 个子 Agent</span>
        <span v-if="failed" class="dim danger">{{ failed }} 个失败</span>
        <span v-if="canvasCount" class="dim ok">画布已更新</span>
      </template>
      <span class="spacer" />
      <el-button v-if="fullText" link size="small" @click="copyAll">复制</el-button>
      <el-button v-if="item.parentId && !state.running" link size="small" @click="retryAnswer">重新生成</el-button>
      <el-button v-if="item.entryId && !state.running" link size="small" type="danger" @click="deleteAnswer">删除</el-button>
    </div>

    <div class="msg-body">
      <div v-if="waiting" class="waiting">
        <el-icon class="spin"><Loading /></el-icon>
        <span>{{ state.activity || "正在思考…" }}</span>
        <span class="dim mono">{{ elapsed.toFixed(1) }}s</span>
      </div>

      <div v-if="empty" class="dim">（模型这次没有返回内容，也没有调用工具）</div>

      <template v-for="(part, index) in parts" :key="index">
        <MarkdownText v-if="part.kind === 'text'" :content="part.text" :streaming="part.streaming" />
        <div v-else-if="part.kind === 'team'" class="part part-team">
          <el-icon><UserFilled /></el-icon>
          <b>{{ part.member }}</b>
          <span class="dim">角色 {{ part.role }}</span>
          <span class="dim">工具白名单：{{ part.tools.join("、") || "无" }}</span>
          <el-tag size="small" :type="part.status === 'done' ? 'success' : 'warning'">
            {{ part.status === "done" ? "已完成" : "执行中" }}
          </el-tag>
        </div>
        <component v-else :is="canBind.get(part.kind)" :part="part" />
      </template>

      <!-- 运行中常驻一行：哪怕已经有工具卡，也要能看出"它还在动" -->
      <div v-if="isLive && !waiting" class="working">
        <i class="pulse" />
        <span>{{ state.activity || "处理中…" }}</span>
        <span class="dim mono">{{ stepElapsed.toFixed(1) }}s</span>
        <span v-if="silent" class="silent">已 {{ silent }}s 无新事件，可能卡住（可点停止）</span>
      </div>

      <!-- 结束章：明确"结束了、怎么结束的、花了多少" -->
      <div v-if="stamp && item.ended" class="run-stamp" :class="stamp.cls">
        <el-icon><component :is="stamp.icon" /></el-icon>
        <span>{{ stamp.text }}</span>
        <span v-if="item.ended.elapsedMs" class="dim mono">{{ (item.ended.elapsedMs / 1000).toFixed(1) }}s</span>
        <span v-if="item.ended.usage" class="dim mono">
          输入 {{ num(item.ended.usage.input) }} / 输出 {{ num(item.ended.usage.output) }}
        </span>
        <span v-if="item.ended.pings" class="dim mono">心跳 {{ item.ended.pings }} 次</span>
      </div>
    </div>
  </div>
</template>
