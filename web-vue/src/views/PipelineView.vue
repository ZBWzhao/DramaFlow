<script setup>
/* 制片台 · 管线产物（只读 + 运行状态）：角色 / 美术 / 站位 / 分镜四类产物视图。
 *
 * 数据全部来自 /api/pipeline 导入的产物详情（studioStore.loadPipeline 缓存在
 * studio.pipeline.detail），不是画布节点。画布老项目或 file 后端没有产物 →
 * 列表为空 → 四类全是空态，页面本身不报错。上游产物是「尽力而为」的 JSON：
 * 各面板内部对所有字段防御取值（缺键渲染空）。
 * 注意：script 详情顶层另有响应视图 episodes[]，本页不消费 script，四类面板
 * 一律只从 detail.content 渲染。
 *
 * 面板状态化（阶段 2 Task 6）：本项目有活动 run 时，容器从 run.stages 派生当前
 * 阶段状态——idle（灰）/running（进度条 + 最近一条事件）/failed（红 + 重试，仅
 * 失败阶段本身可重试）/done（渲染产物本体）；无 run → 维持纯只读现状。归属判定
 * 与 CreateRunPanel 同口径（run.project_id === canvas）：A 项目的运行绝不驱动
 * B 项目页面上的面板状态。
 */
import { computed, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { useRoute } from "vue-router";
import { errorText } from "../api.js";
import { loadPipeline, retryRun, studio } from "../studio/studioStore.js";
import CastPanel from "./pipeline/CastPanel.vue";
import ArtPanel from "./pipeline/ArtPanel.vue";
import StagingPanel from "./pipeline/StagingPanel.vue";
import StoryboardPanel from "./pipeline/StoryboardPanel.vue";
import CreateRunPanel from "./pipeline/CreateRunPanel.vue";

const route = useRoute();

/* 四类产物一个子 tab 一个面板；type = 落库 artifact_type（详情缓存的键） */
const tabs = [
  { key: "cast", label: "角色设定", type: "characters", comp: CastPanel },
  { key: "art", label: "美术设定", type: "art", comp: ArtPanel },
  { key: "staging", label: "开场站位", type: "staging", comp: StagingPanel },
  { key: "storyboard", label: "分镜", type: "storyboard", comp: StoryboardPanel },
];
const tab = ref("cast");
const cur = computed(() => tabs.find((t) => t.key === tab.value) || tabs[0]);

/* 详情在手才透传 content；缺详情（未导入 / 读取失败）→ 空态 */
const content = computed(() => {
  const c = studio.pipeline?.detail?.[cur.value.type]?.content;
  return c && typeof c === "object" ? c : null;
});

/* 元信息一行：schema_version · 导入时间 · 来源批次；都缺就不显示 */
const meta = computed(() => {
  const d = studio.pipeline?.detail?.[cur.value.type];
  if (!d) return "";
  const at = Number(d.updated_at);
  const when = Number.isFinite(at) && at > 0 ? new Date(at * 1000).toLocaleString() : "";
  return [d.schema_version && `v${d.schema_version}`, when, d.source_run_id && `批次 ${d.source_run_id}`]
    .filter(Boolean)
    .join(" · ");
});

const imported = computed(() => Boolean(studio.pipeline?.artifacts?.[cur.value.type]));

/* 创作入口的占位开关（阶段 2 Task 4）：项目还没有任何管线产物 → 常驻入口；
 * 已有产物 → 只在存在活动 run 时才显示运行卡，平时不占位。
 * 归属判定带 project_id：别的项目还有 run 在跑，也不在本页渲染它的运行卡
 * （review 修复：防 A 项目的确认门渲染到 B 项目、对着 B 的大纲缓存点确认）。 */
const hasArtifacts = computed(() => Object.keys(studio.pipeline?.artifacts || {}).length > 0);
const run = computed(() => studio.run);
const runActiveHere = computed(
  () => Boolean(studio.run.id) && studio.run.project_id === studio.canvas,
);

/* 当前 tab 阶段的运行状态（Task 6）：无活动 run → ""（保持只读现状）。
 * running/failed 取自 run.stages 的逐阶段账本；run 已 done 而阶段事件帧缺失
 * （晚连上/断线重连）时整体回落 done；其余一律 idle（尚未推进到）。 */
const stageState = computed(() => {
  if (!runActiveHere.value) return "";
  if (run.value.status === "done") return "done";
  const status = run.value.stages?.[cur.value.type]?.status;
  return status === "running" || status === "failed" || status === "done" ? status : "idle";
});

const STAGE_LABELS = {
  outline: "大纲", script: "剧本", characters: "角色设定",
  art: "美术设定", staging: "开场站位", storyboard: "分镜",
};

/* 本阶段最近一条事件（running 态的进度说明）。执行器事件是尽力而为的 JSON，
 * 防御取值，渲染不出就回落到事件类型本身。 */
const latestText = computed(() => {
  const evs = run.value.events || [];
  let ev = null;
  for (let i = evs.length - 1; i >= 0; i -= 1) {
    if (evs[i]?.stage === cur.value.type && evs[i]?.type !== "hello") { ev = evs[i]; break; }
  }
  if (!ev) return "";
  if (ev.type === "tool") return `${ev.name || "工具"} · ${String(ev.args_summary || "").slice(0, 90)}`;
  if (ev.type === "tool_result") return `${ev.name || "工具"} 回执 · ${String(ev.preview || "").slice(0, 90)}`;
  if (ev.type === "stage_gate") return `第 ${ev.round ?? "?"} 轮质量门：${ev.ok ? "通过" : "未过"}`;
  if (ev.type === "stage_done") return ev.ok ? "产物已落库" : "未通过";
  if (ev.type === "stage") return "开始推进";
  if (ev.type === "retry") return "重试中";
  return String(ev.type || "");
});

/* failed 态的重试：服务端只接受重试失败阶段本身（run.stage），其余阶段 409——
 * 按钮只在阶段匹配时出现，与 CreateRunPanel 失败卡的重试同一条 retryRun 路径。 */
const retrying = ref(false);
const retryError = ref("");
const canRetry = computed(() => run.value.status === "failed" && run.value.stage === cur.value.type);

async function retryStage() {
  if (retrying.value || !canRetry.value) return;
  retrying.value = true;
  retryError.value = "";
  try {
    await retryRun(run.value.stage);
    ElMessage.success(`已重试「${STAGE_LABELS[cur.value.type] || cur.value.type}」`);
  } catch (err) {
    retryError.value = errorText(err);
  } finally {
    retrying.value = false;
  }
}

onMounted(async () => {
  const name = String(route.params.name || "");
  if (name && studio.canvas !== name) studio.canvas = name;
  await loadPipeline();
});
</script>

<template>
  <div class="pv">
    <div class="pv-in">
      <div class="pv-head">
        管线产物
        <span class="sub">来自管线导入，只读 · 修订走重导入</span>
      </div>

      <!-- 创作入口：无产物常驻（从小说开始），有产物只跟随本项目的活动 run -->
      <CreateRunPanel v-if="!hasArtifacts || runActiveHere" />

      <div class="pv-tabs">
        <button
          v-for="t in tabs"
          :key="t.key"
          class="pt"
          :class="{ on: tab === t.key, has: Boolean(studio.pipeline?.artifacts?.[t.type]) }"
          type="button"
          @click="tab = t.key"
        >
          {{ t.label }}
        </button>
      </div>

      <div v-if="meta" class="pv-meta num">{{ meta }}</div>

      <!-- 运行状态条：仅本项目有活动 run 时出现（Task 6 面板状态化）；done 不加条 -->
      <div v-if="stageState === 'running'" class="pv-run running">
        <span class="pbar" aria-hidden="true" />
        <span class="txt">「{{ cur.label }}」推进中 · {{ latestText || "等待执行器第一帧…" }}</span>
      </div>
      <div v-else-if="stageState === 'failed'" class="pv-run failed">
        <span class="txt">「{{ cur.label }}」未通过质量门（已落库产物保持上一版）</span>
        <button v-if="canRetry" class="btn retry" type="button" :disabled="retrying" @click="retryStage">
          {{ retrying ? "重试中…" : "重试本阶段" }}
        </button>
        <span v-if="retryError" class="err">{{ retryError }}</span>
      </div>
      <div v-else-if="stageState === 'idle'" class="pv-run idle">
        <span class="txt">{{ content ? "本轮运行尚未推进到本阶段；以下为已落库内容" : `本轮运行尚未推进到「${cur.label}」` }}</span>
      </div>

      <!-- 面板：有 content 就渲染（running 时那是上一版，状态条会说明）；没有 → 状态化空态 -->
      <component :is="cur.comp" v-if="content" :content="content" :state="stageState" />
      <div v-else-if="stageState === 'done'" class="pv-none">
        「{{ cur.label }}」产物已落库，详情加载中——请稍候，长时间为空请刷新页面。
      </div>
      <div v-else-if="!stageState" class="pv-none">
        该项目无此管线产物
        <span v-if="!imported" class="tip">——在管线导入流程写入 characters / art / staging / storyboard 产物后这里会显示（需 Postgres 后端）</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pv { overflow-y: auto; flex: 1; height: 100%; }
.pv, .pv * { box-sizing: border-box; }
.pv-in { max-width: 980px; margin: 0 auto; padding: var(--sp-6) var(--sp-6) 96px; }
.pv-head { font-family: var(--serif); font-size: var(--fs-20); font-weight: 600; padding-bottom: var(--sp-4); }
.pv-head .sub { font-family: var(--sans); font-size: var(--fs-12); color: var(--text-3); font-weight: 400; margin-left: 12px; }

.pv-tabs { display: flex; gap: var(--sp-1); border-bottom: 1px solid var(--line-2); }
.pt {
  position: relative; padding: 8px 16px; background: none; border: none; cursor: pointer;
  color: var(--text-3); font-size: var(--fs-13); font-family: inherit;
}
.pt:hover { color: var(--text-2); }
.pt.on { color: var(--text); }
.pt.on::after { content: ""; position: absolute; left: 12px; right: 12px; bottom: -1px; height: 2px; background: var(--accent); }
/* 有产物的 tab 加一个可读的小点：没导入的类型一眼可辨，不必点进去看空态 */
.pt.has::before { content: ""; display: inline-block; width: 5px; height: 5px; border-radius: 50%; background: var(--accent); margin-right: 7px; vertical-align: 2px; }

.pv-meta { margin: var(--sp-2) 0 0; color: var(--text-3); font-size: var(--fs-11); }

/* 运行状态条（Task 6）：idle 灰 / running 进度条 / failed 红 + 重试 */
.pv-run {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  margin: var(--sp-2) 0 0; padding: var(--sp-2) var(--sp-3); border-radius: var(--r-2);
  font-size: var(--fs-12); color: var(--text-2);
}
.pv-run .txt { min-width: 0; word-break: break-all; }
.pv-run.idle { border: 1px dashed var(--line-2); color: var(--text-3); }
.pv-run.running { border: 1px solid color-mix(in srgb, var(--warn) 45%, transparent); background: var(--warn-soft); }
.pv-run.failed { border: 1px solid color-mix(in srgb, var(--danger) 40%, transparent); background: color-mix(in srgb, var(--danger) 8%, transparent); }
.pv-run.failed .txt { color: var(--danger); }
.pv-run .err { color: var(--danger); }
.pbar {
  flex: none; width: 90px; height: 4px; border-radius: 99px;
  background: linear-gradient(90deg, transparent, var(--warn), transparent);
  background-size: 50% 100%; background-repeat: no-repeat;
  animation: pv-slide 1.2s linear infinite;
}
@keyframes pv-slide { from { background-position: -60% 0; } to { background-position: 160% 0; } }
.btn.retry { padding: 3px 12px; font-size: var(--fs-12); color: var(--danger); border-color: color-mix(in srgb, var(--danger) 40%, transparent); }
.btn.retry:hover { background: color-mix(in srgb, var(--danger) 10%, transparent); color: var(--danger); }
.btn.retry:disabled { opacity: .5; pointer-events: none; }

.pv-none {
  margin-top: var(--sp-6); padding: var(--sp-6) 0; text-align: center;
  color: var(--text-3); font-size: var(--fs-13);
  border: 1px dashed var(--line-2); border-radius: var(--r-2);
}
.pv-none .tip { display: block; margin-top: 8px; font-size: var(--fs-11); color: var(--text-3); opacity: .75; }
</style>
