<template>
  <div class="pv">
    <!-- ───────────────────────── 左：片段队列 -->
    <section class="pv-col pv-q">
      <header class="pv-head">
        <h3>片段队列</h3>
        <span class="sub">{{ doneCount }}/{{ studio.segments.length }} 已出片</span>
        <span class="grow"></span>
        <button class="btn ghost sm" :disabled="busy" @click="reload">刷新</button>
      </header>

      <div class="pv-solo">
        <button
          class="btn primary wide"
          :disabled="busy || batchBusy || !pendingList.length"
          @click="batchOpen = true"
        >
          {{ batchBusy ? `生成中 ${batchDone}/${batchTotal}` : `批量生成未出片的 ${pendingList.length} 段` }}
        </button>
      </div>

      <div class="pv-scroll pv-q-scroll">
        <button
          v-for="seg in studio.segments"
          :key="seg.node_id"
          class="q-row"
          :class="{ on: seg.node_id === studio.activeSegmentId }"
          @click="pick(seg.node_id)"
        >
          <span class="q-idx">
            <img v-if="seg.thumbnail" :src="mediaUrl(seg.thumbnail)" :alt="seg.title" />
            <template v-else>{{ seg.segment_no }}</template>
          </span>
          <span class="q-main">
            <span class="q-title">{{ seg.title || "未命名片段" }}</span>
            <span class="q-meta">
              <span>{{ seg.duration_s }}s</span>
              <span>{{ seg.take_count || 0 }} 版</span>
              <span v-if="seg.keyframe_path" class="q-kf">首帧</span>
            </span>
          </span>
          <span class="q-st" :class="seg.status">{{ statusText(seg.status) }}</span>
        </button>
        <div v-if="!studio.segments.length" class="empty">还没有片段<br />先在剧本页导入剧本</div>
      </div>
    </section>

    <!-- ───────────────────────── 中：工作区 -->
    <section class="pv-col pv-work">
      <header class="pv-head">
        <template v-if="detail">
          <h3>{{ detail.title || "未命名片段" }}</h3>
          <span class="sub">E{{ detail.episode_no }}-{{ String(detail.segment_no).padStart(2, "0") }}</span>
          <span class="tag" :class="lintTagClass">{{ lintTagText }}</span>
        </template>
        <template v-else>
          <h3>片段详情</h3>
        </template>
        <span class="grow"></span>
        <template v-if="detail">
          <button class="btn ghost sm" :disabled="busy || !dirty" @click="reset">还原</button>
          <button class="btn sm" :disabled="busy || !dirty" @click="save">保存提示词</button>
        </template>
      </header>

      <div v-if="!detail" class="pv-empty">左侧选一个片段</div>

      <div v-else class="pv-scroll">
        <!-- 分镜 -->
        <div class="card">
          <div class="card-h">
            <b>分镜</b>
            <span class="sub">{{ detail.shots.length }} 个镜头 · 合计 {{ shotsTotal }}s</span>
          </div>
          <div class="card-b card-b-flush">
            <table class="shot-table">
              <thead>
                <tr>
                  <th style="width: 34px">#</th>
                  <th style="width: 110px">主体</th>
                  <th>动作</th>
                  <th style="width: 60px">景别</th>
                  <th style="width: 60px">运镜</th>
                  <th style="width: 48px">时长</th>
                  <th style="width: 28%">声音 / 台词</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="s in detail.shots" :key="s.no">
                  <td class="num">{{ s.no }}</td>
                  <td>{{ s.subject }}</td>
                  <td>{{ s.action }}</td>
                  <td>{{ s.shot_size }}</td>
                  <td>{{ s.camera_move }}</td>
                  <td class="num">{{ s.duration }}s</td>
                  <td>
                    <div>{{ s.sound }}</div>
                    <div v-for="(l, i) in s.dialogue || []" :key="i" style="margin-top: 4px">
                      <span class="tag role">{{ l.speaker }}</span>
                      <span style="margin-left: 5px">{{ l.line }}</span>
                    </div>
                  </td>
                </tr>
                <tr v-if="!detail.shots.length">
                  <td colspan="7" class="td-empty">这段没有分镜记录</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- 提示词 -->
        <div class="card">
          <div class="card-h">
            <b>视频提示词</b>
            <span class="sub">@ 引用资产时写 @名字</span>
            <span class="grow"></span>
            <span v-if="dirty" class="tag warn">未保存</span>
          </div>
          <div class="card-b">
            <textarea v-model="promptDraft" class="prompt-box" spellcheck="false"></textarea>
            <div v-if="detail.mentions?.length" class="mentions">
              识别到引用：
              <span v-for="m in detail.mentions" :key="m" class="tag">@{{ m }}</span>
            </div>
          </div>
        </div>

        <!-- 迭代版本 -->
        <div class="card">
          <div class="card-h">
            <b>迭代版本</b>
            <span class="sub">{{ detail.takes.length }} 版 · 点一下切为当前版本</span>
          </div>
          <div class="card-b">
            <div v-if="!detail.takes.length" class="empty">还没有生成过<br />在下方点「生成这一镜」出第一版</div>
            <div v-else class="take-grid">
              <button
                v-for="(t, i) in detail.takes"
                :key="t.take_id"
                class="take-card"
                :class="{ on: t.take_id === detail.current_take_id }"
                :title="t.note || ''"
                @click="useTake(t.take_id)"
              >
                <video v-if="t.path" :src="mediaUrl(t.path)" muted preload="metadata"></video>
                <span v-else class="ph">无</span>
                <span v-if="t.take_id === detail.current_take_id" class="cur">当前</span>
                <span class="cap">
                  <span>第 {{ i + 1 }} 版</span>
                  <span>{{ t.duration_s ? t.duration_s + "s" : "" }}</span>
                </span>
                <span class="cap cap-time">{{ shortTime(t.created_at) }}</span>
              </button>
            </div>
          </div>
        </div>

        <!-- 校验 -->
        <div class="card">
          <div class="card-h">
            <b>校验</b>
            <span class="sub">{{ findings.length }} 条</span>
          </div>
          <div class="card-b list-gap">
            <div v-for="(f, i) in findings" :key="i" class="lint-item" :class="f.severity">
              <span class="ic">{{ f.severity === "error" ? "!" : f.severity === "warn" ? "△" : "·" }}</span>
              <span>{{ f.message }}</span>
            </div>
            <div v-if="!findings.length" class="empty" style="padding: 14px">没有问题，这一镜可以直接生成</div>
          </div>
        </div>
      </div>

      <!-- 底部操作条 -->
      <div v-if="detail" class="act-bar">
        <label class="dur">
          时长
          <input v-model.number="durationDraft" type="number" min="1" max="30" step="0.5" />
          <span class="sub">{{ durationSourceText }}</span>
        </label>

        <div class="act-meta">
          <span class="chip">{{ studio.project?.project?.video_model || "mock-video-v1" }}</span>
          <span class="chip">{{ studio.project?.project?.quality || "480P" }}</span>
          <span class="chip">{{ studio.project?.project?.aspect_ratio || "9:16" }}</span>
          <span class="chip" :class="detail.keyframe?.path ? 'on' : ''">
            {{ detail.keyframe?.path ? "首帧模式" : "参考图模式" }}
          </span>
        </div>

        <span class="grow"></span>

        <div v-if="jobActive" class="job-bar">
          <span class="studio-spin"></span>
          <span>{{ jobText }}</span>
          <button class="btn ghost sm" @click="cancel">取消</button>
        </div>
        <template v-else>
          <button class="btn sm" :disabled="busy" @click="makeKeyframe">
            {{ detail.keyframe?.path ? "重画首帧" : "生成首帧" }}
          </button>
          <button
            v-if="detail.lint?.blocking"
            class="btn sm danger"
            @click="generate(true)"
          >
            忽略问题强制生成
          </button>
          <button class="btn primary" :disabled="busy" @click="generate(false)">
            {{ detail.takes.length ? "再抽一版" : "生成这一镜" }}
          </button>
        </template>
      </div>
    </section>

    <!-- ───────────────────────── 右：预览与素材 -->
    <section class="pv-col pv-side">
      <header class="pv-head">
        <h3>预览</h3>
        <span class="sub">{{ currentTake ? shortTime(currentTake.created_at) : "还没有出片" }}</span>
      </header>

      <div class="pv-scroll">
        <div class="card">
          <div class="preview-stage">
            <video
              v-if="playerUrl"
              :key="playerUrl"
              :src="playerUrl"
              controls
              playsinline
              preload="metadata"
            ></video>
            <div v-else class="preview-empty" :style="{ aspectRatio: ratioCss }">
              还没有视频<br />生成后在这里预览
            </div>
          </div>
          <div class="preview-foot">
            <span class="sub">
              {{ detail?.takes.length || 0 }} 个版本
              <template v-if="currentTake"> · 当前 {{ currentTake.duration_s || "—" }}s</template>
            </span>
            <span class="grow"></span>
            <a v-if="playerUrl" class="btn ghost sm" :href="playerUrl" target="_blank" rel="noopener">下载当前版本</a>
          </div>
        </div>

        <!-- 首帧 -->
        <div class="card">
          <div class="card-h">
            <b>首帧</b>
            <span class="sub">{{ detail?.keyframe?.path ? "这镜以它驱动" : "没首帧时模型只会让参考图微动" }}</span>
          </div>
          <div class="card-b kf-body">
            <div class="kf-thumb">
              <img v-if="detail?.keyframe?.path" :src="mediaUrl(detail.keyframe.path)" alt="首帧" />
              <span v-else>空</span>
            </div>
            <div class="kf-note">
              <p><b>首帧 → 视频</b>：第 0 秒的画面由这张图决定，模型只负责往后演这一条动作。</p>
              <p v-if="detail?.keyframe?.model">
                模型 {{ detail.keyframe.model }}
                <span v-if="detail.keyframe.mock">（Mock 占位）</span>
              </p>
            </div>
          </div>
        </div>

        <!-- 参考素材 -->
        <div class="card">
          <div class="card-h">
            <b>参考素材</b>
            <span class="sub">{{ readyRefs }}/{{ detail?.references?.length || 0 }} 已就绪</span>
          </div>
          <div class="card-b list-gap">
            <div v-for="r in detail?.references || []" :key="r.ordinal" class="ref-row">
              <img v-if="r.path" :src="mediaUrl(r.path)" :alt="r.name" />
              <div v-else class="ph">缺</div>
              <div class="nm" :title="r.name">
                {{ r.name }}
                <div class="sub">{{ kindText(r.kind) }}</div>
              </div>
              <span v-if="r.path" class="ord">图片{{ r.ordinal }}</span>
              <button v-else class="btn ghost sm" @click="goAssets(r.asset_id)">去生成</button>
            </div>
            <div v-if="!detail?.references?.length" class="empty" style="padding: 12px">这段没有引用资产</div>
          </div>
        </div>

        <!-- 标记 -->
        <div class="card">
          <div class="card-h">
            <b>这一镜的问题</b>
            <span class="sub">标记后方便挑版本</span>
          </div>
          <div class="card-b tags">
            <button
              v-for="tg in PRESET_TAGS"
              :key="tg"
              class="btn sm"
              :class="{ primary: (detail?.tags || []).includes(tg) }"
              :disabled="busy"
              @click="toggleTag(tg)"
            >
              {{ tg }}
            </button>
          </div>
        </div>
      </div>
    </section>

    <!-- ───────────────────────── 批量生成确认 -->
    <div v-if="batchOpen" class="import-mask" @click.self="batchOpen = false">
      <div class="import-box" style="max-width: 600px">
        <div class="panel-head">
          批量生成 {{ pendingList.length }} 个片段
          <span class="grow"></span>
          <button class="btn ghost sm" @click="batchOpen = false">取消</button>
        </div>
        <div class="panel-body" style="padding: 16px 18px">
          <div class="sub" style="line-height: 2">
            只生成还<b>没有出片</b>的片段，已经出片的不动（重跑请在该片段里单独点）。
            <br />合计时长 <b>{{ pendingDuration }}s</b> · 模型
            <b>{{ studio.project?.project?.video_model || "mock-video-v1" }}</b> ·
            {{ studio.project?.project?.aspect_ratio || "9:16" }} /
            {{ studio.project?.project?.quality || "480P" }}
            <br />
            <span v-if="isMock" style="color: var(--text-3)">
              当前是 Mock，本地生成不花钱，秒级返回。
            </span>
            <span v-else style="color: var(--warn, #d97706)">
              当前是真模型：按 5 秒一镜约 1 元估算，这一批大约 ¥{{ (pendingDuration / 5).toFixed(0) }}，扣费不可撤销。
            </span>
          </div>
        </div>
        <div class="import-foot">
          <span class="sub">{{ batchBusy ? `进行中 ${batchDone}/${batchTotal}` : "生成期间可以继续看别的片段" }}</span>
          <span class="grow"></span>
          <button class="btn primary" :disabled="batchBusy" @click="runBatch">开始生成</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from "vue";
