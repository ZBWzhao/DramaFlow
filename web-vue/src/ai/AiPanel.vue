<script setup>
/* AI 助手面板 v3 —— 参考 DeepSeek 移动端形态（用户指定）：
 *
 *   头部  头像 + 「AI 助手 / 状态行」 + 新建 + 历史
 *   气泡  用户右（复制），AI 左带头像；正文 MarkdownText + 面板内建完整排版样式
 *   思考  thinking_update 流式展开，出正文收起；工具/SKILL 步骤行在思考块里
 *   问答  ask_user → input_required 事件 → 问题卡片（选项可点，回答后 agent 从原工具继续）
 *   输入  大输入框自增高，Enter 发送 / Shift+Enter 换行，圆形发送键
 *
 * 会话协议：挂载恢复最近会话；POST /api/sessions 建会话；streamNDJSON(/api/agent)；
 * 答题走 POST /api/sessions/{sid}/questions/{qid}/answer（后端 QuestionBroker）。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from "vue";
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
const busyAnswer = ref(false);
const qError = ref("");
/* 刷新后恢复：上一次运行可能还挂在服务端（ask_user 等待/断网）——
 * 挂着时新消息会被 409 拦下，必须给横幅 + 停止入口，并把待答问题接回来。 */
const staleRun = ref(false);
let controller = null;

/* ---------- 状态行（头部第二行） ---------- */
const statusText = computed(() => {
  if (!running.value) return "已就绪";
  const bot = [...messages].reverse().find((m) => m.role === "bot");
  if (bot?.question?.status === "pending") return "等待你的回答";
  if (bot?.think?.streaming) return "思考中…";
  const tool = bot?.tools?.[bot.tools.length - 1];
  if (tool?.status === "run") return `正在${toolLabel(tool.name)}…`;
  return "执行中…";
});

/* ---------- 工具名 → 中文短标签 ---------- */
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
  capabilities: "查询能力",
  list_media_models: "查询模型",
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
  return reactive({
    role: "bot", text: "", streaming: true, error: "",
    think: { text: "", streaming: false, startedAt: 0, seconds: 0, expanded: false, pinned: false },
    tools: [], diffs: [], applied: new Set(), dismissed: new Set(),
    question: null, // { id, title, question, options, allow_custom, status, custom, answer }
  });
}
function blankUser(text) {
  return { role: "user", text };
}

/* ---------- 历史恢复 / 会话切换 ---------- */
function renderEntries(entries) {
  const out = [];
  for (const entry of entries || []) {
    const content = entry?.content;
    if (typeof content !== "string" || !content.trim() || content === "[之前的对话摘要]") continue;
    if (entry.role === "user") out.push(blankUser(content));
    else if (entry.role === "assistant" && !(entry.tool_calls || []).length)
      out.push({ ...blankBot(), streaming: false, text: content, think: null });
  }
  return out;
}
async function restoreHistory() {
  try {
    const data = await getJSON(`/api/sessions?canvas=${encodeURIComponent(props.canvas)}`);
    const latest = (data.sessions || [])[0];
    if (!latest) return;
    sessionId.value = latest.id;
    staleRun.value = !!latest.running;
    const detail = await getJSON(`/api/sessions/${latest.id}/entries?limit=60`);
    messages.push(...renderEntries(detail.entries).slice(-30));
    if (staleRun.value) await restorePendingQuestion();
    scrollEnd();
  } catch {
    /* 无历史/接口不可用都是常态，静默开局 */
  }
}
/* 挂起的运行可能在等用户回答：把待答问题接回成可答卡片 */
async function restorePendingQuestion() {
  try {
    const data = await getJSON(`/api/sessions/${sessionId.value}/questions`);
    const pq = (data.questions || [])[0];
    if (!pq) return;
    const bot = { ...blankBot(), streaming: false, text: "", think: null };
    bot.question = reactive({
      id: pq.question_id, title: pq.title || "需要你的选择", question: pq.question || "",
      options: pq.options || [], allow_custom: pq.allow_custom !== false,
      status: "pending", custom: "", answer: "", stale: true,
    });
    messages.push(bot);
  } catch {
    /* 端点不可用时只显示横幅 */
  }
}
async function stopStaleRun() {
  if (!sessionId.value) return;
  try {
    await postJSON("/api/agent/cancel", { session_id: sessionId.value });
  } catch {
    /* 取消失败保持横幅，用户可重试 */
  }
  staleRun.value = false;
}
onMounted(restoreHistory);
onBeforeUnmount(() => controller?.abort());

