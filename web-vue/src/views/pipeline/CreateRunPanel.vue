<script setup>
/* 创作入口（阶段 2 Task 4）：粘贴小说 → Agent 逐阶段直播 → 大纲人工确认。
 *
 * 三段式：① 入口（textarea，≥50 字才可点开始，空/等待/错误态齐全）→
 * ② 运行卡（SSE 事件倒序展示：工具调用摘要行 + 质量门结果行，确认门/失败/
 * 完成行高亮）→ ③ gated 时在下方渲染大纲草案（复用 PipelineOutlinePanel
 * 只读视图）+ 确认卡。确认门红线：gate 之后 UI 停在确认卡，放行只有
 * 「确认，开写剧本」一个入口，不提供任何跳过路径。
 * 执行器事件是尽力而为的 JSON：describe/lineClass 全部防御取值，未知类型
 * 渲染原文，绝不让整块渲染崩掉。
 */
import { computed, ref } from "vue";
import { ElMessage } from "element-plus";
import { errorText } from "../../api.js";
import { approveRun, retryRun, sendRunFeedback, startRun, studio } from "../../studio/studioStore.js";
import PipelineOutlinePanel from "../script/PipelineOutlinePanel.vue";

const novel = ref("");
const starting = ref(false);
const entryError = ref("");
const approving = ref(false);
const gateError = ref("");
const feedback = ref("");
const sendingFeedback = ref(false);
const retryError = ref("");
const outlineEl = ref(null); // 「查看大纲并确认」的滚动目标

const run = computed(() => studio.run);
/* 归属判定：运行卡（含确认门/重试/意见）只在本项目的页面上渲染——A 项目的活动
 * run 绝不出现在 B 项目的 PipelineView，否则会看着 B 的大纲缓存给 A 的 run 点
 * 确认（review 修复）。不匹配时回落到入口卡片；SSE 流在 store 层继续，不受影响。 */
const runHere = computed(() => Boolean(run.value.id) && run.value.project_id === studio.canvas);
const charCount = computed(() => novel.value.trim().length);
const canStart = computed(() => charCount.value >= 50 && Boolean(studio.canvas) && !starting.value);

/* 事件倒序展示：最新一帧在最上；原始序号做 key（append-only，稳定） */
const reversedEvents = computed(() => run.value.events.slice().reverse());
/* 最近一次 run 级失败事件：findings 从事件帧取，不另设 store 字段 */
const failEvent = computed(() => reversedEvents.value.find((e) => e.type === "failed") || null);

/* gated 确认对象：store 在 gate 事件时已 loadPipeline(true) 强制重拉 */
const outlineContent = computed(() => studio.pipeline?.detail?.outline?.content || null);

const STATUS_TEXT = {
  running: "运行中",
  gated: "等待你的确认",
  failed: "运行失败",
  done: "已完成",
  cancelled: "已取消",
};
const STAGE_LABELS = {
  outline: "大纲",
  script: "剧本",
  characters: "角色设定",
  art: "美术设定",
  staging: "开场站位",
  storyboard: "分镜",
};
const stageLabel = (s) => STAGE_LABELS[s] || s || "";

const KIND_LABELS = {
  hello: "连接",
  stage: "阶段",
  tool: "工具",
  tool_result: "回执",
  stage_gate: "质量门",
  stage_done: "收尾",
  gate: "确认门",
  retry: "重试",
  failed: "失败",
  done: "完成",
};
const kindLabel = (ev) => KIND_LABELS[ev?.type] || ev?.type || "事件";

function describe(ev) {
  const t = ev?.type;
  if (t === "hello") {
    return `已连接 · 状态「${STATUS_TEXT[ev.status] || ev.status || "?"}」 · 当前阶段 ${stageLabel(ev.stage) || "无"}`;
  }
  if (t === "stage") return `开始推进「${stageLabel(ev.stage)}」`;
  if (t === "tool") return `${ev.name || "未知工具"} · ${String(ev.args_summary || "").slice(0, 110)}`;
  if (t === "tool_result") return `${ev.name || "工具"} 回执 · ${String(ev.preview || "").slice(0, 130)}`;
  if (t === "stage_gate") return `第 ${ev?.round ?? "?"} 轮质量门：${ev?.ok ? "通过" : "未过"}`;
  if (t === "stage_done") return `阶段结束：${ev?.ok ? "产物已落库" : "未通过"}`;
  if (t === "gate") return `「${stageLabel(ev.stage)}」已过质量门，等待人工确认`;
  if (t === "retry") return `重试「${stageLabel(ev.stage)}」`;
  if (t === "failed") {
    return `运行失败${ev?.stage ? ` · 阶段「${stageLabel(ev.stage)}」` : ""}${ev?.error ? ` · ${String(ev.error).slice(0, 140)}` : ""}`;
  }
  if (t === "done") return "运行完成";
  return JSON.stringify(ev)?.slice(0, 160) || "";
}

