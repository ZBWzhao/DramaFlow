<script setup>
/* 预览（demo v4 屏6）：交付的最后一公里。
 * 流程沿用旧 PreviewView：prepareComposition（拼审批计划，人工确认）→
 * runComposition（合成 job）→ pollMediaJob → 渲染历史。
 */
import { computed, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { loadAll, pollMediaJob, prepareComposition, runComposition, studio, toast } from "../studio/studioStore.js";

const route = useRoute();
const plan = ref(null);
const composing = ref(false);

const clips = computed(() => studio.preview?.clips || []);
const rendered = computed(() => studio.preview?.rendered || []);
const playable = computed(() => clips.value.filter((c) => c.video_path));
const latest = computed(() => rendered.value[0] || null);

function mediaUrl(p) {
  if (!p) return "";
  const clean = String(p).replace(/^\/+/, "");
  return /^(https?:|data:|blob:)/.test(clean) ? clean : clean.startsWith("assets/") ? "/" + clean : "/assets/" + clean;
}

async function startCompose() {
  composing.value = true;
  try {
    const { assembled, approval } = await prepareComposition();
    const flat = (approval.plans || []).flatMap((job) => (job.clips || []).map((clip) => ({ ...clip, label: clip.label || job.label })));
    const dlg = (approval.plans || []).flatMap((job) => job.dialogue || []);
    const duration = flat.reduce((sum, clip) => sum + Number(clip.asset?.duration || 0), 0);
    const project = studio.project?.project || {};
    plan.value = {
      approval_id: approval.approval_id,
      revision: assembled.revision,
      clips: flat,
      total_duration: Math.round(duration * 10) / 10,
      spec: `${assembled.clip_count} 段 · ${assembled.duration_s}s · ${project.aspect_ratio || "9:16"} · ${project.quality || "480P"}${dlg.length ? ` · 对白 ${dlg.length} 句` : ""}`,
    };
  } catch (err) {
    toast(err?.message || "准备拼接失败", 4200);
  } finally {
    composing.value = false;
  }
}

async function confirmCompose() {
  if (!plan.value) return;
  composing.value = true;
  try {
    const res = await runComposition(plan.value.approval_id, plan.value.revision);
    plan.value = null;
    if (!res?.job_id) {
      toast("合成任务没有返回 job_id", 4000);
      return;
    }
    toast("开始合成，稍等…", 3000);
    const job = await pollMediaJob(res.job_id);
    const results = job?.result?.results || [];
    if (job?.status === "succeeded" && results.length) {
      const first = results[0];
      toast(`成片已生成：${first.width}×${first.height} · ${Math.round(first.duration)}s`, 4200);
    } else {
      toast(job?.error || `合成未完成：${job?.status}`, 4200);
    }
  } catch (err) {
    toast(err?.message || "合成失败", 4200);
  } finally {
    composing.value = false;
  }
}

onMounted(async () => {
  if (!studio.canvas) studio.canvas = String(route.params.name);
  await loadAll();
});
</script>

<template>
  <div class="dash">
    <div class="dash-in">
      <div class="dash-head">
        预览
        <span class="sub">交付的最后一公里</span>
      </div>

      <div class="pv-hero">
        <div class="pv-poster">
          <template v-if="latest">
            <video v-if="playable.length || latest" :key="latest.path" :src="mediaUrl(latest.path)" controls playsinline class="pv-video"></video>
          </template>
          <template v-else>
            <span class="play">▶</span>
            <span class="cc">{{ playable[0]?.dialogue || "还没有成片" }}</span>
          </template>
        </div>

        <div class="pv-meta">
          <h2>第一集 · 整集</h2>
          <div class="spec num">
            <template v-if="latest">{{ latest.width }}×{{ latest.height }} · {{ Math.round(latest.duration || 0) }}s</template>
            <template v-else>{{ clips.length }} 段素材 · 待合成</template>
          </div>
          <div class="row">
            <span class="n">对白</span>
            <span>台词与时长在生成前已硬校验；合成时烧录字幕与对白音轨</span>
          </div>
          <div class="row">
            <span class="n">审批</span>
            <span>合成前会生成片段顺序与输出规格的计划，<b>由你确认后才开始</b></span>
          </div>
          <div class="row">
            <span class="n">合成</span>
            <span>{{ rendered.length ? `已合成 ${rendered.length} 次` : "还没有合成记录" }}</span>
          </div>
          <div class="pv-acts">
            <a v-if="latest" class="btn primary" :href="mediaUrl(latest.path)" download>下载成片</a>
            <button class="btn" type="button" :disabled="composing || !clips.length" @click="startCompose">
              {{ composing ? "准备中…" : latest ? "重新合成" : "开始合成" }}
            </button>
          </div>
          <div v-if="!latest" class="warn-line">合成前请先在制作页批量出片</div>
        </div>
      </div>

      <div v-if="rendered.length > 1" class="history">
        <div class="cap" style="padding-bottom: 8px">渲染历史</div>
        <div v-for="r in rendered" :key="r.path" class="render-row">
          <span class="th"><img :src="mediaUrl(r.thumbnail || r.path)" alt="" loading="lazy" /></span>
          <span class="what">
            <b>整集合成</b><br />
            <span class="num dim">{{ r.width }}×{{ r.height }} · {{ Math.round(r.duration || 0) }}s</span>
          </span>
          <a class="btn sm" :href="mediaUrl(r.path)" download>下载</a>
        </div>
      </div>
    </div>

    <!-- 审批确认 -->
    <div v-if="plan" class="mask" @click.self="plan = null">
      <div class="box">
        <h3>合成审批 · 确认后开始</h3>
        <div class="spec-line num">{{ plan.spec }} · 总时长 {{ plan.total_duration }}s</div>
        <div class="clip-list">
          <div v-for="(c, i) in plan.clips" :key="i" class="clip-row">
            <span class="no num">{{ String(i + 1).padStart(2, "0") }}</span>
            <span class="lbl">{{ c.label || c.node_id }}</span>
            <span class="dur num">{{ Number(c.asset?.duration || 0) }}s</span>
          </div>
        </div>
        <div class="box-foot">
          <span class="tip">合成会再校验片段是否被切版；有变化会要求重新审批</span>
          <button class="btn sm" type="button" @click="plan = null">取消</button>
          <button class="btn primary sm" type="button" :disabled="composing" @click="confirmCompose">确认合成</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dash { overflow-y: auto; flex: 1; height: 100%; }
.dash, .dash * { box-sizing: border-box; }
.dash-in { max-width: 900px; margin: 0 auto; padding: var(--sp-6) var(--sp-6) 96px; }
.dash-head { font-family: var(--serif); font-size: var(--fs-20); font-weight: 600; padding-bottom: var(--sp-4); }
.dash-head .sub { font-family: var(--sans); font-size: var(--fs-12); color: var(--text-3); font-weight: 400; margin-left: 12px; }

.pv-hero { display: flex; gap: var(--sp-6); }
.pv-poster { width: 220px; aspect-ratio: 9/16; border-radius: var(--r-2); background: linear-gradient(165deg, #26221C, #14110D); position: relative; flex: none; border: 1px solid var(--line-2); overflow: hidden; display: grid; place-items: center; }
.pv-video { width: 100%; height: 100%; object-fit: contain; }
.pv-poster .play { color: var(--text); font-size: 18px; }
.pv-poster .cc { position: absolute; bottom: 14px; left: 14px; right: 14px; text-align: center; font-family: var(--serif); color: #F2EDE4; font-size: var(--fs-13); text-shadow: 0 1px 6px rgba(0, 0, 0, .9); }
.pv-meta { flex: 1; min-width: 0; }
.pv-meta h2 { font-family: var(--serif); font-size: var(--fs-20); font-weight: 600; margin-bottom: 6px; }
.pv-meta .spec { font-family: var(--mono); font-size: var(--fs-12); color: var(--text-3); margin-bottom: var(--sp-4); }
.pv-meta .row { display: flex; align-items: baseline; gap: var(--sp-3); padding: 10px 0; border-top: 1px solid var(--line); font-size: var(--fs-12); color: var(--text-2); }
.pv-meta .row .n { width: 40px; flex: none; color: var(--text-3); font-size: var(--fs-11); letter-spacing: .1em; }
.pv-meta .row b { color: var(--gold); font-weight: 500; }
.pv-acts { display: flex; gap: var(--sp-2); padding-top: var(--sp-4); }
.warn-line { margin-top: var(--sp-3); color: var(--warn); font-size: var(--fs-12); }

.history { margin-top: var(--sp-6); }
.render-row { display: flex; align-items: center; gap: var(--sp-3); padding: 10px 0; border-bottom: 1px solid var(--line); }
.render-row .th { width: 46px; aspect-ratio: 9/16; border-radius: var(--r-1); background: linear-gradient(165deg, #23201B, #14110E); flex: none; overflow: hidden; }
.render-row .th img { width: 100%; height: 100%; object-fit: cover; }
.render-row .what { flex: 1; font-size: var(--fs-12); color: var(--text-2); }
.render-row .what b { color: var(--text); font-weight: 500; }
.render-row .dim { color: var(--text-3); }

.mask { position: fixed; inset: 0; background: rgba(5, 4, 3, .7); z-index: 50; display: grid; place-items: center; }
.box { background: var(--panel); border: 1px solid var(--line-2); border-radius: var(--r-3); width: 640px; max-width: 94vw; max-height: 84vh; overflow-y: auto; padding: var(--sp-5); }
.box h3 { font-family: var(--serif); font-size: var(--fs-16); margin-bottom: var(--sp-2); }
.spec-line { font-family: var(--mono); font-size: var(--fs-12); color: var(--gold); margin-bottom: var(--sp-3); }
.clip-list { max-height: 320px; overflow-y: auto; }
.clip-row { display: flex; align-items: baseline; gap: 10px; padding: 7px 0; border-bottom: 1px solid var(--line); font-size: var(--fs-12); color: var(--text-2); }
.clip-row .no { font-family: var(--mono); color: var(--text-3); width: 24px; }
.clip-row .lbl { flex: 1; }
.clip-row .dur { font-family: var(--mono); color: var(--text-3); }
.box-foot { display: flex; align-items: center; gap: var(--sp-2); margin-top: var(--sp-4); }
.box-foot .tip { margin-right: auto; color: var(--text-3); font-size: var(--fs-11); }

.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; padding: 9px 20px; border-radius: var(--r-1); font-size: var(--fs-14); color: var(--text-2); cursor: pointer; background: none; border: none; text-decoration: none; }
.btn:hover { color: var(--text); background: var(--card-2); }
.btn.primary { background: var(--accent); color: #FFF7F0; }
.btn.primary:hover { background: var(--accent-hi); }
.btn.sm { padding: 6px 14px; font-size: var(--fs-13); }
.btn:disabled { opacity: .5; pointer-events: none; }
</style>