/* ---------- 新建 / 历史 ---------- */
const historyOpen = ref(false);
const sessions = ref([]);
async function newChat() {
  if (running.value) return;
  try {
    const detail = await postJSON("/api/sessions", { canvas: props.canvas, title: props.title });
    sessionId.value = detail.id;
  } catch {
    sessionId.value = ""; // 下次发送时再建
  }
  messages.splice(0);
  staleRun.value = false;
  historyOpen.value = false;
}
async function toggleHistory() {
  historyOpen.value = !historyOpen.value;
  if (!historyOpen.value) return;
  try {
    const data = await getJSON(`/api/sessions?canvas=${encodeURIComponent(props.canvas)}`);
    sessions.value = data.sessions || [];
  } catch {
    sessions.value = [];
  }
}
async function loadSession(s) {
  if (running.value) return;
  try {
    const detail = await getJSON(`/api/sessions/${s.id}/entries?limit=60`);
    sessionId.value = s.id;
    messages.splice(0);
    messages.push(...renderEntries(detail.entries).slice(-30));
    scrollEnd();
  } catch {
    /* 加载失败保持现状 */
  }
  historyOpen.value = false;
}
const fmtTime = (ts) => {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};

/* ---------- 发送 / 取消 ---------- */
function send(text) {
  const prompt = String(text ?? input.value ?? "").trim();
  if (!prompt || running.value || staleRun.value) return;
  input.value = "";
  autoGrow();
  messages.push(blankUser(prompt));
  reply(prompt);
}

