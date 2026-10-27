<script setup>
/* 阶段消息（对话流左侧 Agent 气泡）：一个 stage 事件开一条，聚合其后的工具卡与门行。
 * 头部 = ChatAvatar(bot) + 阶段名 + 状态徽：
 *   running → 脉冲点「运行中 {计时}s」（since=0 或非 running 不显示计时）；
 *   done → 绿 ✓；failed → 红 ✗；degraded → 黄 ⛔「产物已落库 · N 项阻断标记」。
 * 主体 = parts 逐个 ToolCard / GateRow。气泡规格见 spec §3：
 * var(--card) + 1px var(--line)、圆角 10px（贴头像的左上侧 4px）、max-width 68%。 */
import { computed } from "vue";
import ChatAvatar from "./ChatAvatar.vue";
import ToolCard from "./ToolCard.vue";
import GateRow from "./GateRow.vue";

const props = defineProps({
  item: { type: Object, required: true },
  nowTick: { type: Number, default: 0 },
  since: { type: Number, default: 0 }, /* 本会话观察到该阶段首帧的时刻 ms，运行中计时用 */
});

/* 计时口径（简报逐字）：(nowTick - since) ms → 秒，toFixed(1)；since=0 或非 running 不显示 */
const timerText = computed(() => {
  if (props.item.status !== "running" || !props.since) return "";
  return `${((props.nowTick - props.since) / 1000).toFixed(1)}s`;
});
</script>

<template>
  <div class="stmsg">
    <ChatAvatar who="bot" />
    <div class="st-bubble">
      <div class="st-head">
        <span class="st-label">{{ item.label }}</span>
        <span v-if="item.status === 'running'" class="badge running">
          <i class="pulse" />运行中<template v-if="timerText">&nbsp;{{ timerText }}</template>
        </span>
        <span v-else-if="item.status === 'done' && !item.degraded" class="badge done">✓</span>
        <span v-else-if="item.status === 'failed'" class="badge failed">✗</span>
        <span v-if="item.degraded" class="badge degraded">⛔ 产物已落库 · {{ item.blocking }} 项阻断标记</span>
      </div>
      <div v-if="item.parts && item.parts.length" class="st-parts">
        <template v-for="(p, i) in item.parts" :key="i">
          <ToolCard v-if="p.kind === 'tool'" :part="p" />
          <GateRow v-else-if="p.kind === 'gate'" :row="p" />
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.stmsg {
  display: flex;
  align-items: flex-start;
  gap: 10px; /* 气泡贴头像 10px（spec §3） */
}
.st-bubble {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  max-width: 68%;
  padding: 9px 13px;
  border: 1px solid var(--line);
  border-radius: 10px 10px 10px 4px; /* 左上小角贴「管线」头像 */
  background: var(--card);
}
.st-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sp-2);
}
.st-label { color: var(--text); font-size: var(--fs-13); font-weight: 600; line-height: 1.65; }
.badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--fs-11);
  line-height: 1.6;
  font-variant-numeric: tabular-nums;
}
.badge.running { color: var(--warn); }
.pulse {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--warn);
  display: inline-block;
  animation: st-pulse 1.2s ease-in-out infinite;
}
@keyframes st-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.25; } }
.badge.done { color: var(--ok); font-size: var(--fs-13); font-weight: 600; }
.badge.failed { color: var(--danger); font-size: var(--fs-13); font-weight: 600; }
.badge.degraded { color: var(--warn); }

.st-parts {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.st-parts > * { max-width: 100%; }
</style>