function lineClass(ev) {
  if (ev?.type === "gate" || ev?.type === "done") return "hi";
  if (ev?.type === "failed") return "bad";
  if (ev?.type === "hello" || ev?.type === "tool_result") return "dim";
  return "";
}

async function begin() {
  if (!canStart.value) return;
  if (!studio.canvas) {
    entryError.value = "缺少项目标识，请从项目列表进入本页";
    return;
  }
  starting.value = true;
  entryError.value = "";
  try {
    await startRun(studio.canvas, novel.value.trim());
  } catch (err) {
    entryError.value = errorText(err);
  } finally {
    starting.value = false;
  }
}

async function confirmApprove() {
  if (approving.value) return;
  approving.value = true;
  gateError.value = "";
  try {
    await approveRun();
    ElMessage.success("已确认，继续写剧本");
  } catch (err) {
    gateError.value = errorText(err);
  } finally {
    approving.value = false;
  }
}

async function rewriteWithFeedback() {
  const text = feedback.value.trim();
  if (!text || sendingFeedback.value) return;
  sendingFeedback.value = true;
  gateError.value = "";
  try {
    await sendRunFeedback(text);
    feedback.value = "";
    ElMessage.success("意见已记录；确认门不会自动放行，确认仍需你点击");
  } catch (err) {
    gateError.value = errorText(err);
  } finally {
    sendingFeedback.value = false;
  }
}

async function retryFailedStage() {
  if (!run.value.stage) return;
  retryError.value = "";
  try {
    await retryRun(run.value.stage);
  } catch (err) {
    retryError.value = errorText(err);
  }
}

function scrollToOutline() {
  outlineEl.value?.scrollIntoView({ behavior: "smooth", block: "start" });
}
</script>