import {
  studio,
  loadSegments,
  loadDetail,
  savePrompt,
  saveTags,
  selectTake,
  generateSegment,
  generateKeyframe,
  cancelJob,
  toast,
} from "./studioStore.js";

const PRESET_TAGS = ["可用", "人脸漂移", "站位有误", "台词问题", "动作僵硬", "镜头太快"];

const promptDraft = ref("");
const durationDraft = ref(0);
const busy = ref(false);
const jobActive = ref(false);
const jobText = ref("");
const batchOpen = ref(false);
const batchBusy = ref(false);
const batchDone = ref(0);
const batchTotal = ref(0);

/* 批量只跑「还没出片」的片段：出片成本高，重跑必须由人针对某一镜单独决定。 */
const pendingList = computed(() => studio.segments.filter((s) => !s.has_video && s.status !== "generating"));
const pendingDuration = computed(() => pendingList.value.reduce((sum, s) => sum + Number(s.duration_s || 0), 0));
const isMock = computed(() => {
  const m = String(studio.project?.project?.video_model || "");
  return !m || m.startsWith("mock");
});

async function runBatch() {
  batchBusy.value = true;
  batchDone.value = 0;
  const targets = pendingList.value.slice();
  batchTotal.value = targets.length;
  let ok = 0;
  let failed = 0;
  for (const seg of targets) {
    try {
      await generateSegment(seg.node_id, { duration: seg.duration_s });
      ok += 1;
    } catch (err) {
      failed += 1;
    }
    batchDone.value += 1;
  }
  batchBusy.value = false;
  batchOpen.value = false;
  await loadSegments();
  toast(`批量生成完成：成功 ${ok} 段${failed ? `，失败 ${failed} 段` : ""}`, 4200);
}

