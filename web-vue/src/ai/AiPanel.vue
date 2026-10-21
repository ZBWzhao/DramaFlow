<script setup>
/* AI 助手面板 —— 全局共享对话组件（剧本/画布页停靠 dock，其余页面抽屉 drawer）。
 *
 * 会话协议与画布 FloatingAgent 同源（见 store.js askNode）：
 *   · 挂载时恢复历史：GET /api/sessions?canvas= 取最近会话 → /entries 还原消息；
 *   · 首次发送 POST /api/sessions 建会话（绑定 canvas）；
 *   · streamNDJSON("/api/agent") 收 NDJSON 流：message_update 增量、
 *     tool_execution_start/end 渲染成工具调用卡（AI 干了什么一目了然）、
 *     tool_execution_end 表示 AI 动过数据（向上冒泡让宿主刷新）；
 *   · 运行中可取消（/api/agent/cancel + AbortController）。
 *
 * 回复正文走 ai/md.js 渲染（先转义再转换，防注入）；
 * 修改建议以 diff 卡片呈现（ai/diff.js 约定）：应用走 applyHandler prop。
 */
import { onMounted, reactive, ref } from "vue";
import { getJSON, postJSON, streamNDJSON } from "../api.js";
import { parseDiffs } from "./diff.js";
import { renderMd } from "./md.js";

const props = defineProps({
  mode: { type: String, default: "dock" }, // dock | drawer
  canvas: { type: String, required: true },
  title: { type: String, default: "AI 助手" },
  chips: { type: Array, default: () => [] },
  /* async (diff) => boolean：应用字段修改；返回 true 则卡片置「已应用」 */
  applyHandler: { type: Function, default: null },
});
const emit = defineEmits(["canvas-changed"]);

const messages = reactive([]); // {role:'user'|'bot', text, streaming, error, tools:[], diffs, applied:Set, dismissed:Set}
const input = ref("");
const running = ref(false);
const sessionId = ref("");
const bodyEl = ref(null);
let controller = null;

/* 工具名 → 中文短标签：模型可用的工具越来越多，裸函数名对用户是噪音 */
const TOOL_LABELS = {
  get_canvas: "读取画布",
  identify_story: "识别故事",
  add_node: "建节点",
  add_nodes: "批量建节点",
  update_node: "更新节点",
  connect_nodes: "连接节点",
  delete_nodes: "删除节点",
  set_text: "写入正文",
  set_storyboard: "写入分镜",
  export_prompt: "导出提示词",
  read_file: "读文件",
  list_dir: "查目录",
  read_skill: "读取技能",
  update_story_analysis: "修改项目设定",
  update_asset_tables: "修改资产表",
  run_pipeline: "触发解析",
  word_count: "数字数",
  generate_media: "生成媒体",
  subagent: "委托子代理",
  ask_user: "向用户提问",
};
function toolLabel(name) {
  return TOOL_LABELS[name] || name;
}
function formatArgs(args) {
  try {
    return JSON.stringify(args, null, 2);
  } catch {
    return String(args ?? "");
  }
}

function scrollEnd() {
  requestAnimationFrame(() => bodyEl.value?.scrollTo({ top: bodyEl.value.scrollHeight }));
}

function blankBot() {
  return reactive({ role: "bot", text: "", streaming: true, error: "", tools: [], diffs: [], applied: new Set(), dismissed: new Set() });
}

/* 刷新后恢复历史：最近一条会话的 user/assistant 正文（工具卡不落盘，只还原文字） */
async function restoreHistory() {
  try {
    const data = await getJSON(`/api/sessions?canvas=${encodeURIComponent(props.canvas)}`);
    const latest = (data.sessions || [])[0];
    if (!latest) return;
    const detail = await getJSON(`/api/sessions/${latest.id}/entries?limit=60`);
    const restored = [];
    for (const entry of detail.entries || []) {
      const content = entry?.content;
      if (typeof content !== "string" || !content.trim() || content === "[之前的对话摘要]") continue;
      if (entry.role === "user") restored.push({ role: "user", text: content, tools: [] });
      else if (entry.role === "assistant" && !(entry.tool_calls || []).length) restored.push({ role: "bot", text: content, tools: [], streaming: false });
    }
    sessionId.value = latest.id;
    messages.push(...restored.slice(-30));
    scrollEnd();
  } catch {
    /* 没有历史 / 接口不可用都是常态，静默开局 */
  }
}
onMounted(restoreHistory);

function send(text) {
  const prompt = String(text ?? input.value ?? "").trim();
  if (!prompt || running.value) return;
  input.value = "";
  messages.push({ role: "user", text: prompt, tools: [] });
  reply(prompt);
}

