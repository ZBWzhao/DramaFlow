<script setup>
/* 运行对话流容器：feed 正序渲染 + 活的信号三件套 + 断线横幅 + 恢复说明。
 * 计时口径：事件帧无时间戳，只有本会话观察到的阶段才有时刻（stageSince）。 */
import { computed, nextTick, onUnmounted, ref, watch } from "vue";
import { studio } from "../../../studio/studioStore.js";
import { buildFeed } from "./useRunFeed.js";
import ChatAvatar from "./ChatAvatar.vue";
import SystemNote from "./SystemNote.vue";
import StageMessage from "./StageMessage.vue";
import UserNovelBubble from "./UserNovelBubble.vue";
import GateConfirmCard from "./GateConfirmCard.vue";
import FailCard from "./FailCard.vue";
import DoneCard from "./DoneCard.vue";

const run = computed(() => studio.run);
const feed = computed(() => {
  const items = buildFeed(run.value.events, run.value.novel);
  // F5 恢复不回放历史帧（events 只剩 reconnect 的 hello），门卡从 run 态合成；确认门不可跳过
  if (run.value.status === "gated" && !items.some((it) => it.kind === "gate-card")) {
    items.push({ kind: "gate-card", stage: run.value.stage });
  }
  return items;
});

/* 活的信号：200ms tick 只在 running 时走；12s 无新事件提示（照 ChatPanel 口径） */
const nowTick = ref(Date.now());
let ticker = null;
function stopTicker() { if (ticker) { clearInterval(ticker); ticker = null; } }
watch(() => run.value.status === "running", (on) => {
  nowTick.value = Date.now(); stopTicker();
  if (on) ticker = setInterval(() => { nowTick.value = Date.now(); }, 200);
}, { immediate: true });
onUnmounted(stopTicker);
const lastEventAt = ref(0);
watch(() => run.value.events.length, () => { lastEventAt.value = Date.now(); });
const silent = computed(() => (run.value.status === "running" && lastEventAt.value
  && nowTick.value - lastEventAt.value >= 12000)
  ? Math.round((nowTick.value - lastEventAt.value) / 1000) : 0);

/* 运行中阶段的计时起点：本会话首次观察到该 stage 帧的时刻 */
const stageSince = ref({});
watch(() => run.value.events.length, () => {
  const evs = run.value.events;
  const last = evs[evs.length - 1];
  if (last?.type === "stage" && !stageSince.value[last.stage]) {
    stageSince.value = { ...stageSince.value, [last.stage]: Date.now() };
  }
});

/* 恢复说明：reattach 路径 link='restored' 时一次性提示（历史帧不回放） */
const restoredNote = ref(false);
watch(() => [run.value.id, run.value.link], ([, link]) => {
  if (link === "restored") restoredNote.value = true;
  if (link === "") restoredNote.value = false;
}, { immediate: true });

/* stick-bottom 滚动（照 ChatPanel：距底 90px 内才跟随） */
const streamEl = ref(null);
const stickBottom = ref(true);
function onScroll() {
  const el = streamEl.value;
  if (el) stickBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 90;
}
watch(() => [feed.value.length, run.value.events.length], () => {
  nextTick(() => {
    const el = streamEl.value;
    if (el && stickBottom.value) el.scrollTop = el.scrollHeight;
  });
});
</script>

<template>
  <section class="rc">
    <div v-if="run.link === 'reconnecting'" class="rc-banner warn">直播连接断开，自动重连中…</div>
    <div v-else-if="run.link === 'lost'" class="rc-banner err">直播连接已断开（服务可能已重启），请刷新页面查看最新进度</div>
    <div ref="streamEl" class="rc-stream" @scroll="onScroll">
      <SystemNote v-if="restoredNote" text="已接入运行现场，此前过程不回放" />
      <div v-if="!feed.length" class="rc-none">正在连接执行器，等待第一帧…</div>
      <template v-for="(item, i) in feed" :key="i">
        <div v-if="item.kind === 'user'" class="rc-row user">
          <UserNovelBubble :text="item.text" /><ChatAvatar who="user" />
        </div>
        <!-- stage 消息自带头像（StageMessage 内含 ChatAvatar），不再包 .rc-row，防双头像 -->
        <StageMessage v-else-if="item.kind === 'stage'" :item="item" :now-tick="nowTick"
                      :since="stageSince[item.stage] || 0" />
        <GateConfirmCard v-else-if="item.kind === 'gate-card'" :stage="item.stage" />
        <FailCard v-else-if="item.kind === 'fail-card'"
                  :stage="item.stage" :findings="item.findings" />
        <DoneCard v-else-if="item.kind === 'done-card'" />
        <SystemNote v-else-if="item.kind === 'system'" :text="item.text" />
        <div v-else class="rc-row bot">
          <ChatAvatar who="bot" />
          <div class="rc-bubble unknown">{{ item.raw }}</div>
        </div>
      </template>
      <div v-if="silent" class="rc-silent">已 {{ silent }}s 无新事件，可能卡住或断线</div>
    </div>
  </section>
</template>

<style scoped>
.rc { display: flex; flex-direction: column; gap: var(--sp-2); margin-bottom: var(--sp-5); }
.rc, .rc * { box-sizing: border-box; }

/* 断线横幅（spec §4.4）：流顶、克制，站内 warn/danger 变量 */
.rc-banner { padding: var(--sp-2) var(--sp-3); border-radius: var(--r-2); font-size: var(--fs-12); line-height: 1.7; }
.rc-banner.warn { border: 1px solid color-mix(in srgb, var(--warn) 45%, transparent); background: var(--warn-soft); color: var(--warn); }
.rc-banner.err { border: 1px solid color-mix(in srgb, var(--danger) 40%, transparent); background: color-mix(in srgb, var(--danger) 8%, transparent); color: var(--danger); }

/* 流：内滚（页面本身也可滚）；980px 容器由 .pv-in 负责，这里不设 max-width */
.rc-stream {
  display: flex; flex-direction: column; gap: var(--sp-3);
  max-height: 640px; overflow-y: auto;
  padding: var(--sp-3); border: 1px solid var(--line-2); border-radius: var(--r-2);
  background: var(--bg);
}

/* 行：头像 + 气泡，气泡贴头像 10px（spec §3） */
.rc-row { display: flex; align-items: flex-start; gap: 10px; }
/* 头像居最右列（spec §3「靠右」、ChatAvatar 注释）；row-reverse 会把气泡挤到外沿，不用 */
.rc-row.user { justify-content: flex-end; }

/* 未知事件灰气泡：Agent 气泡灰调 + --mono + 防撑爆（spec §2 末行） */
.rc-bubble.unknown {
  max-width: 68%; padding: 9px 13px;
  border: 1px solid var(--line); border-radius: 4px 10px 10px 10px;
  background: var(--card); color: var(--text-3);
  font-family: var(--mono); font-size: var(--fs-12); line-height: 1.7;
  word-break: break-all;
}

/* 等待首帧 / 12s 无新事件：居中灰字，克制（spec §5 / §4.3，SystemNote 同调） */
.rc-none, .rc-silent { text-align: center; color: var(--text-3); font-size: var(--fs-11); line-height: 1.6; padding: var(--sp-2) 0; }
</style>