const detail = computed(() => studio.detail);
const findings = computed(() => detail.value?.lint?.findings || []);
const readyRefs = computed(() => (detail.value?.references || []).filter((r) => r.path).length);
const doneCount = computed(() => studio.segments.filter((s) => s.status === "done").length);
const dirty = computed(() => detail.value && promptDraft.value !== (detail.value.prompt || ""));
const shotsTotal = computed(() =>
  (detail.value?.shots || []).reduce((sum, s) => sum + Number(s.duration || 0), 0).toFixed(1)
);

/* 当前版本用于右侧大预览：优先当前 take，其次列表里最后一个。 */
const currentTake = computed(() => {
  const d = detail.value;
  if (!d) return null;
  const takes = d.takes || [];
  return takes.find((t) => t.take_id === d.current_take_id) || takes[takes.length - 1] || null;
});
const playerUrl = computed(() => mediaUrl(currentTake.value?.path || detail.value?.video_path || ""));
const ratioCss = computed(() => String(studio.project?.project?.aspect_ratio || "9:16").replace(":", " / "));

const lintTagClass = computed(() => {
  const sev = detail.value?.lint?.severity;
  if (sev === "error") return "err";
  if (sev === "warn") return "warn";
  return "ok";
});
const lintTagText = computed(() => {
  const lint = detail.value?.lint;
  if (!lint) return "";
  if (lint.blocking) return `${lint.findings.filter((f) => f.severity === "error").length} 个问题会坏片`;
  if (lint.severity === "warn") return "可以生成，但有提醒";
  return "无阻塞问题";
});