async function reply(prompt) {
  const bot = blankBot();
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
        } else if (ev.type === "tool_execution_start") {
          bot.tools.push({ id: ev.tool_call_id, name: ev.tool, args: ev.args, status: "run", ms: 0, result: "" });
          scrollEnd();
        } else if (ev.type === "tool_execution_end") {
          const card = bot.tools.find((t) => t.id === ev.tool_call_id) || bot.tools[bot.tools.length - 1];
          if (card) {
            card.status = ev.isError ? "err" : "done";
            card.ms = ev.duration_ms || 0;
            card.result = String(ev.result || "").slice(0, 300);
          }
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
      <div v-if="!messages.length" class="greet">
        <p>我在。这个项目里我可以：</p>
        <ul>
          <li>「解析出资产表」「重新分析剧本」——触发后台解析，进度在左侧面板看</li>
          <li>「把林小满的人物小传细化一下」——直接改资产表</li>
          <li>「把世界观改成民国背景」——直接改项目设定</li>
          <li>改提示词、查过期原因、重拆分镜（涉及修改的操作先给 diff，确认才应用）</li>
        </ul>
      </div>
      <template v-for="(m, mi) in messages" :key="mi">
        <div v-for="tl in m.tools || []" :key="`${mi}-${tl.id}`" class="tool" :class="tl.status">
          <details>
            <summary>
              <span class="tico">{{ tl.status === "run" ? "◐" : tl.status === "err" ? "✕" : "✓" }}</span>
              <span class="tname">{{ toolLabel(tl.name) }}</span>
              <span class="tst num">{{ tl.status === "run" ? "执行中…" : tl.status === "err" ? "失败" : `${tl.ms}ms` }}</span>
            </summary>
            <pre class="targs">{{ formatArgs(tl.args) }}</pre>
            <div v-if="tl.result" class="tres">{{ tl.result }}</div>
          </details>
        </div>
        <div v-if="m.role === 'user'" class="msg user">{{ m.text }}</div>
        <div v-else-if="m.text || m.streaming" class="msg bot" :class="{ streaming: m.streaming }" v-html="renderMd(m.text)"></div>
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

.ai-body { flex: 1; overflow-y: auto; padding: var(--sp-4); display: flex; flex-direction: column; gap: 10px; }

/* 开场引导 */
.greet { border: 1px dashed var(--line-2); border-radius: var(--r-2); padding: var(--sp-3) var(--sp-4); color: var(--text-2); font-size: var(--fs-12); line-height: 1.9; }
.greet p { margin-bottom: 4px; }
.greet ul { margin: 0; padding-left: 16px; }
.greet li { margin: 2px 0; }
.greet b { color: var(--gold); font-weight: 500; }

/* 消息气泡：用户右侧描边块，AI 左对齐卡片 */
.msg { font-size: var(--fs-12); line-height: 1.85; color: var(--text-2); }
.msg.user { align-self: flex-end; max-width: 88%; background: var(--card); border: 1px solid var(--line-2); border-radius: var(--r-2) var(--r-2) 2px var(--r-2); padding: 8px 12px; color: var(--text); white-space: pre-wrap; }
.msg.bot { align-self: stretch; background: none; border: none; padding: 2px 2px; }
.msg.bot :deep(.md-p) { margin: 0 0 6px; }
.msg.bot :deep(.md-h) { font-family: var(--serif); font-size: var(--fs-13); color: var(--text); margin: 10px 0 4px; letter-spacing: .04em; }
.msg.bot :deep(.md-h):first-child { margin-top: 0; }
.msg.bot :deep(.md-ul), .msg.bot :deep(.md-ol) { margin: 2px 0 8px; padding-left: 18px; }
.msg.bot :deep(li) { margin: 3px 0; }
.msg.bot :deep(b) { color: var(--text); font-weight: 600; }
.msg.bot :deep(.md-code) { font-family: var(--mono); font-size: var(--fs-11); background: var(--card); border: 1px solid var(--line); border-radius: 4px; padding: 1px 5px; }
.msg.bot :deep(.md-pre) { background: var(--card); border: 1px solid var(--line); border-radius: var(--r-1); padding: 10px 12px; overflow-x: auto; margin: 4px 0 8px; }
.msg.bot :deep(.md-pre code) { font-family: var(--mono); font-size: var(--fs-11); color: var(--text-2); white-space: pre; }
.msg.bot :deep(.md-gap) { height: 4px; }
.msg.bot.streaming::after { content: "▍"; color: var(--gold); animation: blink 1s steps(2) infinite; }
@keyframes blink { 50% { opacity: 0; } }
.msg.err { color: var(--bad); }

/* 工具调用卡 */
.tool { align-self: stretch; border: 1px solid var(--line); border-radius: var(--r-1); background: var(--card); overflow: hidden; }
.tool.run { border-color: rgba(194, 163, 107, .45); }
.tool.err { border-color: rgba(196, 74, 58, .5); }
.tool summary { display: flex; align-items: center; gap: 8px; padding: 6px 10px; cursor: pointer; list-style: none; font-size: var(--fs-11); }
.tool summary::-webkit-details-marker { display: none; }
.tool .tico { width: 14px; text-align: center; color: var(--ok); }
.tool.run .tico { color: var(--gold); }
.tool.err .tico { color: var(--bad); }
.tool .tname { color: var(--text-2); }
.tool .tst { margin-left: auto; color: var(--text-3); }
.tool .targs { margin: 0; padding: 8px 12px; border-top: 1px dashed var(--line); font-family: var(--mono); font-size: var(--fs-11); color: var(--text-3); white-space: pre-wrap; word-break: break-all; max-height: 160px; overflow-y: auto; }
.tool .tres { padding: 6px 12px 8px; border-top: 1px dashed var(--line); font-size: var(--fs-11); color: var(--text-3); white-space: pre-wrap; word-break: break-all; max-height: 120px; overflow-y: auto; }

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