<template>
  <section class="crp">
    <!-- ① 入口：本项目没有可显示的 run（无 run，或活动 run 属于别的项目） -->
    <div v-if="!runHere" class="card">
      <div class="cap">从小说开始<span class="sub">粘贴正文 → Agent 逐阶段改编 → 大纲停下来等你确认</span></div>
      <textarea
        v-model="novel"
        class="novel"
        rows="7"
        placeholder="粘贴小说正文（全文，至少 50 字）……"
        :disabled="starting"
      />
      <div class="bar">
        <span class="cnt num" :class="{ ready: charCount >= 50 }">{{ charCount }} / 50 字</span>
        <span v-if="entryError" class="err">{{ entryError }}</span>
        <button class="btn primary go" type="button" :disabled="!canStart" @click="begin">
          {{ starting ? "正在创建运行…" : "开始创作" }}
        </button>
      </div>
      <p class="hint">开始后先由 Agent 产出大纲并过质量门，然后停下来等你确认，确认后才会继续写剧本（需 Postgres 后端）。</p>
    </div>

    <!-- ② 运行卡：有活动 run -->
    <div v-else class="card">
      <header class="run-head">
        <span class="dot" :class="run.status" />
        <b class="run-cap">{{ STATUS_TEXT[run.status] || run.status || "…" }}</b>
        <span v-if="run.stage" class="stage-chip">{{ stageLabel(run.stage) }}</span>
        <span v-if="run.link === 'reconnecting'" class="warn-line">直播连接断开，自动重连中…</span>
        <span v-if="run.link === 'lost'" class="err">直播连接已断开（服务可能已重启），请刷新页面查看最新进度</span>
        <span class="rid num">run {{ run.id.slice(0, 8) }}</span>
      </header>

      <!-- 确认门（红线）：gate 之后停在这里，放行只有一个入口 -->
      <div v-if="run.status === 'gated'" class="gate">
        <div class="gate-cap">「{{ stageLabel(run.stage) }}」已通过质量门，等你确认</div>
        <p class="tip">确认后才会继续写剧本；确认门不可跳过，也不会自动放行。对大纲有意见可以先留言再确认。</p>
        <div v-if="gateError" class="err">{{ gateError }}</div>
        <div class="acts">
          <button class="btn" type="button" @click="scrollToOutline">查看大纲并确认</button>
          <button class="btn primary" type="button" :disabled="approving" @click="confirmApprove">
            {{ approving ? "正在放行…" : "确认，开写剧本" }}
          </button>
        </div>
        <div class="fb">
          <input
            v-model="feedback"
            class="fb-in"
            type="text"
            placeholder="对大纲的意见，如：第一集节奏太慢、主角出场再提前……"
            :disabled="sendingFeedback"
            @keyup.enter="rewriteWithFeedback"
          />
          <button class="btn" type="button" :disabled="!feedback.trim() || sendingFeedback" @click="rewriteWithFeedback">
            {{ sendingFeedback ? "记录中…" : "带意见重写" }}
          </button>
        </div>
      </div>

      <!-- 失败：findings + 重试本阶段 -->
      <div v-else-if="run.status === 'failed'" class="failbox">
        <div class="fail-cap">阶段「{{ stageLabel(run.stage) || "未知" }}」未通过</div>
        <div v-if="failEvent?.findings?.length" class="findings">
          <div v-for="(f, i) in failEvent.findings" :key="i" class="finding">
            <span v-if="f?.gate" class="gate-tag">{{ f.gate }}</span>{{ f?.detail || f?.message || "" }}
          </div>
        </div>
        <div v-else class="tip">没有返回具体原因，可查看下方事件流。</div>
        <div v-if="retryError" class="err">{{ retryError }}</div>
        <div class="acts">
          <button class="btn primary" type="button" :disabled="!run.stage" @click="retryFailedStage">重试本阶段</button>
        </div>
      </div>

      <!-- 完成 -->
      <div v-else-if="run.status === 'done'" class="donebox">本轮运行完成，产物已导入，可在下方各页签查看。</div>

      <!-- 事件流（倒序：最新在上） -->
      <div class="log">
        <div v-if="!reversedEvents.length" class="log-none">正在连接执行器，等待第一帧…</div>
        <div
          v-for="(ev, i) in reversedEvents"
          :key="run.events.length - 1 - i"
          class="log-line"
          :class="lineClass(ev)"
        >
          <span class="log-kind">{{ kindLabel(ev) }}</span>
          <span class="log-txt">{{ describe(ev) }}</span>
        </div>
      </div>
    </div>

    <!-- ③ gated：大纲草案（滚动目标 + 确认对象）；同 runHere 判归属 -->
    <div v-if="runHere && run.status === 'gated'" ref="outlineEl" class="outline-wrap">
      <div class="outline-cap">大纲草案 · 质量门已过，待人工确认</div>
      <PipelineOutlinePanel v-if="outlineContent" :outline="outlineContent" />
      <div v-else class="outline-none">大纲产物加载中，请稍候……若长时间为空请刷新页面。</div>
    </div>
  </section>
</template>

<style scoped>
.crp { display: flex; flex-direction: column; gap: var(--sp-4); margin-bottom: var(--sp-5); }
.crp, .crp * { box-sizing: border-box; }
.card { border: 1px solid var(--line); border-radius: var(--r-2); background: var(--card); padding: var(--sp-3) var(--sp-4); }
.cap { font-family: var(--serif); font-size: var(--fs-14); font-weight: 600; margin-bottom: var(--sp-2); }
.cap .sub { font-family: var(--sans); font-size: var(--fs-11); color: var(--text-3); font-weight: 400; margin-left: 12px; }

.novel { width: 100%; min-height: 120px; resize: vertical; padding: 10px 12px; border: 1px solid var(--line-2); border-radius: var(--r-1); background: var(--bg); color: var(--text); font-family: var(--sans); font-size: var(--fs-13); line-height: 1.8; }
.novel:focus { outline: none; border-color: var(--accent); }
.novel:disabled { opacity: .6; }
.bar { display: flex; align-items: center; gap: var(--sp-3); margin-top: var(--sp-2); }
.cnt { font-size: var(--fs-12); color: var(--text-3); }
.cnt.ready { color: var(--ok); }
.err { color: var(--danger); font-size: var(--fs-12); line-height: 1.7; }
.go { margin-left: auto; }
.hint { margin: var(--sp-2) 0 0; font-size: var(--fs-11); color: var(--text-3); line-height: 1.8; }