const durationSourceText = computed(() => {
  const s = detail.value?.duration_source;
  return (
    { "data.duration_s": "来自节点设定", prompt: "从提示词推出", shots: "从分镜合计推出" }[s] || s || ""
  );
});

function mediaUrl(p) {
  if (!p) return "";
  if (/^(https?:|data:|blob:)/.test(p)) return p;
  const clean = String(p).replace(/^\/+/, "");
  return clean.startsWith("assets/") ? "/" + clean : "/assets/" + clean;
}
function statusText(s) {
  return { pending: "未生成", generating: "生成中", done: "已完成", error: "失败", no_slot: "缺生成节点" }[s] || s;
}
function kindText(k) {
  return { character: "角色", scene: "场景", prop: "道具", style: "风格" }[k] || k || "";
}
function shortTime(v) {
  if (!v) return "";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return String(v).slice(-8);
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(
    d.getHours()
  ).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

async function pick(nodeId) {
  if (studio.activeSegmentId === nodeId) return;
  studio.activeSegmentId = nodeId;
  await loadDetail(nodeId);
}
async function reload() {
  await loadSegments();
  if (studio.activeSegmentId) await loadDetail(studio.activeSegmentId);
  else if (studio.segments.length) await pick(studio.segments[0].node_id);
}
async function save() {
  busy.value = true;
  try {
    await savePrompt(detail.value.node_id, promptDraft.value);
    toast("已保存");
    durationDraft.value = detail.value?.duration_s || 0;
  } catch (err) {
    toast(err?.message || "保存失败");
  } finally {
    busy.value = false;
  }
}
function reset() {
  promptDraft.value = detail.value?.prompt || "";
}
async function useTake(takeId) {
  busy.value = true;
  try {
    await selectTake(detail.value.node_id, takeId);
    toast("已设为当前版本");
  } catch (err) {
    toast(err?.message || "切换失败");
  } finally {
    busy.value = false;
  }
}
async function toggleTag(tg) {
  const cur = detail.value.tags || [];
  const next = cur.includes(tg) ? cur.filter((t) => t !== tg) : [...cur, tg];
  busy.value = true;
  try {
    await saveTags(detail.value.node_id, next);
  } catch (err) {
    toast(err?.message || "标记失败");
  } finally {
    busy.value = false;
  }
}
function goAssets(assetId) {
  studio.assetTab = studio.assetById(assetId)?.kind || "character";
  studio.activeAssetId = assetId;
  studio.tab = "assets";
}

async function makeKeyframe() {
  const nodeId = detail.value.node_id;
  const shotNo = Number(detail.value.shots?.[0]?.no) || 1;
  busy.value = true;
  jobActive.value = true;
  jobText.value = "正在画首帧…";
  try {
    await generateKeyframe(nodeId, { shotNo });
    toast("首帧好了，可以出片了");
  } catch (err) {
    toast(err?.message || "首帧生成失败", 3600);
  } finally {
    busy.value = false;
    jobActive.value = false;
  }
}

async function generate(force) {
  const nodeId = detail.value.node_id;
  const shotNo = Number(detail.value.shots?.[0]?.no) || 1;
  const duration = force ? Number(durationDraft.value) || detail.value.duration_s || 15 : 0;
  if (force && !window.confirm("这段的提示词有会坏片的问题，仍然生成？")) return;

  busy.value = true;
  jobActive.value = true;
  jobText.value = "正在生成…";
  try {
    const job = await generateSegment(nodeId, { duration, shotNo });
    if (job?.status === "succeeded" || job?.status === "done") toast("出片了");
    else if (job?.status === "cancelled") toast("已取消");
    else toast(job?.error || "生成失败");
  } catch (err) {
    const findings = err?.payload?.detail?.findings;
    if (err?.status === 422 && findings?.length) {
      toast(`校验未通过：${findings[0].message}`, 3600);
    } else {
      toast(err?.message || "生成失败", 3600);
    }
  } finally {
    busy.value = false;
    jobActive.value = false;
  }
}
async function cancel() {
  await cancelJob(detail.value?.active_job_id);
  jobActive.value = false;
}

watch(
  () => studio.detail,
  (d) => {
    promptDraft.value = d?.prompt || "";
    durationDraft.value = d?.duration_s || 0;
  }
);

onMounted(async () => {
  if (!studio.segments.length) await loadSegments();
  const id = studio.activeSegmentId || studio.segments[0]?.node_id;
  if (id) await pick(id);
});
</script>

<style scoped>
/* 制作页：三栏（队列 / 工作区 / 预览），比原先的四栏松一档。
   目标不是好看，是让工作人员能连续干活：队列要一眼扫完，提示词要看得清，
   成片要够大能判断，按钮固定在底部不用来回找。 */
.pv {
  flex: 1;
  display: grid;
  grid-template-columns: 264px minmax(0, 1fr) 392px;
  min-height: 0;
  height: 100%;
}

.pv-col {
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}
.pv-col + .pv-col {
  border-left: 1px solid var(--line);
}

.pv-head {
  flex: none;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 13px 16px;
  border-bottom: 1px solid var(--line);
  background: var(--bg-overlay);
}
.pv-head h3 {
  margin: 0;
  font-size: 13px;
  font-weight: 650;
  letter-spacing: 0.01em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.pv-head .sub,
.pv .sub {
  font-size: 11px;
  color: var(--text-3);
  font-weight: 400;
}
.pv .grow {
  margin-left: auto;
}

.pv-solo {
  padding: 12px 14px 6px;
}

.pv-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 14px 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.pv-q-scroll {
  padding: 4px 10px 16px;
  gap: 4px;
}
.pv-empty {
  flex: 1;
  display: grid;
  place-items: center;
  color: var(--text-3);
  font-size: 12.5px;
}

/* ── 队列 ── */
.q-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  width: 100%;
  padding: 10px;
  border: 1px solid transparent;
  border-radius: var(--ui-radius);
  background: transparent;
  text-align: left;
  cursor: pointer;
  transition: background var(--dur-fast) var(--ease-out), border-color var(--dur-fast) var(--ease-out);
}
.q-row:hover {
  background: var(--card);
}
.q-row.on {
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary-light-8);
}
.q-idx {
  flex: none;
  width: 38px;
  height: 38px;
  border-radius: 8px;
  background: var(--card-strong);
  color: var(--text-2);
  font-size: 12px;
  font-weight: 600;
  display: grid;
  place-items: center;
  overflow: hidden;
}
.q-idx img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.q-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.q-title {
  font-size: 12.5px;
  font-weight: 600;
  line-height: 1.35;
  color: var(--text);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.q-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 10.5px;
  color: var(--text-3);
}
.q-kf {
  padding: 0 5px;
  border-radius: var(--radius-pill);
  background: #e9f7f0;
  color: #116c4c;
}
.q-st {
  flex: none;
  margin-top: 2px;
  padding: 2px 7px;
  border-radius: var(--radius-pill);
  font-size: 10px;
  background: var(--card-strong);
  color: var(--text-3);
}
.q-st.done {
  background: #e9f7f0;
  color: #116c4c;
}
.q-st.generating {
  background: #eef4ff;
  color: #2559b8;
}
.q-st.error {
  background: #fdeef1;
  color: #a3243b;
}

