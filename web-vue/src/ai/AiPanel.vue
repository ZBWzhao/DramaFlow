<script setup>
/* AI 助手面板 —— DeepSeek 式对话（剧本/画布页停靠 dock，其余页面抽屉 drawer）。
 *
 * 结构对齐 deepseek.com 的对话形态：
 *   · 思考块可折叠：流式时自动展开（thinking_update），出正文后自动收起；
 *     工具 / SKILL 调用作为步骤行显示在思考块内部；
 *   · 对话用气泡：用户右侧强调底色，AI 左侧卡片；正文走 MarkdownText（消毒+节流）；
 *   · 大输入框：textarea 自增高（2~7 行），Enter 发送 / Shift+Enter 换行；
 *   · 圆角与留白放大（12~14px 半径、12~16px 内距），避免"挤在一起"。
 *
 * 会话协议与画布 FloatingAgent 同源：
 *   · 挂载时恢复历史：GET /api/sessions?canvas= 最近会话 → /entries 还原消息；
 *   · 首次发送 POST /api/sessions 建会话（绑定 canvas）；
 *   · streamNDJSON("/api/agent")：message_update / thinking_update / thinking_end /
 *     tool_execution_start / tool_execution_end（向上冒泡让宿主刷新数据）；
 *   · 运行中可取消（/api/agent/cancel + AbortController）。
 */
import { nextTick, onMounted, reactive, ref } from "vue";
import { getJSON, postJSON, streamNDJSON } from "../api.js";
import { parseDiffs } from "./diff.js";
import MarkdownText from "../components/MarkdownText.vue";

const props = defineProps({
  mode: { type: String, default: "dock" }, // dock | drawer
  canvas: { type: String, required: true },
  title: { type: String, default: "AI 助手" },
  chips: { type: Array, default: () => [] },
  /* async (diff) => boolean：应用字段修改；返回 true 则卡片置「已应用」 */
  applyHandler: { type: Function, default: null },
});
const emit = defineEmits(["canvas-changed"]);

const messages = reactive([]);
const input = ref("");
const running = ref(false);
const sessionId = ref("");
const bodyEl = ref(null);
const taEl = ref(null);
let controller = null;

/* 工具名 → 中文短标签：裸函数名对用户是噪音 */
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
  word_count: "统计字数",
  generate_media: "生成媒体",
  subagent: "委托子代理",
  ask_user: "向用户提问",
};
const toolLabel = (name) => TOOL_LABELS[name] || name;
const formatArgs = (args) => {
  try {
    return JSON.stringify(args, null, 2);
  } catch {
    return String(args ?? "");
  }
};

function scrollEnd() {
  nextTick(() => requestAnimationFrame(() => bodyEl.value?.scrollTo({ top: bodyEl.value.scrollHeight })));
}

function blankBot() {
  // think：推理模型的思考流；tools：挂在思考块里的步骤行
  return reactive({
    role: "bot", text: "", streaming: true, error: "",
    think: { text: "", streaming: false, startedAt: 0, seconds: 0, expanded: false, pinned: false },
    tools: [], diffs: [], applied: new Set(), dismissed: new Set(),
  });
}

