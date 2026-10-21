<script setup>
/* AI 助手面板 —— 全局共享对话组件（剧本/画布页停靠 dock，其余页面抽屉 drawer）。
 *
 * 会话协议与画布 FloatingAgent 同源（见 store.js askNode）：
 *   · 首次发送 POST /api/sessions 建会话（绑定 canvas）；
 *   · streamNDJSON("/api/agent") 收 NDJSON 流：message_update 增量、
 *     tool_execution_end 表示 AI 动过画布（向上冒泡让宿主刷新数据）；
 *   · 运行中可取消（/api/agent/cancel + AbortController）。
 *
 * 修改建议以 diff 卡片呈现（ai/diff.js 约定）：应用走 applyHandler prop，
 * 由宿主落到 POST /segments/{id}/prompt（人改优先）；没有 handler 时只展示建议。
 */
import { reactive, ref } from "vue";
import { postJSON, streamNDJSON } from "../api.js";
import { parseDiffs } from "./diff.js";

const props = defineProps({
  mode: { type: String, default: "dock" }, // dock | drawer
  canvas: { type: String, required: true },
  title: { type: String, default: "AI 助手" },
  chips: { type: Array, default: () => [] },
  /* async (diff) => boolean：应用字段修改；返回 true 则卡片置「已应用」 */
  applyHandler: { type: Function, default: null },
});
const emit = defineEmits(["canvas-changed"]);

const messages = reactive([]); // {role:'user'|'bot', text, streaming, error, diffs, applied:Set, dismissed:Set}
const input = ref("");
const running = ref(false);
const sessionId = ref("");
const bodyEl = ref(null);
let controller = null;

function scrollEnd() {
  requestAnimationFrame(() => bodyEl.value?.scrollTo({ top: bodyEl.value.scrollHeight }));
}

function send(text) {
  const prompt = String(text ?? input.value ?? "").trim();
  if (!prompt || running.value) return;
  input.value = "";
  messages.push({ role: "user", text: prompt });
  reply(prompt);
}

async function reply(prompt) {
  const bot = reactive({ role: "bot", text: "", streaming: true, error: "", diffs: [], applied: new Set(), dismissed: new Set() });
  messages.push(bot);
  running.value = true;
  controller = new AbortController();
  try {
    if (!sessionId.value) {
      const detail = await postJSON("/api/sessions", { canvas: props.canvas, title: "制片台助手" });
      sessionId.value = detail.id;
    }
    await streamNDJSON(
      "/api/agent",
      { prompt, session_id: sessionId.value || null, canvas: props.canvas, media_policy: "plan_only" },
      (ev) => {
        if (ev.type === "message_update") {
          bot.text += ev.delta || "";
          scrollEnd();
        } else if (ev.type === "tool_execution_end") {
          emit("canvas-changed");
        }
      },
      (err) => {
        bot.error = err?.message || String(err);
      },
      controller.signal,
    );
  } catch (err) {
    bot.error = err?.message || String(err);
  } finally {
    const { plain, diffs } = parseDiffs(bot.text);
    bot.text = plain;
    bot.diffs = diffs;
    bot.streaming = false;
    running.value = false;
    controller = null;
    scrollEnd();
  }
}

async function cancel() {
  controller?.abort();
  if (sessionId.value) {
    postJSON("/api/agent/cancel", { session_id: sessionId.value }).catch(() => {});
  }
}

async function apply(message, diff, idx) {
  if (!props.applyHandler) return;
  try {
    if (await props.applyHandler(diff)) message.applied.add(idx);
  } catch (err) {
    /* 失败保持未应用状态；宿主负责提示 */
    console.warn("[AiPanel] apply failed:", err);
  }
}

defineExpose({ send });
</script>