/* ── 卡片 ── */
.card {
  flex: none; /* 列是 flex 容器，不写这条卡片会被压扁、内容被裁掉 */
  border: 1px solid var(--line);
  border-radius: var(--ui-radius);
  background: var(--bg-overlay);
  overflow: hidden;
}
.card-h {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 11px 14px;
  border-bottom: 1px solid var(--line);
  background: var(--card);
}
.card-h b {
  font-size: 12.5px;
  font-weight: 650;
}
.card-b {
  padding: 14px;
}
.card-b .empty {
  padding: 22px 16px;
}
.card-b-flush {
  padding: 0;
}
.list-gap {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

/* ── 分镜表 ── */
.shot-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.shot-table th {
  padding: 8px;
  text-align: left;
  font-weight: 600;
  font-size: 11px;
  color: var(--text-3);
  border-bottom: 1px solid var(--line);
  background: var(--bg-overlay);
}
.shot-table td {
  padding: 9px 8px;
  border-bottom: 1px dashed var(--line);
  vertical-align: top;
  line-height: 1.55;
}
.shot-table tr:last-child td {
  border-bottom: 0;
}
.shot-table .num {
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}
.td-empty {
  color: var(--text-3);
  text-align: center;
  padding: 18px;
}

/* ── 提示词 ── */
.prompt-box {
  width: 100%;
  min-height: 300px;
  resize: vertical;
  padding: 13px 14px;
  border: 1px solid var(--line);
  border-radius: var(--ui-radius-small);
  background: var(--bg-overlay);
  color: var(--text);
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 12.5px;
  line-height: 1.75;
}
.prompt-box:focus {
  outline: 2px solid var(--el-color-primary-light-7);
  border-color: var(--primary);
}
.mentions {
  margin-top: 10px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: var(--text-3);
}

/* ── 版本 ── */
.take-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(118px, 1fr));
  gap: 10px;
}
.take-card {
  position: relative;
  padding: 0;
  border: 1px solid var(--line);
  border-radius: var(--ui-radius-small);
  background: var(--bg-overlay);
  overflow: hidden;
  cursor: pointer;
  text-align: left;
}
.take-card:hover {
  border-color: var(--line-strong);
}
.take-card.on {
  border-color: var(--primary);
  box-shadow: 0 0 0 2px var(--el-color-primary-light-8);
}
.take-card video,
.take-card .ph {
  width: 100%;
  height: 104px;
  object-fit: cover;
  display: block;
  background: #0d1117;
  color: #6b7280;
}
.take-card .cur {
  position: absolute;
  top: 5px;
  right: 5px;
  padding: 1px 6px;
  border-radius: var(--radius-pill);
  background: var(--primary);
  color: #fff;
  font-size: 9px;
}
.take-card .cap {
  display: flex;
  justify-content: space-between;
  gap: 4px;
  padding: 4px 7px;
  font-size: 10px;
  color: var(--text-2);
}
.take-card .cap-time {
  padding-top: 0;
  color: var(--text-3);
}

