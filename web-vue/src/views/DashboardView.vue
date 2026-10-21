<script setup>
/* 数据看板（demo v4 屏5）：集进度 / 资产就绪 / 制作断点 / 成片。
 * 全部数字取自 studioStore 已有派生数据（后端算规则，前端不造规则）。
 * 「最近动态」后端没有事件流端点，以「断点提示 + 下一步」代替（记入台账的偏离）。
 */
import { computed, onMounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { loadAll, studio } from "../studio/studioStore.js";
import { assetUrl as mediaUrl } from "../utils.js";

const route = useRoute();
const router = useRouter();

const segments = computed(() => studio.segments);
const done = computed(() => segments.value.filter((s) => s.status === "done").length);
const pct = computed(() => (segments.value.length ? Math.round((done.value / segments.value.length) * 100) : 0));

const assets = computed(() => studio.assets || {});
const assetsTotal = computed(
  () => ["character", "scene", "prop"].reduce((n, k) => n + (assets.value[k] || []).length, 0),
);
const assetsReady = computed(
  () => ["character", "scene", "prop"].reduce((n, k) => n + (assets.value[k] || []).filter((a) => a.look_ready).length, 0),
);

/* 断点：没有出片的段数（提示按顺序：先首帧质检，再批量出片） */
const missing = computed(() => segments.value.filter((s) => !s.has_video));
const staleCount = computed(() => segments.value.filter((s) => s.keyframe_stale || s.video_stale).length);
const renders = computed(() => studio.preview?.rendered || []);

onMounted(async () => {
  const name = String(route.params.name || "");
  if (name && studio.canvas !== name) studio.canvas = name;
  await loadAll();
});
</script>

<template>
  <div class="dash">
    <div class="dash-in">
      <div class="dash-head">
        数据看板
        <span class="sub">{{ studio.project?.title }} · 第 {{ studio.project?.episode || 1 }} 集</span>
      </div>

      <div class="stats">
        <div class="stat">
          <div class="k">集进度</div>
          <div class="v num">{{ done }} <small>/ {{ segments.length }} 段出片</small></div>
          <div class="bar"><i :style="{ width: pct + '%' }"></i></div>
          <div class="d">一镜一段 3-8s</div>
        </div>
        <div class="stat">
          <div class="k">资产就绪</div>
          <div class="v num">{{ assetsReady }} <small>/ {{ assetsTotal }} 有图</small></div>
          <div class="d">角色 {{ (assets.character || []).length }} · 场景 {{ (assets.scene || []).length }} · 道具 {{ (assets.prop || []).length }}</div>
        </div>
        <div class="stat" :class="{ alert: missing.length }">
          <div class="k">制作断点</div>
          <div class="v num">{{ missing.length }}</div>
          <div class="d">{{ staleCount ? `${staleCount} 段素材过期 · ` : "" }}段未出片</div>
        </div>
        <div class="stat">
          <div class="k">成片</div>
          <div class="v num">{{ renders.length }}</div>
          <div class="d">{{ renders[0]?.duration ? `最近 ${Math.round(renders[0].duration)}s` : "还没有合成" }}</div>
        </div>
      </div>

      <div v-if="missing.length" class="bp">
        <span class="dot"></span>
        <span class="msg">{{ staleCount ? `${staleCount} 段素材已过期（分镜/形象变更）——` : "" }}{{ missing.length }} 段未出片——先过首帧墙质检，再批量出片</span>
        <button class="btn sm" type="button" @click="router.push(`/project/${route.params.name}/wall`)">去补首帧</button>
        <button class="btn primary sm" type="button" @click="router.push(`/project/${route.params.name}/production`)">去制作</button>
      </div>

      <div class="two-col">
        <div class="panel">
          <h2>下一步</h2>
          <div class="act">
            <span class="time num">1</span>
            <span class="what"><b>还没有剧本？</b>去剧本页粘贴全文，自动分集后一键交给 Agent</span>
            <button class="btn sm" type="button" @click="router.push(`/project/${route.params.name}/script`)">去剧本</button>
          </div>
          <div class="act">
            <span class="time num">2</span>
            <span class="what"><b>分镜没拆？</b>剧本页「开始制作」——不生成画面，不花钱</span>
          </div>
          <div class="act">
            <span class="time num">3</span>
            <span class="what"><b>出片后</b>在预览页拼接整集、烧录对白与字幕</span>
            <button class="btn sm" type="button" @click="router.push(`/project/${route.params.name}/preview`)">去预览</button>
          </div>
        </div>

        <div class="panel">
          <h2>成片</h2>
          <div v-if="!renders.length" class="empty">还没有合成——去预览页拼接整集</div>
          <div v-for="r in renders" :key="r.path" class="render-row">
            <span class="th"><img v-if="r.thumbnail || r.path" :src="mediaUrl(r.thumbnail || r.path)" alt="" loading="lazy" /></span>
            <span class="what">
              <b>整集合成</b><br />
              <span class="num dim">{{ r.width }}×{{ r.height }} · {{ Math.round(r.duration || 0) }}s<template v-if="r.bytes"> · {{ (r.bytes / 1048576).toFixed(1) }} MB</template></span>
            </span>
            <a class="btn sm" :href="mediaUrl(r.path)" download>下载</a>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dash { overflow-y: auto; flex: 1; height: 100%; }
.dash, .dash * { box-sizing: border-box; }
.dash-in { max-width: 1080px; margin: 0 auto; padding: var(--sp-6) var(--sp-6) 96px; }
.dash-head { font-family: var(--serif); font-size: var(--fs-20); font-weight: 600; padding-bottom: var(--sp-4); }
.dash-head .sub { font-family: var(--sans); font-size: var(--fs-12); color: var(--text-3); font-weight: 400; margin-left: 12px; }

.stats { display: grid; grid-template-columns: repeat(4, 1fr); border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); margin-bottom: var(--sp-5); }
.stat { padding: var(--sp-5) var(--sp-5) var(--sp-4); }
.stat + .stat { border-left: 1px solid var(--line); }
.stat .k { font-size: var(--fs-11); letter-spacing: .14em; color: var(--text-3); margin-bottom: 10px; }
.stat .v { font-family: var(--serif); font-size: var(--fs-20); font-weight: 600; }
.stat .v small { font-size: var(--fs-12); color: var(--text-3); font-weight: 400; font-family: var(--sans); }
.stat .bar { margin-top: 10px; height: 2px; background: var(--card-3); }
.stat .bar i { display: block; height: 100%; background: var(--ok); }
.stat .d { margin-top: 8px; color: var(--text-3); font-size: var(--fs-11); }
.stat.alert .v { color: var(--warn); }