<template>
  <aside class="ai-panel" :class="`is-${mode}`">
    <div class="ai-head">
      <span class="nm">{{ title }}</span>
      <span class="st ok"><i></i>在线</span>
      <slot name="head-extra" />
    </div>

    <div ref="bodyEl" class="ai-body">
      <div v-if="!messages.length" class="msg bot">
        我在。可以让我改提示词、查过期原因、重拆分镜、调音色——<b>涉及修改的操作都会先给你看改动内容，确认后才应用</b>。
      </div>
      <template v-for="(m, mi) in messages" :key="mi">
        <div class="msg" :class="m.role">{{ m.text }}</div>
        <div v-if="m.error" class="msg err">⚠ {{ m.error }}</div>
        <div v-for="(d, di) in m.diffs || []" :key="`${mi}-${di}`" class="diff" :class="{ applied: m.applied.has(di), dismissed: m.dismissed.has(di) }">
          <div class="cap-row">建议修改 · 片段 {{ d.segmentId || "?" }} ·【{{ d.field }}】</div>
          <div class="d-old">{{ d.old }}</div>
          <div class="d-new">{{ d.new }}</div>
          <div class="d-acts">
            <button class="btn sm primary" type="button" @click="apply(m, d, di)">应用修改</button>
            <button class="btn sm" type="button" @click="m.dismissed.add(di)">放弃</button>
          </div>
        </div>
      </template>
    </div>

    <div class="ai-quick">
      <button v-for="c in chips" :key="c" class="q-chip" type="button" :disabled="running" @click="send(c)">{{ c }}</button>
    </div>

    <div class="ai-input">
      <input
        v-model="input"
        :disabled="running"
        placeholder="告诉 AI 你想改什么…"
        @keydown.enter="send()"
      />
      <button v-if="running" class="send cancel" type="button" title="取消" @click="cancel">✕</button>
      <button v-else class="send" type="button" @click="send()">↑</button>
    </div>
  </aside>
</template>

<style scoped>
.ai-panel { background: var(--bg-2); display: flex; flex-direction: column; min-height: 0; }
.ai-head { display: flex; align-items: baseline; gap: 10px; padding: var(--sp-4); border-bottom: 1px solid var(--line); }
.ai-head .nm { font-family: var(--serif); font-weight: 600; font-size: var(--fs-14); flex: 1; }
.st { display: inline-flex; align-items: center; gap: 6px; font-size: var(--fs-12); color: var(--ok); }
.st i { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); }

.ai-body { flex: 1; overflow-y: auto; padding: var(--sp-4); display: flex; flex-direction: column; gap: var(--sp-4); }
.msg { font-size: var(--fs-12); line-height: 1.8; color: var(--text-2); white-space: pre-wrap; }
.msg.user { padding-left: 10px; border-left: 2px solid var(--line-3); }
.msg.bot b { color: var(--text); font-weight: 500; }
.msg.err { color: var(--bad); }

.diff { border: 1px solid var(--line-2); border-radius: var(--r-2); overflow: hidden; }
.diff .cap-row { padding: 7px 12px; font-size: var(--fs-11); letter-spacing: .1em; color: var(--gold); background: var(--gold-soft); display: flex; align-items: center; }
.diff .d-old { padding: 8px 12px; color: var(--text-3); font-size: var(--fs-12); text-decoration: line-through; text-decoration-color: var(--line-3); border-bottom: 1px dashed var(--line-2); }
.diff .d-new { padding: 8px 12px; color: var(--text); font-size: var(--fs-12); background: rgba(194, 163, 107, .05); }
.diff .d-acts { display: flex; gap: 8px; padding: 9px 12px; border-top: 1px solid var(--line); }
.diff.applied .d-acts { display: none; }
.diff.applied .cap-row::after { content: "已应用 ✓"; margin-left: auto; color: var(--ok); }
.diff.dismissed { display: none; }

.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; padding: 9px 20px; border-radius: var(--r-1); font-size: var(--fs-14); color: var(--text-2); transition: all var(--dur) var(--ease); }
.btn:hover { color: var(--text); background: var(--card-2); }
.btn.primary { background: var(--accent); color: var(--text-on-accent, #FFF7F0); }
.btn.primary:hover { background: var(--accent-hi); }
.btn.sm { padding: 6px 14px; font-size: var(--fs-13); }

.ai-quick { padding: 0 var(--sp-4) var(--sp-2); display: flex; flex-wrap: wrap; gap: 6px; }
.q-chip { font-size: var(--fs-12); color: var(--text-3); border: 1px solid var(--line-2); border-radius: 999px; padding: 5px 14px; }
.q-chip:hover { color: var(--gold); border-color: rgba(194, 163, 107, .4); }
.q-chip:disabled { opacity: .5; }

.ai-input { padding: var(--sp-3) var(--sp-4); border-top: 1px solid var(--line); display: flex; gap: 8px; }
.ai-input input { flex: 1; background: var(--card); border: 1px solid var(--line); border-radius: var(--r-1); padding: 10px 14px; color: var(--text); font: inherit; font-size: var(--fs-13); }
.ai-input input:focus { outline: none; border-color: rgba(201, 85, 61, .5); }
.send { width: 40px; border-radius: var(--r-1); background: var(--accent); color: var(--text-on-accent, #FFF7F0); display: grid; place-items: center; font-size: var(--fs-14); }
.send:hover { background: var(--accent-hi); }
.send.cancel { background: var(--card-3); color: var(--text-2); }
</style>