/* 刷新后恢复历史：最近一条会话的 user/assistant 正文（思考流不落盘，只还原对话） */
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
      if (entry.role === "user") restored.push({ role: "user", text: content, think: null, tools: [] });
      else if (entry.role === "assistant" && !(entry.tool_calls || []).length)
        restored.push({ role: "bot", text: content, streaming: false, error: "", think: null, tools: [], diffs: [], applied: new Set(), dismissed: new Set() });
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
  autoGrow();
  messages.push({ role: "user", text: prompt, think: null, tools: [] });
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
          // 正文一来就收起思考块（用户手动展开过则尊重）
          if (bot.think && !bot.think.pinned) bot.think.expanded = false;
          bot.text += ev.delta || "";
          scrollEnd();
        } else if (ev.type === "thinking_update") {
          if (!bot.think.streaming) {
            bot.think.streaming = true;
            bot.think.startedAt = Date.now();
          }
          bot.think.expanded = true; // 思考流式时保持展开
          bot.think.text += ev.delta || "";
          scrollEnd();
        } else if (ev.type === "thinking_end") {
          bot.think.streaming = false;
          bot.think.seconds = bot.think.startedAt ? (Date.now() - bot.think.startedAt) / 1000 : 0;
          if (!bot.think.pinned) bot.think.expanded = false;
        } else if (ev.type === "tool_execution_start") {
          bot.tools.push({ id: ev.tool_call_id, name: ev.tool, args: ev.args, status: "run", ms: 0, result: "", open: false });
          if (bot.think && !bot.think.pinned) bot.think.expanded = true; // 有步骤在跑时亮出过程
          scrollEnd();
        } else if (ev.type === "tool_execution_end") {
          const card = bot.tools.find((t) => t.id === ev.tool_call_id) || bot.tools[bot.tools.length - 1];
          if (card) {
            card.status = ev.isError ? "err" : "done";
            card.ms = ev.duration_ms || 0;
            card.result = String(ev.result || "").slice(0, 260);
          }
          emit("canvas-changed");
          scrollEnd();
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
    if (bot.think) bot.think.expanded = bot.think.pinned === true && bot.think.expanded;
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

/* 大输入框：内容自适应 2~7 行 */
function autoGrow() {
  const el = taEl.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, 168)}px`;
}

function onThinkToggle(bot) {
  if (bot.think) bot.think.pinned = true; // 用户手动干预过，流式不再自动收展
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
        <p class="g-t">我在，这个项目里可以让我：</p>
        <p class="g-i">「解析出资产表」「重新分析剧本」—— 触发后台解析，进度在左侧面板看</p>
        <p class="g-i">「把林小满的人物小传细化一下」—— 直接改资产表</p>
        <p class="g-i">「把世界观改成民国背景」—— 直接改项目设定</p>
        <p class="g-i">改提示词、查过期原因、重拆分镜 —— 涉及修改的操作先给 diff，确认才应用</p>
      </div>

      <template v-for="(m, mi) in messages" :key="mi">
        <!-- 用户：右侧气泡 -->
        <div v-if="m.role === 'user'" class="row u">
          <div class="bubble user">{{ m.text }}</div>
        </div>

        <!-- AI：思考块 + 气泡 -->
        <div v-else class="row b">
          <details
            v-if="m.think && (m.think.text || m.tools.length)"
            class="think"
            :class="{ streaming: m.think.streaming }"
            :open="m.think.expanded"
            @toggle="m.think.expanded = $event.target.open; onThinkToggle(m)"
          >
            <summary class="think-head">
              <span class="chev">›</span>
              <span class="t-ind" :class="{ spin: m.think.streaming }"></span>
              <span class="t-label">
                {{ m.think.streaming ? "思考中…" : m.think.seconds ? `已深度思考 · ${m.think.seconds.toFixed(1)} 秒` : "思考过程" }}
                <em v-if="m.tools.length">· {{ m.tools.length }} 个步骤</em>
              </span>
            </summary>
            <div class="think-body">
              <MarkdownText v-if="m.think.text" :content="m.think.text" :streaming="m.think.streaming" class="think-md" />
              <div
                v-for="tl in m.tools"
                :key="tl.id"
                class="step"
                :class="tl.status"
              >
                <details :open="tl.status === 'run'">
                  <summary>
                    <span class="s-ico">{{ tl.status === "run" ? "◐" : tl.status === "err" ? "✕" : "✓" }}</span>
                    <span class="s-name">{{ toolLabel(tl.name) }}</span>
                    <span class="s-st num">{{ tl.status === "run" ? "执行中…" : tl.status === "err" ? "失败" : `${tl.ms}ms` }}</span>
                  </summary>
                  <pre class="s-args">{{ formatArgs(tl.args) }}</pre>
                  <div v-if="tl.result" class="s-res">{{ tl.result }}</div>
                </details>
              </div>
            </div>
          </details>

          <div v-if="m.text || m.streaming" class="bubble bot" :class="{ bare: !m.think || (!m.think.text && !m.tools.length) }">
            <MarkdownText :content="m.text" :streaming="m.streaming" />
          </div>
          <div v-if="m.error" class="bubble err">⚠ {{ m.error }}</div>

          <div v-for="(d, di) in m.diffs || []" :key="`${mi}-${di}`" class="diff" :class="{ applied: m.applied.has(di), dismissed: m.dismissed.has(di) }">
            <div class="cap-row">建议修改 · 片段 {{ d.segmentId || "?" }} ·【{{ d.field }}】</div>
            <div class="d-old">{{ d.old }}</div>
            <div class="d-new">{{ d.new }}</div>
            <div class="d-acts">
              <button class="btn sm primary" type="button" @click="apply(m, d, di)">应用修改</button>
              <button class="btn sm" type="button" @click="m.dismissed.add(di)">放弃</button>
            </div>
          </div>
        </div>
      </template>
    </div>

    <div class="ai-quick">
      <button v-for="c in chips" :key="c" class="q-chip" type="button" :disabled="running" @click="send(c)">{{ c }}</button>
    </div>

    <div class="composer">
      <div class="c-box">
        <textarea
          ref="taEl"
          v-model="input"
          rows="2"
          placeholder="给 AI 发消息…（Enter 发送，Shift+Enter 换行）"
          @keydown.enter.exact.prevent="send()"
          @input="autoGrow"
        ></textarea>
        <button v-if="running" class="send cancel" type="button" title="取消" @click="cancel">✕</button>
        <button v-else class="send" type="button" title="发送" :disabled="!input.trim()" @click="send()">↑</button>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.ai-panel { background: var(--bg-2); display: flex; flex-direction: column; min-height: 0; }
.ai-head { display: flex; align-items: baseline; gap: 10px; padding: 14px 16px; border-bottom: 1px solid var(--line); flex: none; }
.ai-head .nm { font-family: var(--serif); font-weight: 600; font-size: var(--fs-14); flex: 1; }
.st { display: inline-flex; align-items: center; gap: 6px; font-size: var(--fs-12); color: var(--ok); }
.st i { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); }

.ai-body { flex: 1; overflow-y: auto; padding: 16px 16px 10px; display: flex; flex-direction: column; gap: 14px; }
.row { display: flex; flex-direction: column; gap: 8px; }
.row.u { align-items: flex-end; }

/* 气泡：大圆角 + 足够内距 */
.bubble {
  border-radius: 14px;
  padding: 12px 14px;
  font-size: var(--fs-13);
  line-height: 1.85;
  color: var(--text-2);
  max-width: 100%;
}
.bubble.user {
  background: var(--card);
  border: 1px solid var(--line-2);
  color: var(--text);
  white-space: pre-wrap;
  max-width: 86%;
  border-radius: 14px 14px 4px 14px;
}
.bubble.bot { background: none; border: none; padding: 2px 4px; }
.bubble.bot.bare { padding: 2px 4px; }
.bubble.err { color: var(--bad); background: rgba(196, 74, 58, .08); border: 1px solid rgba(196, 74, 58, .25); display: inline-block; width: fit-content; }

/* 开场引导 */
.greet { border: 1px dashed var(--line-2); border-radius: 14px; padding: 14px 16px; color: var(--text-2); font-size: var(--fs-12); line-height: 1.9; }
.greet .g-t { color: var(--text); font-family: var(--serif); font-size: var(--fs-13); margin-bottom: 6px; }
.greet .g-i { padding: 2px 0 2px 12px; position: relative; }
.greet .g-i::before { content: ""; position: absolute; left: 0; top: 11px; width: 4px; height: 4px; border-radius: 50%; background: var(--gold); }

/* 思考块（DeepSeek 式）：默认收起，头部一读 */
.think { border: 1px solid var(--line); border-radius: 12px; background: var(--card); overflow: hidden; }
.think-head { display: flex; align-items: center; gap: 8px; padding: 9px 12px; cursor: pointer; list-style: none; user-select: none; font-size: var(--fs-12); color: var(--text-3); }
.think-head::-webkit-details-marker { display: none; }
.think .chev { display: inline-block; transition: transform 160ms var(--ease, ease); color: var(--text-3); font-size: var(--fs-14); line-height: 1; }
.think[open] .chev { transform: rotate(90deg); }
.t-ind { width: 8px; height: 8px; border-radius: 50%; border: 2px solid var(--line-3); border-top-color: var(--gold); }
.t-ind.spin { animation: think-spin 900ms linear infinite; }
@keyframes think-spin { to { transform: rotate(360deg); } }
.t-label { color: var(--text-2); }
.t-label em { font-style: normal; color: var(--text-3); }
.think-body { padding: 4px 14px 12px; border-top: 1px dashed var(--line); display: flex; flex-direction: column; gap: 8px; }
.think-md { font-size: var(--fs-12); color: var(--text-3); line-height: 1.8; }
.think.streaming .think-md { color: var(--text-3); }

/* 思考块里的步骤行 */
.step { border: 1px solid var(--line); border-radius: 10px; background: var(--bg-2); overflow: hidden; }
.step summary { display: flex; align-items: center; gap: 8px; padding: 7px 10px; cursor: pointer; list-style: none; font-size: var(--fs-12); }
.step summary::-webkit-details-marker { display: none; }
.step .s-ico { width: 14px; text-align: center; color: var(--ok); }
.step.run .s-ico { color: var(--gold); }
.step.err .s-ico { color: var(--bad); }
.step.err .s-name { color: var(--bad); }
.step .s-name { color: var(--text-2); }
.step .s-st { margin-left: auto; color: var(--text-3); font-size: var(--fs-11); }
.s-args { margin: 0; padding: 8px 12px; border-top: 1px dashed var(--line); font-family: var(--mono); font-size: var(--fs-11); color: var(--text-3); white-space: pre-wrap; word-break: break-all; max-height: 150px; overflow-y: auto; }
.s-res { padding: 6px 12px 9px; border-top: 1px dashed var(--line); font-size: var(--fs-11); color: var(--text-3); white-space: pre-wrap; word-break: break-all; max-height: 110px; overflow-y: auto; }

/* diff 卡片（人改优先） */
.diff { border: 1px solid var(--line-2); border-radius: 12px; overflow: hidden; }
.diff .cap-row { padding: 8px 12px; font-size: var(--fs-11); letter-spacing: .08em; color: var(--gold); background: var(--gold-soft); display: flex; align-items: center; }
.diff .d-old { padding: 9px 14px; color: var(--text-3); font-size: var(--fs-12); text-decoration: line-through; text-decoration-color: var(--line-3); border-bottom: 1px dashed var(--line-2); }
.diff .d-new { padding: 9px 14px; color: var(--text); font-size: var(--fs-12); background: rgba(194, 163, 107, .05); }
.diff .d-acts { display: flex; gap: 8px; padding: 10px 14px; border-top: 1px solid var(--line); }
.diff.applied .d-acts { display: none; }
.diff.applied .cap-row::after { content: "已应用 ✓"; margin-left: auto; color: var(--ok); }
.diff.dismissed { display: none; }

.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; padding: 9px 20px; border-radius: 10px; font-size: var(--fs-14); color: var(--text-2); transition: all var(--dur) var(--ease); }
.btn:hover { color: var(--text); background: var(--card-2); }
.btn.primary { background: var(--accent); color: var(--text-on-accent, #FFF7F0); }
.btn.primary:hover { background: var(--accent-hi); }
.btn.sm { padding: 6px 14px; font-size: var(--fs-13); }

/* 快捷指令 */
.ai-quick { padding: 4px 16px 8px; display: flex; flex-wrap: wrap; gap: 6px; flex: none; }
.q-chip { font-size: var(--fs-12); color: var(--text-3); border: 1px solid var(--line-2); border-radius: 999px; padding: 6px 14px; background: none; }
.q-chip:hover { color: var(--gold); border-color: rgba(194, 163, 107, .4); }
.q-chip:disabled { opacity: .5; }

/* 大输入框：圆角容器 + 内嵌发送键 */
.composer { padding: 6px 16px 14px; flex: none; }
.c-box { position: relative; border: 1px solid var(--line-2); border-radius: 14px; background: var(--card); transition: border-color 160ms var(--ease, ease); }
.c-box:focus-within { border-color: rgba(201, 85, 61, .55); }
.c-box textarea {
  display: block; width: 100%; min-height: 56px; max-height: 168px;
  resize: none; border: none; background: none; outline: none;
  padding: 12px 52px 12px 14px;
  color: var(--text); font: inherit; font-size: var(--fs-13); line-height: 1.7;
}
.send {
  position: absolute; right: 10px; bottom: 10px;
  width: 34px; height: 34px; border-radius: 10px; border: none;
  background: var(--accent); color: var(--text-on-accent, #FFF7F0);
  display: grid; place-items: center; font-size: var(--fs-14); cursor: pointer;
}
.send:hover { background: var(--accent-hi); }
.send:disabled { opacity: .45; cursor: default; }
.send.cancel { background: var(--card-3); color: var(--text-2); }
</style>