.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; padding: 7px 16px; border-radius: var(--r-1); font-size: var(--fs-13); color: var(--text-2); cursor: pointer; background: none; border: 1px solid var(--line-2); font-family: inherit; }
.btn:hover { color: var(--text); background: var(--card-2); }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #FFF7F0; }
.btn.primary:hover { background: var(--accent-hi); }
.btn:disabled { opacity: .5; pointer-events: none; }

.run-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.run-cap { font-family: var(--serif); font-size: var(--fs-14); }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-3); flex: none; }
.dot.running { background: var(--warn); animation: crp-pulse 1.2s ease-in-out infinite; }
.dot.gated { background: var(--gold); }
.dot.failed { background: var(--danger); }
.dot.done { background: var(--ok); }
@keyframes crp-pulse { 50% { opacity: .35; } }
.stage-chip { background: var(--card-2); border-radius: 99px; padding: 1px 10px; font-size: var(--fs-11); color: var(--text-2); }
.warn-line { color: var(--warn); font-size: var(--fs-12); }
.rid { margin-left: auto; font-size: var(--fs-11); color: var(--text-3); }

/* 确认门卡：鎏金高亮——整页最醒目的一块，停在这里等人工 */
.gate { margin-top: var(--sp-3); border: 1px solid color-mix(in srgb, var(--gold) 45%, transparent); background: var(--warn-soft); border-radius: var(--r-2); padding: var(--sp-3) var(--sp-4); }
.gate-cap { font-family: var(--serif); font-size: var(--fs-14); font-weight: 600; }
.tip { margin: 6px 0 0; font-size: var(--fs-12); color: var(--text-2); line-height: 1.8; }
.acts { display: flex; gap: var(--sp-2); margin-top: var(--sp-3); flex-wrap: wrap; }
.fb { display: flex; gap: var(--sp-2); margin-top: var(--sp-2); }
.fb-in { flex: 1; min-width: 0; padding: 7px 10px; border: 1px solid var(--line-2); border-radius: var(--r-1); background: var(--bg); color: var(--text); font-family: inherit; font-size: var(--fs-12); }
.fb-in:focus { outline: none; border-color: var(--gold); }

.failbox { margin-top: var(--sp-3); border: 1px solid color-mix(in srgb, var(--danger) 40%, transparent); border-radius: var(--r-2); padding: var(--sp-3) var(--sp-4); }
.fail-cap { color: var(--danger); font-family: var(--serif); font-size: var(--fs-14); font-weight: 600; }
.findings { margin-top: var(--sp-2); display: flex; flex-direction: column; gap: 4px; }
.finding { font-size: var(--fs-12); color: var(--text-2); line-height: 1.7; word-break: break-all; }
.gate-tag { display: inline-block; margin-right: 8px; padding: 0 8px; background: var(--card-2); border-radius: 99px; color: var(--warn); font-size: var(--fs-11); }

.donebox { margin-top: var(--sp-3); font-size: var(--fs-13); color: var(--ok); }

.log { margin-top: var(--sp-3); border-top: 1px solid var(--line); padding-top: var(--sp-2); max-height: 320px; overflow-y: auto; }
.log-none { font-size: var(--fs-12); color: var(--text-3); padding: var(--sp-2) 0; }
.log-line { display: flex; gap: 8px; padding: 3px 0; font-size: var(--fs-12); color: var(--text-2); line-height: 1.7; }
.log-line.hi { color: var(--gold); }
.log-line.bad { color: var(--danger); }
.log-line.dim { color: var(--text-3); }
.log-kind { flex: none; width: 52px; color: var(--text-3); font-size: var(--fs-11); padding-top: 1px; }
.log-line.hi .log-kind { color: var(--gold); }
.log-line.bad .log-kind { color: var(--danger); }
.log-txt { min-width: 0; word-break: break-all; }

.outline-wrap { border: 1px solid color-mix(in srgb, var(--gold) 45%, transparent); border-radius: var(--r-2); background: var(--card); padding: var(--sp-3) var(--sp-4); scroll-margin-top: 60px; }
.outline-cap { font-family: var(--serif); font-size: var(--fs-13); color: var(--gold); margin-bottom: var(--sp-2); }
.outline-none { font-size: var(--fs-12); color: var(--text-3); padding: var(--sp-3) 0; text-align: center; }
</style>