.bp { display: flex; align-items: center; gap: var(--sp-3); padding: var(--sp-3) 0 var(--sp-5); font-size: var(--fs-13); }
.bp .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--warn); flex: none; }
.bp .msg { color: var(--warn); flex: 1; }

.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-6); }
.panel h2 { font-family: var(--serif); font-size: var(--fs-14); font-weight: 600; padding-bottom: var(--sp-2); border-bottom: 1px solid var(--line); margin-bottom: var(--sp-2); }
.act { display: flex; align-items: center; gap: var(--sp-3); padding: 10px 0; border-bottom: 1px solid var(--line); font-size: var(--fs-12); }
.act .time { color: var(--text-3); font-family: var(--mono); font-size: var(--fs-11); width: 20px; flex: none; }
.act .what { flex: 1; color: var(--text-2); line-height: 1.7; }
.act .what b { color: var(--text); font-weight: 500; }
.render-row { display: flex; align-items: center; gap: var(--sp-3); padding: 10px 0; border-bottom: 1px solid var(--line); }
.render-row .th { width: 46px; aspect-ratio: 9/16; border-radius: var(--r-1); background: linear-gradient(165deg, #23201B, #14110E); flex: none; overflow: hidden; }
.render-row .th img { width: 100%; height: 100%; object-fit: cover; }
.render-row .what { flex: 1; font-size: var(--fs-12); color: var(--text-2); }
.render-row .what b { color: var(--text); font-weight: 500; }
.render-row .dim { color: var(--text-3); }
.empty { padding: var(--sp-4) 0; color: var(--text-3); font-size: var(--fs-12); }

.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; padding: 9px 20px; border-radius: var(--r-1); font-size: var(--fs-14); color: var(--text-2); cursor: pointer; background: none; border: none; text-decoration: none; }
.btn:hover { color: var(--text); background: var(--card-2); }
.btn.primary { background: var(--accent); color: #FFF7F0; }
.btn.primary:hover { background: var(--accent-hi); }
.btn.sm { padding: 6px 14px; font-size: var(--fs-13); }
</style>
