<script setup>
/* 任务中心：顶栏「N 任务 ▾」+ 下拉面板。
 *
 * 数据只读 studioStore：studio.jobs（生成/合成等 media/studio job，轮询时写入）
 * 与 studio.produceJob（Agent 制作）。轮询超时的残留任务由 sweepJobs 每 6s
 * 兜底补查一次（只查已在跑的 id，不发起新任务），其余只做聚合展示：运行中在上，已结束置灰在下。
 */
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { cancelJob, studio, sweepJobs, toast } from "../studio/studioStore.js";

const open = ref(false);
const rootEl = ref(null);

const KIND_LABEL = { video: "视频生成", image: "图片生成", keyframe: "首帧生成", audio: "音频生成", batch_generate: "批量出片" };
const ACTIVE = new Set(["queued", "running"]);

const jobs = computed(() =>
  Object.entries(studio.jobs)
    // 批量出片的子任务（sjob_）已归并在批量行里，单独显示只会刷屏
    .filter(([, job]) => job && !job.parent)
    .map(([id, job]) => ({
      id,
      label: KIND_LABEL[job?.kind] || job?.kind || "任务",
      status: job?.status || "unknown",
      active: ACTIVE.has(job?.status),
      progress: typeof job?.progress === "number" ? Math.round(job.progress <= 1 ? job.progress * 100 : job.progress) : null,
      // 批量出片的进度是对象（done/ok/failed/percent），走专用的行内展示
      batch: job?.kind === "batch_generate" && job?.progress && typeof job.progress === "object" ? job.progress : null,
      detail: job?.error || (job?.result?.warnings?.length ? `${job.result.warnings.length} 条告警` : ""),
    }))
    .sort((a, b) => Number(b.active) - Number(a.active)),
);

function stopBatch(id) {
  cancelJob(id).catch((err) => toast(err?.message || String(err)));
}

/* Agent 制作是一等任务：单独成行，带耗时 */
const PRODUCE_STATUS_LABEL = { succeeded: "✓", limited: "⏸ 未完成", failed: "✗ 失败", cancelled: "已取消", timeout: "超时" };
const produce = computed(() => {
  const p = studio.produceJob;
  if (!p) return null;
  return {
    label: `Agent 制作 · 第 ${p.episode ?? "?"} 集`,
    status: p.status,
    statusLabel: PRODUCE_STATUS_LABEL[p.status] || p.status,
    active: ACTIVE.has(p.status),
    elapsed: p.elapsed ? `${p.elapsed}s` : "",
    detail: p.error || "",
    progress: p.progress || null,
  };
});

const activeCount = computed(
  () => jobs.value.filter((j) => j.active).length + (produce.value?.active ? 1 : 0),
);

function onDocClick(e) {
  if (open.value && rootEl.value && !rootEl.value.contains(e.target)) open.value = false;
}
let sweepTimer = 0;
onMounted(() => {
  document.addEventListener("click", onDocClick);
  sweepTimer = window.setInterval(() => { sweepJobs().catch(() => {}); }, 6000);
});
onBeforeUnmount(() => {
  document.removeEventListener("click", onDocClick);
  window.clearInterval(sweepTimer);
});
</script>

