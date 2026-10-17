<template>
  <div class="preview-wrap">
    <!-- ───────────────────────────── 左：成片预览 -->
    <div style="display: flex; flex-direction: column; gap: 12px; min-height: 0">
      <div class="panel" style="flex: none">
        <div class="panel-body" style="display: flex; flex-direction: column; gap: 9px">
          <div class="progress-line">
            <span>本集完成度</span>
            <div class="bar"><i :style="{ width: pct + '%' }"></i></div>
            <span><b>{{ done }}</b>/{{ total }} 段（{{ pct }}%）</span>
          </div>
          <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap">
            <button class="btn primary" :disabled="!playable.length" @click="playAll">
              连播这一集（{{ playable.length }} 段）
            </button>
            <button class="btn" :disabled="composing || !clips.length" @click="startCompose">
              {{ composing ? "合成中…" : "合成整集" }}
            </button>
            <button class="btn" :disabled="!studio.preview" @click="exportPrompts">导出提示词</button>
            <span class="grow" style="margin-left: auto"></span>
            <span style="font-size: 11px; color: var(--text-3)">
              {{ studio.project?.project?.aspect_ratio || "9:16" }} ·
              {{ studio.project?.project?.quality || "480P" }}
            </span>
          </div>
        </div>
      </div>

      <div class="panel" style="flex: none; max-width: 300px; margin: 0 auto">
        <div class="player-shell">
          <video
            v-if="current?.video_path"
            ref="playerEl"
            :src="mediaUrl(current.video_path)"
            controls
            @ended="onEnded"
          ></video>
          <div v-else class="empty" style="color: #6b7280">
            选一段有片子的片段<br />或点「连播这一集」
          </div>
        </div>
      </div>
      <div v-if="current" style="text-align: center; font-size: 12px; color: var(--text-2)">
        {{ current.title }}
        <span style="color: var(--text-3)">
          · {{ current.duration_s }}s · 第 {{ playIndex + 1 }}/{{ playable.length }} 段
        </span>
      </div>
    </div>

    <!-- ───────────────────────────── 右：素材 + 成片 -->
    <div style="display: flex; flex-direction: column; gap: 12px; min-height: 0">
      <div class="panel" style="flex: 1; min-height: 0">
        <div class="panel-head">
          镜头素材
          <span class="sub">{{ done }}/{{ total }} 已生成</span>
        </div>
        <div class="panel-body">
          <div class="clip-grid">
            <div
              v-for="c in clips"
              :key="c.node_id"
              class="clip-card"
              :class="{ on: c.node_id === current?.node_id }"
              @click="play(c)"
            >
              <div class="fig">
                <video v-if="c.video_path" :src="mediaUrl(c.video_path)" muted preload="metadata"></video>
                <span v-else>未生成</span>
                <span class="dur">{{ c.duration_s }}s</span>
              </div>
              <div class="cap">
                <span class="t" :title="c.title">#{{ c.segment_no }} {{ c.title }}</span>
                <span style="display: flex; gap: 4px; flex-wrap: wrap">
                  <span class="tag" :class="c.ready ? 'ok' : ''" style="font-size: 9px">
                    {{ c.ready ? "已完成" : "缺" }}
                  </span>
                  <span v-for="t in c.tags || []" :key="t" class="tag warn" style="font-size: 9px">{{ t }}</span>
                </span>
              </div>
            </div>
            <div v-if="!clips.length" class="empty" style="grid-column: 1 / -1">
              还没有片段<br />先去制作页生成
            </div>
          </div>
        </div>
      </div>

      <div class="panel" style="flex: none">
        <div class="panel-head">
          成片
          <span class="sub">{{ rendered.length }} 个</span>
          <span class="grow"></span>
          <span style="font-size: 11px; color: var(--text-3)">ffmpeg 归一化后拼接，画面比例跟随项目设定</span>
        </div>
        <div class="panel-body" style="padding: 10px 12px">
          <div v-if="!rendered.length" class="empty" style="padding: 16px">
            还没有成片<br />点「合成整集」把 {{ total }} 段接起来
          </div>
          <div v-else style="display: flex; flex-direction: column; gap: 8px">
            <div v-for="r in rendered" :key="r.path" class="ref-row" style="align-items: center">
              <span class="tag ok" style="font-size: 9px">成片</span>
              <span class="mono" style="flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap" :title="r.path">
                {{ r.name || r.path }}
              </span>
              <span style="font-size: 11px; color: var(--text-3)">{{ kb(r.size) }}</span>
              <a class="btn ghost sm" :href="mediaUrl(r.path)" target="_blank" rel="noopener">播放</a>
              <a class="btn ghost sm" :href="mediaUrl(r.path)" download>下载</a>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ───────────────────────────── 合成前确认浮层（人工审批点） -->
    <div v-if="plan" class="import-mask" @click.self="plan = null">
      <div class="import-box" style="width: min(640px, 92vw)">
        <div class="panel-head">
          确认合成
          <span class="sub">这一步对应成片管线的人工审批：顺序、时长、输出规格确认后才落盘</span>
          <span class="grow"></span>
          <button class="btn ghost sm" @click="plan = null">取消</button>
        </div>
        <div class="panel-body" style="display: flex; flex-direction: column; gap: 10px">
          <div class="field">
            <label>片段顺序（共 {{ plan.clips.length }} 段 · 合计 {{ plan.total_duration }}s）</label>
            <div style="display: flex; flex-direction: column; gap: 4px; max-height: 240px; overflow: auto">
              <div v-for="(c, i) in plan.clips" :key="c.node_id" class="ref-row">
                <span class="mono" style="color: var(--text-3)">{{ i + 1 }}</span>
                <span style="flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">{{ c.label || c.node_id }}</span>
                <span style="font-size: 11px; color: var(--text-3)">{{ (c.asset?.duration || 0).toFixed(1) }}s</span>
              </div>
            </div>
          </div>
          <div class="field">
            <label>输出规格</label>
            <div class="mono">{{ plan.spec }}</div>
          </div>
        </div>
        <div class="import-foot">
          <span class="sub">确认后会调用 ffmpeg 归一化 + 拼接</span>
          <span class="grow"></span>
          <button class="btn primary" :disabled="composing" @click="confirmCompose">确认并合成</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from "vue";
