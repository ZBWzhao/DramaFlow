<script setup>
/* 首帧墙（demo v4 屏3）：整集静帧质检——批量出片前的构图关卡。
 *
 * staleness 语义（服务端算好）：fresh 一致 / stale 过期（分镜或形象变更）/
 * none 缺首帧 / unknown 旧产物无指纹（不算过期，按缺帧处理）。
 * 点击缺帧格 = 补齐首帧；点击过期格 = 解释处理路径；双击 fresh = 去该段生成。
 */
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { generateKeyframe, loadStoryboard, studio, toast } from "../studio/studioStore.js";
import { assetUrl } from "../utils.js";

const route = useRoute();
const router = useRouter();

const cells = ref([]);
const loading = ref(false);
const busyNode = ref("");
const error = ref("");

const episodes = computed(() => studio.script?.episodes || []);
const currentEp = computed(() => Number(studio.project?.episode || episodes.value[0]?.no || 1));
const count = (k) => cells.value.filter((c) => c.staleness === k).length;

async function reload() {
  const name = String(route.params.name || "");
  if (name && studio.canvas !== name) studio.canvas = name;
  loading.value = true;
  error.value = "";
  try {
    const data = await loadStoryboard(currentEp.value);
    cells.value = data.cells || [];
  } catch (err) {
    error.value = err?.message || String(err);
  } finally {
    loading.value = false;
  }
}

async function fill(cell) {
  busyNode.value = cell.node_id;
  try {
    await generateKeyframe(cell.node_id, { shotNo: 1 });
    toast(`片段 ${cell.segment_no} 首帧已生成`);
    await reload();
  } catch (err) {
    toast(err?.message || String(err));
  } finally {
    busyNode.value = "";
  }
}

function onCell(cell) {
  if (cell.staleness === "stale") {
    toast(`片段 ${cell.segment_no} 已过期：${cell.stale_reason || "分镜已变更"} · 重出首帧后再出片，重做由你决定`);
  } else if (cell.staleness === "none" || cell.staleness === "unknown") {
    fill(cell);
  } else {
    toast(`片段 ${cell.segment_no} 首帧与分镜一致`);
  }
}

function open(cell) {
  studio.activeSegmentId = cell.node_id;
  router.push(`/project/${route.params.name}/production`);
}

async function fillAll() {
  const missing = cells.value.filter((c) => c.staleness === "none" || c.staleness === "unknown");
  for (const c of missing) await fill(c);
  if (missing.length) toast(`已补齐 ${missing.length} 张首帧`);
}

onMounted(reload);
</script>

<template>
  <div class="wall-wrap">
    <div class="wall-head2">
      <h2>首帧墙</h2>
      <span class="sub">整集静帧质检——批量出片前先过一遍构图</span>
      <div class="grow"></div>
      <select class="ep-sel" :value="currentEp" @change="studio.project.episode = Number($event.target.value); reload()">
        <option v-for="e in episodes" :key="e.no" :value="e.no">{{ e.title || `第${e.no}集` }}</option>
      </select>
      <button class="btn primary sm" type="button" :disabled="loading || !count('none') && !count('unknown')" @click="fillAll">
        补齐缺的 {{ count("none") + count("unknown") }} 张
      </button>
    </div>

    <div v-if="error" class="err">{{ error }} <button class="btn sm" type="button" @click="reload">重试</button></div>
    <div v-else-if="loading" class="hint-line">正在读取首帧墙…</div>

    <div v-else class="wall">
      <div
        v-for="c in cells"
        :key="c.node_id"
        class="kf"
        :class="{ fresh: c.staleness === 'fresh', stale: c.staleness === 'stale', miss: !c.thumb, busy: busyNode === c.node_id }"
        @click="onCell(c)"
        @dblclick="open(c)"
      >
        <img v-if="c.thumb" class="ph" :src="assetUrl(c.thumb)" :alt="`片段 ${c.segment_no}`" loading="lazy" />
        <span class="no num">{{ String(c.segment_no).padStart(2, "0") }}</span>
        <span v-if="busyNode === c.node_id" class="gen">生成中…</span>
        <div class="dlg">
          <template v-if="c.staleness === 'stale'">过期 · {{ c.stale_reason || "分镜已变更" }} · 点击处理</template>
          <template v-else-if="!c.thumb">缺首帧 · {{ c.title || c.dialogue || "点击补齐" }}</template>
          <template v-else>{{ c.dialogue || c.title || "无对白 · 环境音" }}</template>
        </div>
      </div>
      <div v-if="!cells.length" class="hint-line">这一集还没有片段——回剧本页「开始制作」</div>
    </div>

    <div class="wall-legend">
      <span class="lg ok"><i></i>首帧与分镜一致（{{ count("fresh") }}）</span>
      <span class="lg warn"><i></i>过期——分镜/形象已变更（{{ count("stale") }}）</span>
      <span class="lg mute"><i></i>缺首帧（{{ count("none") + count("unknown") }}）· 点击补齐 · 双击去生成</span>
    </div>
  </div>