<template>
  <div ref="rootEl" class="task-center">
    <button class="runstat" type="button" @click="open = !open">
      <span v-if="activeCount" class="pulse"></span>
      <span class="num">{{ activeCount }} 任务</span>
      <span class="caret">▾</span>
    </button>

    <div v-if="open" class="task-pop">
      <h3>任务中心</h3>

      <div v-if="produce" class="tj" :class="{ done: !produce.active }">
        <div class="r1">
          <span class="nm">{{ produce.label }}</span>
          <span v-if="produce.active" class="pc num">{{ produce.elapsed }}</span>
          <span v-else class="pc">{{ produce.statusLabel }}</span>
        </div>
        <div v-if="produce.progress" class="r1 dim-row">
          <span class="pc num">片段 {{ produce.progress.prompts }}/{{ produce.progress.total }}</span>
          <span v-if="produce.active" class="pc num">{{ produce.progress.percent }}%</span>
        </div>
        <div v-if="produce.active && produce.progress?.total" class="bar">
          <i :style="{ width: `${produce.progress.percent}%` }"></i>
        </div>
        <div v-if="produce.detail" class="d">{{ produce.detail }}</div>
      </div>

      <div v-for="j in jobs" :key="j.id" class="tj" :class="{ done: !j.active }">
        <div class="r1">
          <span class="nm">{{ j.label }} <span class="sub num">{{ j.id.slice(0, 8) }}</span></span>
          <span v-if="j.active" class="pc num">{{ j.progress != null ? `${j.progress}%` : j.status }}</span>
          <span v-else class="pc">{{ j.status === "succeeded" ? "✓" : j.status }}</span>
        </div>
        <template v-if="j.batch">
          <div class="r1 dim-row">
            <span class="pc num">片段 {{ j.batch.ok }}✓ {{ j.batch.failed }}✗ · {{ j.batch.done }}/{{ j.batch.total }}</span>
            <span class="grow"></span>
            <button v-if="j.active" class="cx" type="button" @click="stopBatch(j.id)">取消</button>
          </div>
          <div v-if="j.active" class="bar"><i :style="{ width: `${j.batch.percent}%` }"></i></div>
        </template>
        <div v-else-if="j.progress != null && j.active" class="bar"><i :style="{ width: `${j.progress}%` }"></i></div>
        <div v-if="j.detail" class="d">{{ j.detail }}</div>
      </div>

      <div v-if="!jobs.length && !produce" class="empty">本会话还没有触发过任务</div>
    </div>
  </div>
</template>

<style scoped>
.task-center { position: relative; display: flex; align-items: center; }
.runstat { display: flex; align-items: center; gap: 7px; font-size: var(--fs-13); color: var(--text-3); padding: 5px 3px; }
.runstat:hover { color: var(--text-2); }
.pulse { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); animation: tc-pulse 1.6s ease-in-out infinite; }
@keyframes tc-pulse { 50% { opacity: .3; } }
.caret { font-size: 10px; }

.task-pop {
  position: absolute; top: 34px; right: 0; width: 320px;
  background: var(--panel); border: 1px solid var(--line-2); border-radius: var(--r-2);
  padding: var(--sp-4); z-index: 60; box-shadow: 0 12px 40px rgba(0, 0, 0, .5);
}
.task-pop h3 { font-family: var(--serif); font-size: var(--fs-14); font-weight: 600; padding-bottom: 10px; border-bottom: 1px solid var(--line); }
.tj { padding: 10px 0; border-bottom: 1px solid var(--line); font-size: var(--fs-12); }
.tj:last-of-type { border-bottom: none; }
.tj .r1 { display: flex; align-items: center; gap: 8px; }
.tj .nm { color: var(--text); flex: 1; }
.tj .nm .sub { color: var(--text-3); font-size: var(--fs-11); }
.tj .pc { color: var(--text-3); font-family: var(--mono); font-size: var(--fs-11); }
.tj .d { margin-top: 4px; color: var(--bad); font-size: var(--fs-11); }
.tj .bar { margin-top: 7px; height: 2px; background: var(--card-3); }
.tj .bar i { display: block; height: 100%; background: var(--ok); }
.tj.done .nm { color: var(--text-3); }
.tj .dim-row { margin-top: 4px; }
.tj .bar { margin-top: 7px; height: 2px; background: var(--card-3); }
.tj .bar i { display: block; height: 100%; background: var(--ok); transition: width .6s ease; }
.tj .grow { flex: 1; }
.tj .cx { color: var(--warn); background: none; border: 1px solid var(--line-2); border-radius: var(--r-1); padding: 2px 10px; font-size: var(--fs-11); cursor: pointer; }
.tj .cx:hover { border-color: var(--warn); }
.empty { padding: 14px 0 4px; color: var(--text-3); font-size: var(--fs-12); }
</style>