async function reply(prompt) {
  const bot = blankBot();
  messages.push(bot);
  running.value = true;
  controller = new AbortController();
  try {
    if (!sessionId.value) {
      const detail = await postJSON("/api/sessions", { canvas: props.canvas, title: props.title });
      sessionId.value = detail.id;
    }
    await streamNDJSON(
      "/api/agent",
      { prompt, session_id: sessionId.value || null, canvas: props.canvas, media_policy: "plan_only" },
      (ev) => {
        if (ev.type === "message_update") {
          if (bot.think && !bot.think.pinned) bot.think.expanded = false;
          bot.text += ev.delta || "";
          scrollEnd();
        } else if (ev.type === "thinking_update") {
          if (!bot.think.streaming) {
            bot.think.streaming = true;
            bot.think.startedAt = Date.now();
          }
          bot.think.expanded = true;
          bot.think.text += ev.delta || "";
          scrollEnd();
        } else if (ev.type === "thinking_end") {
          bot.think.streaming = false;
          bot.think.seconds = bot.think.startedAt ? (Date.now() - bot.think.startedAt) / 1000 : 0;
          if (bot.think.pinned !== true) bot.think.expanded = false;
        } else if (ev.type === "tool_execution_start") {
          bot.tools.push({ id: ev.tool_call_id, name: ev.tool, args: ev.args, status: "run", ms: 0, result: "", open: false });
          if (bot.think && !bot.think.pinned) bot.think.expanded = true;
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
        } else if (ev.type === "input_required") {
          // agent 暂停等用户：问题卡片挂到当前消息，光标停转（showCursor 判定）
          bot.question = reactive({
            id: ev.question_id, title: ev.title || "需要你的选择", question: ev.question || "",
            options: ev.options || [], allow_custom: ev.allow_custom !== false,
            status: "pending", custom: "", answer: "",
          });
          scrollEnd();
        } else if (ev.type === "input_resolved") {
          for (const m of messages) {
            if (m.role === "bot" && m.question?.id === ev.question_id) {
              m.question.status = "answered";
              m.question.answer = ev.answer || "";
            }
          }
          messages.push(blankUser(ev.answer || ""));
          scrollEnd();
        } else if (ev.type === "input_cancelled") {
          for (const m of messages) {
            if (m.role === "bot" && m.question?.id === ev.question_id) m.question.status = "cancelled";
          }
        }
      },
      (err) => {
        bot.error = err?.message || String(err);
      },
      controller.signal,
    );
  } catch (err) {
    const msg = err?.message || String(err);
    // 上一次运行没结束时后端回 409：转成横幅 + 停止入口，不吓唬用户
    if (msg.includes("already running") || msg.includes("未结束")) {
      staleRun.value = true;
      bot.error = "上一次运行还没结束，点上方「停止它」后再发新消息";
    } else {
      bot.error = msg;
    }
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
  if (sessionId.value) postJSON("/api/agent/cancel", { session_id: sessionId.value }).catch(() => {});
}

/* ---------- 回答 ask_user ---------- */
async function answerQuestion(m, text) {
  const q = m.question;
  const val = String(text ?? "").trim();
  if (!q || q.status !== "pending" || !val) return;
  if (!sessionId.value || busyAnswer.value) return;
  busyAnswer.value = true;
  qError.value = "";
  try {
    await postJSON(`/api/sessions/${sessionId.value}/questions/${q.id}/answer`, { answer: val });
    // input_resolved 事件回推：卡片置已回答、回答进气泡流、agent 继续输出
  } catch (err) {
    qError.value = err?.message || String(err);
  } finally {
    busyAnswer.value = false;
  }
}

/* ---------- diff 应用 / 复制 / 自增高 ---------- */
async function apply(message, diff, idx) {
  if (!props.applyHandler) return;
  try {
    if (await props.applyHandler(diff)) message.applied.add(idx);
  } catch (err) {
    console.warn("[AiPanel] apply failed:", err);
  }
}
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    /* 剪贴板不可用时静默 */
  }
}
function autoGrow() {
  const el = taEl.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, 168)}px`;
}
function onThinkToggle(bot) {
  if (bot.think) bot.think.pinned = true;
}
const showCursor = (m) => m.streaming && !(m.question && m.question.status === "pending");

defineExpose({ send });
</script>

<template>
  <aside class="ai-panel" :class="`is-${mode}`">
    <!-- ── 头部：头像 + 名称/状态 + 新建/历史 ── -->
    <div class="ai-head">
      <span class="avatar">AI</span>
      <span class="id-block">
        <span class="nm">{{ title }}</span>
        <span class="status" :class="{ wait: running }">{{ statusText }}</span>
      </span>
      <span class="grow"></span>
      <button class="h-btn" type="button" :disabled="running" title="开启新对话" @click="newChat">
        <span class="h-ico">＋</span>新建
      </button>
      <button class="h-btn" type="button" :class="{ on: historyOpen }" title="历史会话" @click="toggleHistory">
        <span class="h-ico">🕘</span>历史
      </button>
      <slot name="head-extra" />
    </div>

    <!-- 停滞运行横幅 -->
    <div v-if="staleRun" class="stale-bar">
      <span class="s-txt">上一次运行还没结束（可能在等你回答）</span>
      <button class="ui-btn sm" type="button" @click="stopStaleRun">停止它</button>
    </div>

    <!-- 历史下拉（只列当前项目的会话） -->
    <div v-if="historyOpen" class="history">
      <div class="h-cap num">{{ canvas }} · 历史会话 {{ sessions.length ? `(${sessions.length})` : "" }}</div>
      <div v-if="!sessions.length" class="h-empty">这个项目还没有历史会话</div>
      <button v-for="s in sessions" :key="s.id" class="h-item" type="button" @click="loadSession(s)">
        <span class="h-title">{{ s.title || "未命名会话" }}</span>
        <span class="h-meta num">{{ s.messages ?? 0 }} 条 · {{ fmtTime(s.updated_at) }}</span>
      </button>
    </div>

    <!-- ── 消息流 ── -->
    <div ref="bodyEl" class="ai-body">
      <div v-if="!messages.length" class="greet">
        <div class="g-hero">把活儿交给我</div>
        <p class="g-i">「解析出资产表」「重新分析剧本」—— 触发后台解析，进度在左侧面板看</p>
        <p class="g-i">「细化林小满的人物小传」—— 直接改资产表</p>
        <p class="g-i">「把世界观改成民国背景」—— 直接改项目设定</p>
        <p class="g-i">改提示词、查过期原因 —— 涉及修改的操作先给 diff，确认才应用</p>
      </div>

      <template v-for="(m, mi) in messages" :key="mi">
        <!-- 用户：右气泡 + 复制 -->
        <div v-if="m.role === 'user'" class="row u">
          <div class="bubble user">{{ m.text }}</div>
          <button class="copy" type="button" @click="copyText(m.text)">复制</button>
        </div>

        <!-- AI：头像 + 思考块 + 气泡 -->
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
              <MarkdownText v-if="m.think.text" :content="m.think.text" :streaming="m.think.streaming" class="md think-md" />
              <div v-for="tl in m.tools" :key="tl.id" class="step" :class="tl.status">
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

          <div class="b-line">
            <span v-if="m.text || m.streaming || m.question" class="avatar sm">AI</span>
            <div class="b-col">
              <div v-if="m.text || m.streaming" class="bubble bot">
                <MarkdownText :content="m.text" :streaming="m.streaming" class="md" />
                <span v-if="showCursor(m)" class="cursor"></span>
              </div>
              <div v-if="m.error" class="bubble errb">⚠ {{ m.error }}</div>

              <!-- ask_user 问题卡片 -->
              <div v-if="m.question" class="q-card" :class="{ done: m.question.status === 'answered' }">
                <div class="q-head"><span class="q-ico">?</span>{{ m.question.title }}</div>
                <div class="q-body">{{ m.question.question }}</div>
                <div v-if="m.question.status === 'pending'" class="q-opts">
                  <button
                    v-for="opt in m.question.options"
                    :key="opt"
                    class="q-opt"
                    type="button"
                    :disabled="busyAnswer"
                    @click="answerQuestion(m, opt)"
                  >
                    {{ opt }}
                  </button>
                </div>
                <div v-if="m.question.status === 'pending' && m.question.allow_custom" class="q-custom">
                  <input
                    v-model="m.question.custom"
                    placeholder="或者输入你自己的答案…"
                    :disabled="busyAnswer"
                    @keydown.enter="answerQuestion(m, m.question.custom)"
                  />
                  <button class="q-send" type="button" :disabled="busyAnswer || !m.question.custom.trim()" @click="answerQuestion(m, m.question.custom)">发送</button>
                </div>
                <div v-if="m.question.status === 'answered'" class="q-done">
                  已回答 · {{ m.question.answer }}
                  <span v-if="m.question.stale" class="q-hint">AI 在后台继续，稍后刷新页面可见后续输出</span>
                </div>
                <div v-if="qError" class="q-err">{{ qError }}</div>
              </div>

              <!-- diff 卡片（人改优先） -->
              <div v-for="(d, di) in m.diffs || []" :key="`${mi}-${di}`" class="diff" :class="{ applied: m.applied.has(di), dismissed: m.dismissed.has(di) }">
                <div class="cap-row">建议修改 · 片段 {{ d.segmentId || "?" }} ·【{{ d.field }}】</div>
                <div class="d-old">{{ d.old }}</div>
                <div class="d-new">{{ d.new }}</div>
                <div class="d-acts">
                  <button class="ui-btn primary sm" type="button" @click="apply(m, d, di)">应用修改</button>
                  <button class="ui-btn sm" type="button" @click="m.dismissed.add(di)">放弃</button>
                </div>
              </div>

              <button v-if="m.text && !m.streaming" class="copy" type="button" @click="copyText(m.text)">复制</button>
            </div>
          </div>
        </div>
      </template>
    </div>

    <!-- ── 快捷指令 + 大输入框 ── -->
    <div class="composer">
      <div v-if="chips.length" class="ai-quick">
        <button v-for="c in chips" :key="c" class="q-chip" type="button" :disabled="running" @click="send(c)">{{ c }}</button>
      </div>
      <div class="c-box">
        <textarea
          ref="taEl"
          v-model="input"
          rows="2"
          placeholder="告诉 AI 你想做什么…（Enter 发送）"
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
.ai-panel { background: var(--bg-2); display: flex; flex-direction: column; min-height: 0; height: 100%; }

/* ---------- 头部 ---------- */
.ai-head { display: flex; align-items: center; gap: 10px; padding: 12px 14px; border-bottom: 1px solid var(--line); flex: none; position: relative; z-index: 5; background: var(--bg-2); }
.avatar {
  width: 34px; height: 34px; border-radius: 50%; flex: none;
  display: grid; place-items: center; font-family: var(--serif); font-weight: 600; font-size: var(--fs-13);
  color: #FFF7F0; background: linear-gradient(140deg, var(--accent), #8E3A28);
  box-shadow: inset 0 1px 0 rgba(255, 251, 243, .25);
}
.avatar.sm { width: 26px; height: 26px; font-size: var(--fs-11); }
.id-block { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.id-block .nm { font-family: var(--serif); font-weight: 600; font-size: var(--fs-14); color: var(--text); line-height: 1.3; }
.id-block .status { font-size: var(--fs-11); color: var(--text-3); line-height: 1.3; }
.id-block .status.wait { color: var(--gold); }
.grow { flex: 1; }
.h-btn {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: var(--fs-12); color: var(--text-3); background: none; border: none;
  padding: 6px 8px; border-radius: 8px;
}
.h-btn:hover { color: var(--text); background: var(--card); }
.h-btn.on { color: var(--gold); }
.h-btn:disabled { opacity: .4; }
.h-ico { font-size: var(--fs-13); line-height: 1; }

/* 历史下拉（absolute 悬浮，不挤消息流）。
 * 底色要和聊天区拉开一档（#1e1d1b vs #121110），否则边界隐形像「盖住内容」。 */
.history {
  position: absolute; top: 58px; right: 10px; z-index: 40;
  width: min(320px, calc(100% - 20px)); max-height: 340px; overflow-y: auto;
  background: #1e1d1b; border: 1px solid rgba(255, 251, 243, .16); border-radius: 12px;
  box-shadow: 0 20px 56px rgba(0, 0, 0, .72), 0 2px 8px rgba(0, 0, 0, .5); padding: 6px;
}
.h-cap { padding: 8px 12px 7px; color: var(--text-3); font-size: var(--fs-11); letter-spacing: .04em; border-bottom: 1px solid var(--line); margin-bottom: 4px; }
.h-empty { padding: 16px; text-align: center; color: var(--text-3); font-size: var(--fs-12); }
.h-item { display: flex; flex-direction: column; gap: 3px; width: 100%; text-align: left; padding: 9px 12px; border-radius: 8px; background: none; border: none; }
.h-item:hover { background: var(--card); }
.h-item .h-title { font-size: var(--fs-13); color: var(--text-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.h-item .h-meta { font-size: var(--fs-11); color: var(--text-3); }

/* ---------- 消息流 ---------- */
.ai-body { flex: 1; min-height: 0; overflow-y: auto; padding: 18px 16px 10px; display: flex; flex-direction: column; gap: 18px; }
.row { display: flex; flex-direction: column; gap: 6px; }
.row.u { align-items: flex-end; }
.row.b { align-items: stretch; }
.b-line { display: flex; gap: 8px; align-items: flex-start; }
.b-line .avatar.sm { margin-top: 2px; }
.b-col { display: flex; flex-direction: column; gap: 8px; min-width: 0; flex: 1; }

.bubble { border-radius: 14px; padding: 12px 15px; font-size: var(--fs-14); line-height: 1.9; color: var(--text-2); max-width: 100%; }
.bubble.user {
  background: var(--card); border: 1px solid var(--line-2); color: var(--text);
  white-space: pre-wrap; max-width: 86%; border-radius: 14px 14px 4px 14px;
}
.bubble.bot { background: var(--card); border: 1px solid var(--line); border-radius: 4px 14px 14px 14px; position: relative; }
.bubble.errb { color: var(--bad); background: rgba(196, 74, 58, .08); border: 1px solid rgba(196, 74, 58, .25); display: inline-block; width: fit-content; }
.cursor { display: inline-block; width: 7px; height: 15px; margin-left: 2px; vertical-align: -2px; background: var(--gold); animation: blink 1s steps(2) infinite; }
@keyframes blink { 50% { opacity: 0; } }

.copy { align-self: flex-end; background: none; border: none; color: var(--text-3); font-size: var(--fs-11); padding: 2px 6px; border-radius: 6px; opacity: 0; }
.row:hover .copy, .b-col:hover .copy { opacity: 1; }
.copy:hover { color: var(--text); background: var(--card); }

/* ---------- 开场引导 ---------- */
.greet { border: 1px dashed var(--line-2); border-radius: 14px; padding: 18px 18px 14px; color: var(--text-3); font-size: var(--fs-12); line-height: 1.9; }
.greet .g-hero { font-family: var(--serif); font-size: var(--fs-16); color: var(--text); margin-bottom: 8px; }
.greet .g-i { padding: 3px 0 3px 14px; position: relative; }
.greet .g-i::before { content: ""; position: absolute; left: 0; top: 13px; width: 4px; height: 4px; border-radius: 50%; background: var(--gold); }

/* ---------- Markdown 排版（面板内建，不依赖外部上下文） ---------- */
.md :deep(p) { margin: 0 0 10px; line-height: 1.9; }
.md :deep(p:last-child) { margin-bottom: 0; }
.md :deep(h1), .md :deep(h2), .md :deep(h3), .md :deep(h4) { margin: 16px 0 8px; line-height: 1.5; color: var(--text); font-weight: 600; }
.md :deep(h1) { font-size: var(--fs-16); }
.md :deep(h2) { font-size: var(--fs-14); }
.md :deep(h3) { font-size: var(--fs-13); }
.md :deep(h4) { font-size: var(--fs-13); color: var(--text-2); }
.md :deep(h1:first-child), .md :deep(h2:first-child), .md :deep(h3:first-child) { margin-top: 0; }
.md :deep(ul), .md :deep(ol) { margin: 4px 0 12px; padding-left: 20px; }
.md :deep(li) { margin: 5px 0; line-height: 1.85; }
.md :deep(li::marker) { color: var(--text-3); }
.md :deep(strong) { color: var(--text); font-weight: 600; }
.md :deep(code) { background: var(--card-2); border: 1px solid var(--line); border-radius: 6px; padding: 1px 6px; font-family: var(--mono); font-size: var(--fs-12); color: var(--gold); }
.md :deep(pre) { background: var(--bg); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; overflow-x: auto; margin: 10px 0 14px; }
.md :deep(pre code) { background: none; border: none; padding: 0; color: var(--text-2); }
.md :deep(blockquote) { border-left: 3px solid var(--gold-soft); padding: 2px 12px; margin: 10px 0; color: var(--text-3); }
.md :deep(hr) { border: none; border-top: 1px solid var(--line); margin: 14px 0; }
.md :deep(a) { color: var(--gold); text-decoration: none; }
.md :deep(a:hover) { text-decoration: underline; }
.md :deep(table) { width: 100%; border-collapse: collapse; font-size: var(--fs-12); margin: 10px 0 14px; }
.md :deep(th), .md :deep(td) { border-bottom: 1px solid var(--line); padding: 7px 10px; text-align: left; line-height: 1.7; }
.md :deep(th) { color: var(--text-3); font-weight: 500; font-size: var(--fs-11); }

/* 思考块里的思考文本：弱化排版 */
.think-md { font-size: var(--fs-12); color: var(--text-3); }
.think-md :deep(p) { margin: 0 0 8px; line-height: 1.8; }

/* ---------- 思考块 ---------- */
.think { border: 1px solid var(--line); border-radius: 12px; background: var(--bg); overflow: hidden; }
.think-head { display: flex; align-items: center; gap: 8px; padding: 9px 12px; cursor: pointer; list-style: none; user-select: none; font-size: var(--fs-12); color: var(--text-3); }
.think-head::-webkit-details-marker { display: none; }
.think .chev { display: inline-block; transition: transform 160ms var(--ease, ease); color: var(--text-3); font-size: var(--fs-14); line-height: 1; }
.think[open] .chev { transform: rotate(90deg); }
.t-ind { width: 8px; height: 8px; border-radius: 50%; border: 2px solid var(--line-3); border-top-color: var(--gold); flex: none; }
.t-ind.spin { animation: think-spin 900ms linear infinite; }
@keyframes think-spin { to { transform: rotate(360deg); } }
.t-label { color: var(--text-2); }
.t-label em { font-style: normal; color: var(--text-3); }
.think-body { padding: 6px 12px 12px; border-top: 1px dashed var(--line); display: flex; flex-direction: column; gap: 8px; }

/* 步骤行 */
.step { border: 1px solid var(--line); border-radius: 10px; background: var(--bg-2); overflow: hidden; }
.step summary { display: flex; align-items: center; gap: 8px; padding: 7px 10px; cursor: pointer; list-style: none; font-size: var(--fs-12); }
.step summary::-webkit-details-marker { display: none; }
.step .s-ico { width: 14px; text-align: center; color: var(--ok); flex: none; }
.step.run .s-ico { color: var(--gold); }
.step.err .s-ico { color: var(--bad); }
.step.err .s-name { color: var(--bad); }
.step .s-name { color: var(--text-2); }
.step .s-st { margin-left: auto; color: var(--text-3); font-size: var(--fs-11); }
.s-args { margin: 0; padding: 8px 12px; border-top: 1px dashed var(--line); font-family: var(--mono); font-size: var(--fs-11); color: var(--text-3); white-space: pre-wrap; word-break: break-all; max-height: 150px; overflow-y: auto; }
.s-res { padding: 6px 12px 9px; border-top: 1px dashed var(--line); font-size: var(--fs-11); color: var(--text-3); white-space: pre-wrap; word-break: break-all; max-height: 110px; overflow-y: auto; }

/* ---------- ask_user 问题卡片 ---------- */
.q-card { border: 1px solid rgba(194, 163, 107, .35); border-radius: 12px; background: var(--gold-soft); padding: 12px 14px; display: flex; flex-direction: column; gap: 10px; }
.q-card.done { opacity: .75; background: var(--card); border-color: var(--line-2); }
.q-head { display: flex; align-items: center; gap: 8px; font-family: var(--serif); font-size: var(--fs-13); color: var(--gold); font-weight: 600; }
.q-ico { width: 18px; height: 18px; border-radius: 50%; background: var(--gold); color: #14110E; display: grid; place-items: center; font-size: var(--fs-11); font-weight: 700; flex: none; }
.q-body { font-size: var(--fs-13); color: var(--text-2); line-height: 1.8; white-space: pre-wrap; }
.q-opts { display: flex; flex-direction: column; gap: 6px; }
.q-opt {
  text-align: left; padding: 9px 14px; border-radius: 10px; font-size: var(--fs-13);
  color: var(--text); background: var(--card); border: 1px solid var(--line-2); cursor: pointer;
  transition: all var(--dur) var(--ease); line-height: 1.6;
}
.q-opt:hover { border-color: var(--gold); color: var(--gold); }
.q-opt:disabled { opacity: .5; }
.q-custom { display: flex; gap: 8px; }
.q-custom input {
  flex: 1; background: var(--card); border: 1px solid var(--line-2); border-radius: 10px;
  padding: 8px 12px; color: var(--text); font: inherit; font-size: var(--fs-13); outline: none; min-width: 0;
}
.q-custom input:focus { border-color: rgba(194, 163, 107, .5); }
.q-send { padding: 8px 14px; border-radius: 10px; border: none; background: var(--gold); color: #14110E; font-size: var(--fs-12); cursor: pointer; flex: none; }
.q-send:disabled { opacity: .45; }
.q-done { font-size: var(--fs-12); color: var(--ok); display: flex; flex-direction: column; gap: 4px; }
.q-done .q-hint { color: var(--text-3); font-size: var(--fs-11); }
.q-err { font-size: var(--fs-11); color: var(--bad); }

/* 停滞运行横幅 */
.stale-bar {
  display: flex; align-items: center; gap: 10px; flex: none;
  margin: 8px 14px 0; padding: 8px 8px 8px 14px;
  background: var(--warn-soft); border: 1px solid rgba(201, 138, 75, .35); border-radius: 10px;
}
.stale-bar .s-txt { flex: 1; font-size: var(--fs-12); color: var(--warn); line-height: 1.6; }

/* ---------- diff 卡片 ---------- */
.diff { border: 1px solid var(--line-2); border-radius: 12px; overflow: hidden; }
.diff .cap-row { padding: 8px 12px; font-size: var(--fs-11); letter-spacing: .08em; color: var(--gold); background: var(--gold-soft); display: flex; align-items: center; }
.diff .d-old { padding: 9px 14px; color: var(--text-3); font-size: var(--fs-12); text-decoration: line-through; text-decoration-color: var(--line-3); border-bottom: 1px dashed var(--line-2); }
.diff .d-new { padding: 9px 14px; color: var(--text); font-size: var(--fs-12); background: rgba(194, 163, 107, .05); }
.diff .d-acts { display: flex; gap: 8px; padding: 10px 14px; border-top: 1px solid var(--line); }
.diff.applied .d-acts { display: none; }
.diff.applied .cap-row::after { content: "已应用 ✓"; margin-left: auto; color: var(--ok); }
.diff.dismissed { display: none; }

/* ---------- 输入区 ---------- */
.composer { padding: 8px 14px 14px; flex: none; }
.ai-quick { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.q-chip { font-size: var(--fs-12); color: var(--text-3); border: 1px solid var(--line-2); border-radius: 999px; padding: 6px 14px; background: none; }
.q-chip:hover { color: var(--gold); border-color: rgba(194, 163, 107, .4); }
.q-chip:disabled { opacity: .5; }
.c-box { position: relative; border: 1px solid var(--line-2); border-radius: 16px; background: var(--card); transition: border-color 160ms var(--ease, ease); }
.c-box:focus-within { border-color: rgba(201, 85, 61, .55); }
.c-box textarea {
  display: block; width: 100%; min-height: 56px; max-height: 168px;
  resize: none; border: none; background: none; outline: none;
  padding: 13px 54px 13px 16px;
  color: var(--text); font: inherit; font-size: var(--fs-13); line-height: 1.7;
}
.send {
  position: absolute; right: 10px; bottom: 10px;
  width: 36px; height: 36px; border-radius: 50%; border: none;
  background: var(--accent); color: var(--text-on-accent);
  display: grid; place-items: center; font-size: var(--fs-16); cursor: pointer;
  box-shadow: 0 4px 14px rgba(201, 85, 61, .35);
}
.send:hover { background: var(--accent-hi); }
.send:disabled { opacity: .4; box-shadow: none; }
.send.cancel { background: var(--card-3); color: var(--text-2); box-shadow: none; font-size: var(--fs-13); }
</style>