</template>

<style scoped>
.wall-wrap { overflow-y: auto; flex: 1; padding: var(--sp-5) var(--sp-6) 96px; height: 100%; }
.wall-wrap, .wall-wrap * { box-sizing: border-box; }
.wall-head2 { display: flex; align-items: baseline; gap: var(--sp-4); max-width: 1200px; margin: 0 auto var(--sp-4); }
.wall-head2 h2 { font-family: var(--serif); font-size: var(--fs-16); font-weight: 600; }
.wall-head2 .sub { font-size: var(--fs-12); color: var(--text-3); }
.grow { flex: 1; }
.ep-sel { font-size: var(--fs-13); color: var(--text-2); padding: 7px 12px; border-radius: var(--r-1); background: var(--card); border: 1px solid var(--line); }
.ep-sel option { background: var(--panel); }

.wall { display: grid; grid-template-columns: repeat(8, 1fr); gap: 10px; max-width: 1200px; margin: 0 auto; }
.kf { aspect-ratio: 9/16; border-radius: var(--r-1); border: 1px solid var(--line); position: relative; overflow: hidden; background: var(--card); cursor: pointer; }
.kf:hover { border-color: var(--line-3); }
.kf.fresh { border-color: rgba(111, 162, 132, .4); }
.kf.stale { border-color: rgba(201, 138, 75, .55); }
.kf.miss { background: repeating-linear-gradient(45deg, var(--card) 0 7px, var(--bg-2) 7px 14px); }
.kf.busy { opacity: .6; }
.kf .ph { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.kf .no { position: absolute; top: 6px; left: 6px; font-family: var(--mono); font-size: 9px; color: var(--text-2); }
.kf .gen { position: absolute; top: 6px; right: 6px; font-size: 9px; color: var(--gold); }
.kf .dlg { position: absolute; left: 0; right: 0; bottom: 0; padding: 16px 8px 6px; background: linear-gradient(transparent, rgba(8, 7, 6, .85)); font-size: 10px; color: var(--text-2); }
.kf.stale .dlg { color: var(--warn); }
.kf.miss .dlg { position: static; padding: 24px 8px 6px; background: none; text-align: center; }

.wall-legend { display: flex; gap: var(--sp-5); font-size: var(--fs-11); max-width: 1200px; margin: var(--sp-4) auto 0; }
.lg { display: inline-flex; align-items: center; gap: 7px; color: var(--text-3); }
.lg i { width: 6px; height: 6px; border-radius: 50%; }
.lg.ok i { background: var(--ok); }
.lg.warn i { background: var(--warn); }
.lg.warn { color: var(--warn); }
.lg.mute i { background: var(--text-3); }
.hint-line { text-align: center; color: var(--text-3); font-size: var(--fs-13); padding: var(--sp-6) 0; }
.err { color: var(--bad); font-size: var(--fs-13); display: flex; align-items: center; gap: var(--sp-3); }

.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; padding: 9px 20px; border-radius: var(--r-1); font-size: var(--fs-14); color: var(--text-2); cursor: pointer; background: none; border: none; }
.btn:hover { color: var(--text); background: var(--card-2); }
.btn.primary { background: var(--accent); color: #FFF7F0; }
.btn.primary:hover { background: var(--accent-hi); }
.btn.sm { padding: 6px 14px; font-size: var(--fs-13); }
.btn:disabled { opacity: .5; pointer-events: none; }
</style>