/* ── 校验 ── */
.lint-item {
  display: flex;
  gap: 8px;
  padding: 9px 11px;
  border-radius: var(--ui-radius-small);
  font-size: 12px;
  line-height: 1.55;
}
.lint-item.error {
  background: #fdeef1;
  color: #8d1f33;
}
.lint-item.warn {
  background: #fff7e6;
  color: #8a5200;
}
.lint-item.info {
  background: var(--card);
  color: var(--text-2);
}
.lint-item .ic {
  flex: none;
  font-weight: 700;
}

/* ── 底部操作条 ── */
.act-bar {
  flex: none;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 11px 16px;
  border-top: 1px solid var(--line);
  background: var(--bg-overlay);
  flex-wrap: wrap;
}
.act-bar .dur {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12px;
  color: var(--text-2);
}
.act-bar .dur input {
  width: 64px;
  padding: 6px 8px;
  border: 1px solid var(--line);
  border-radius: var(--ui-radius-small);
  background: var(--bg-overlay);
  color: var(--text);
  font-size: 12px;
}
.act-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.act-meta .chip {
  padding: 3px 8px;
  border-radius: var(--radius-pill);
  background: var(--card);
  color: var(--text-2);
  font-size: 11px;
}
.act-meta .chip.on {
  background: #e9f7f0;
  color: #116c4c;
}
.act-bar .job-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-2);
}
.btn.danger {
  border-color: #f0b7c3;
  color: #a3243b;
}