import { studio, toast, prepareComposition, runComposition, pollMediaJob } from "./studioStore.js";

const playerEl = ref(null);
const queue = ref([]);
const playIndex = ref(0);
const picked = ref("");
const plan = ref(null);
const composing = ref(false);

const clips = computed(() => studio.preview?.clips || []);
const rendered = computed(() => studio.preview?.rendered || []);
const total = computed(() => studio.preview?.total || 0);
const done = computed(() => studio.preview?.done || 0);
const pct = computed(() => (total.value ? Math.round((done.value / total.value) * 100) : 0));
const playable = computed(() => clips.value.filter((c) => c.video_path));

const current = computed(() => {
  if (picked.value) return clips.value.find((c) => c.node_id === picked.value) || null;
  return playable.value[0] || null;
});

function mediaUrl(p) {
  if (!p) return "";
  if (/^(https?:|data:|blob:)/.test(p)) return p;
  const clean = String(p).replace(/^\/+/, "");
  return clean.startsWith("assets/") ? "/" + clean : "/assets/" + clean;
}

function kb(n) {
  if (!n) return "";
  return n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`;
}

function play(clip) {
  picked.value = clip.node_id;
  queue.value = [];
}

function playAll() {
  queue.value = playable.value.map((c) => c.node_id);
  playIndex.value = 0;
  picked.value = "";
  next();
}

function next() {
  if (!queue.value.length) return;
  picked.value = queue.value[playIndex.value] || "";
  setTimeout(async () => {
    try {
      await playerEl.value?.play?.();
    } catch {
      /* 浏览器可能拒绝自动播放，用户手点即可 */
    }
  }, 60);
}

function onEnded() {
  if (!queue.value.length) return;
  playIndex.value += 1;
  if (playIndex.value >= queue.value.length) {
    queue.value = [];
    playIndex.value = 0;
    return;
  }
  next();
}

async function startCompose() {
  composing.value = true;
  try {
    const { assembled, approval } = await prepareComposition();
    const flat = (approval.plans || []).flatMap((job) =>
      (job.clips || []).map((clip) => ({ ...clip, label: clip.label || job.label })),
    );
    const duration = flat.reduce((sum, clip) => sum + Number(clip.asset?.duration || 0), 0);
    const project = studio.project?.project || {};
    plan.value = {
      approval_id: approval.approval_id,
      revision: assembled.revision,
      clips: flat,
      total_duration: Math.round(duration * 10) / 10,
      spec: `${assembled.clip_count} 段 · ${assembled.duration_s}s · ${project.aspect_ratio || "9:16"} · ${project.quality || "480P"}`,
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
      toast(job?.error || "合成失败", 4800);
    }
  } catch (err) {
    toast(err?.message || "合成失败", 4800);
  } finally {
    composing.value = false;
  }
}

async function exportPrompts() {
  try {
    const res = await fetch("/api/canvas/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ canvas: studio.canvas }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const data = await res.json();
    toast(`已导出 ${data.segments ?? ""} 段提示词`);
  } catch (err) {
    toast(err?.message || "导出失败", 3600);
  }
}
</script>