/* ── 预览 ── */
.preview-stage {
  display: flex;
  justify-content: center;
  align-items: center;
  background: #0d1117;
  padding: 10px 0;
}
/* 竖屏片子按高度铺满，避免两侧留大黑边（宽度交给比例决定） */
.preview-stage video {
  height: min(52vh, 500px);
  width: auto;
  max-width: 100%;
  object-fit: contain;
  background: #0d1117;
  display: block;
}
.preview-empty {
  width: 100%;
  max-height: 460px;
  display: grid;
  place-items: center;
  text-align: center;
  font-size: 12px;
  line-height: 1.9;
  color: #6b7280;
}
.preview-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 12px;
  border-top: 1px solid var(--line);
}

/* ── 首帧 ── */
.kf-body {
  display: flex;
  gap: 13px;
  align-items: flex-start;
}
.kf-thumb {
  flex: none;
  width: 92px;
  height: 162px;
  border-radius: var(--ui-radius-small);
  overflow: hidden;
  background: var(--card);
  display: grid;
  place-items: center;
  font-size: 11px;
  color: var(--text-3);
}
.kf-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.kf-note {
  font-size: 11px;
  line-height: 1.75;
  color: var(--text-3);
}
.kf-note p {
  margin: 0 0 6px;
}
.kf-note b {
  color: var(--text-2);
}

/* ── 参考素材 ── */
.ref-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 0;
}
.ref-row + .ref-row {
  border-top: 1px dashed var(--line);
}
.ref-row img,
.ref-row .ph {
  width: 40px;
  height: 52px;
  border-radius: 5px;
  object-fit: cover;
  background: var(--card);
  display: grid;
  place-items: center;
  font-size: 10px;
  color: var(--text-3);
  flex: none;
}
.ref-row .nm {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ref-row .ord {
  flex: none;
  font-size: 10px;
  color: var(--text-3);
}

.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}
</style>
