<script setup>
/* 制作页（demo v4 屏2）：概要 | 片段队列 | 提示词编辑器 | 视频预览。
 *
 * 交互语义（spec §6）：
 *   · 提示词字段保存 = POST /segments/{id}/prompt，转为人改，服务端不再覆盖；
 *   · 重生成只追加版本、不抢占采用版；采用走 POST /take 显式切换；
 *   · 硬校验拦截显式展示（lint.blocking），强制生成必须带明确时长（人工确认语义）；
 *   · 分镜表为只读（分镜编辑在后端无写入端点，改动走重新制作/画布）。
 */
import { computed, inject, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { applyField } from "../ai/diff.js";
import { errorText } from "../api.js";
import { assetUrl as mediaUrl } from "../utils.js";
import {
  generateKeyframe,
  generateSegment,
  loadAll,
  loadDetail,
  loadScript,
  loadSegments,
  savePrompt,
  saveTags,
  selectTake,
  studio,
  toast,
} from "../studio/studioStore.js";

const route = useRoute();
const router = useRouter();
const openAi = inject("ai-open", () => {});

const busy = ref(false);
const mode = ref("structured"); // structured | raw
const rawText = ref("");
const editingKey = ref("");
const editValue = ref("");
const lintBlock = ref(""); // 强制生成前展示的拦截原因

const detail = computed(() => studio.detail);
const segments = computed(() => studio.segments);
const episodes = computed(() => studio.script?.episodes || []);
const currentEp = computed(() => Number(studio.project?.episode || episodes.value[0]?.no || 1));
const segDone = computed(() => segments.value.filter((s) => s.status === "done").length);

const STATUS_MAP = {
  done: { cls: "ok", text: "完成" },
  stale: { cls: "warn", text: "过期" },
  partial: { cls: "warn", text: "部分" },
};
function segStatus(s) {
  if (s.keyframe_stale || s.video_stale) return STATUS_MAP.stale;
  return STATUS_MAP[s.status] || { cls: "mute", text: s.has_video ? "完成" : "未完成" };
}

/* ---------- 提示词结构化 ---------- */
const FIELDS = ["禁止", "人物", "场景", "道具", "画风", "站位", "画面", "台词", "时长", "镜头"];
const promptFields = computed(() => {
  const rows = [];
  for (const line of String(detail.value?.prompt || "").split("\n")) {
    const m = line.match(/^【([^】]+)】([\s\S]*)$/);
    if (m) rows.push({ k: m[1], v: m[2] });
  }
  return rows;
});
const promptHead = computed(() => {
  const head = String(detail.value?.prompt || "").split("\n").filter((l) => l && !l.startsWith("【"));
  return head.join("\n");
});

function startEdit(k) {
  editingKey.value = k;
  editValue.value = promptFields.value.find((f) => f.k === k)?.v || "";
}
async function saveField() {
  const k = editingKey.value;
  if (!k) return;
  editingKey.value = "";
  busy.value = true;
  try {
    await savePrompt(detail.value.node_id, applyField(detail.value.prompt || "", k, editValue.value));
    toast(`已保存 ·【${k}】转为人改，服务端不再覆盖`);
  } catch (err) {
    toast(errorText(err));
  } finally {
    busy.value = false;
  }
}
async function saveRaw() {
  busy.value = true;
  try {
    await savePrompt(detail.value.node_id, rawText.value);
    toast("已保存 · 提示词转为人改，服务端不再覆盖");
  } catch (err) {
    toast(errorText(err));
  } finally {
    busy.value = false;
  }
}
function aiFix(k) {
  openAi(`把片段 ${detail.value.segment_no} 的【${k}】改得好一点，用 dramaflow-diff 给我确认`);
}

/* ---------- lint / 生成 ---------- */
const lint = computed(() => detail.value?.lint || null);
const lintItems = computed(() => (lint.value?.findings || []).filter((f) => f.severity === "error"));
const genBusy = computed(() => !!studio.generating[detail.value?.node_id]);
const genParams = computed(() => {
  const p = studio.project || {};
  return `${p.video_model || "mock"} · ${p.aspect_ratio || "9:16"} · ${p.quality || "480P"}`;
});

async function generate(force = false) {
  const node = detail.value?.node_id;
  if (!node) return;
  lintBlock.value = "";
  busy.value = true;
  try {
    const opts = force ? { duration: Number(detail.value.duration_s) || 0, force: true } : {};
    const job = await generateSegment(node, opts);
    if (job.status === "succeeded") toast("已出片 · 新版本进入迭代列表，当前采用版不变");
    else toast(`生成未完成：${job.status}`);
  } catch (err) {
    lintBlock.value = errorText(err);
  } finally {
    busy.value = false;
  }
}

async function batchGenerate() {
  const todo = segments.value.filter((s) => !s.has_video);
  if (!todo.length) return;
  busy.value = true;
  let ok = 0;
  const failed = [];
  try {
    for (const s of todo) {
      try {
        const job = await generateSegment(s.node_id, {});
        if (job.status === "succeeded") ok += 1;
        else failed.push(`${s.segment_no}(${job.status})`);
      } catch (err) {
        failed.push(`${s.segment_no}(${errorText(err).slice(0, 24)})`);
      }
    }
    toast(`批量出片：成功 ${ok}/${todo.length}${failed.length ? `；未过：${failed.join("、")}` : ""}`);
  } finally {
    busy.value = false;
  }
}

/* ---------- 版本 / 标记 / 首帧 ---------- */
const takes = computed(() => (detail.value?.takes || []).slice().reverse());
const TAGS = ["人脸漂移", "站位有误", "台词问题", "可用"];
const tags = computed(() => detail.value?.tags || []);
async function toggleTag(t) {
  const next = tags.value.includes(t) ? tags.value.filter((x) => x !== t) : [...tags.value, t];
  try {
    await saveTags(detail.value.node_id, next);
  } catch (err) {
    toast(errorText(err));
  }
}
async function redoKeyframe() {
  busy.value = true;
  try {
    await generateKeyframe(detail.value.node_id, { shotNo: 1 });
    toast("首帧已重出 · 构图以新首帧为准");
  } catch (err) {
    toast(errorText(err));
  } finally {
    busy.value = false;
  }
}

/* ---------- 选择与切换 ---------- */
function pick(s) {
  studio.activeSegmentId = s.node_id;
  loadDetail(s.node_id).catch((err) => toast(errorText(err)));
}
watch(
  () => studio.activeSegmentId,
  () => {
    editingKey.value = "";
    lintBlock.value = "";
  },
);
async function switchEpisode(no) {
  studio.project.episode = Number(no);
  busy.value = true;
  try {
    await Promise.all([loadSegments(), loadScript()]);
    const first = studio.segments[0];
    if (first) await loadDetail(first.node_id);
  } finally {
    busy.value = false;
  }
}
onMounted(async () => {
  const name = String(route.params.name || "");
  if (name && studio.canvas !== name) studio.canvas = name;
  busy.value = true;
  try {
    await loadAll();
  } finally {
    busy.value = false;
  }
});
</script>

<template>
  <div class="prod">
    <div class="prod-top">
      <select class="ep-sel" :value="currentEp" @change="switchEpisode($event.target.value)">
        <option v-for="e in episodes" :key="e.no" :value="e.no">{{ e.title || `第${e.no}集` }}</option>
      </select>
      <span class="st ok"><i></i>进行中</span>
      <span class="count num">完成 {{ segDone }}/{{ segments.length }} · {{ currentEp }} 集</span>
      <div class="bar"><i :style="{ width: `${segments.length ? Math.round((segDone / segments.length) * 100) : 0}%` }"></i></div>
      <div class="grow"></div>
      <span v-if="studio.notice" class="notice">{{ studio.notice }}</span>
      <button class="btn sm" type="button" @click="router.push(`/project/${route.params.name}/canvas`)">画布视图</button>
      <button class="btn sm" type="button" @click="router.push(`/project/${route.params.name}/wall`)">首帧墙</button>
    </div>

    <div class="prod4">
      <!-- ── 概要 ── -->
      <div class="col col-overview">
        <div class="col-cap cap">第 {{ currentEp }} 集 · 概要</div>
        <div class="summary ui-card">
          <span class="ui-badge gold">第 {{ currentEp }} 集 · {{ episodes.length }} 集</span>
          <b class="s-title">{{ studio.project?.title || "未命名项目" }}</b>
          <div class="ui-meta">
            <div class="row"><span class="k">画风</span><span class="v">{{ studio.project?.style_preset || "未设" }}</span></div>
            <div class="row"><span class="k">画幅画质</span><span class="v num">{{ studio.project?.aspect_ratio || "—" }} · {{ studio.project?.quality || "—" }}</span></div>
            <div class="row"><span class="k">出片</span><span class="v num">{{ segDone }}/{{ segments.length }} 段完成</span></div>
          </div>
          <p class="dim">一镜一段 3-8s · 时长与台词由系统校验</p>
        </div>
      </div>

      <!-- ── 片段队列 ── -->
      <div class="col">
        <div class="col-cap cap">片段队列 <span class="grow"></span><span class="num" style="letter-spacing: 0">{{ segDone }}/{{ segments.length }}</span></div>
        <div class="seg-scroll">
          <div
            v-for="s in segments"
            :key="s.node_id"
            class="seg-item"
            :class="{ cur: s.node_id === studio.activeSegmentId }"
            @click="pick(s)"
          >
            <span class="no num">{{ s.segment_no }}</span>
            <span class="st" :class="segStatus(s).cls"><i></i>{{ segStatus(s).text }}</span>
            <span v-if="s.duration_s" class="dur num">{{ s.duration_s }}s</span>
          </div>
          <div v-if="!segments.length" class="ui-empty seg-empty">
            <span class="ico">🎬</span>
            <span class="tt">这一集还没有片段</span>
            <span class="ds">回剧本页点「开始制作」，Agent 会拆分镜、写提示词，片段会出现在这里</span>
            <span class="act">
              <button class="ui-btn primary sm" type="button" @click="router.push(`/project/${route.params.name}/script`)">去剧本页开始制作</button>
            </span>
          </div>
        </div>
        <div class="side-acts">
          <button class="side-act" type="button" @click="router.push(`/project/${route.params.name}/wall`)">首帧墙 · 整集质检</button>
          <button class="side-act primary" type="button" :disabled="busy || !segments.some((s) => !s.has_video)" @click="batchGenerate">
            批量生成未出片 ({{ segments.filter((s) => !s.has_video).length }})
          </button>
        </div>
      </div>

      <!-- ── 提示词编辑器 ── -->
      <div class="col main-col">
        <div class="col-cap">
          <span class="t">提示词</span>
          <span class="cap" style="letter-spacing: .06em">{{ detail ? `${detail.segment_no} · ${detail.title || ""}` : "—" }}</span>
          <span class="grow"></span>
          <span class="mode-sw">
            <button :class="{ on: mode === 'structured' }" type="button" @click="mode = 'structured'">结构化</button>
            <button :class="{ on: mode === 'raw' }" type="button" @click="(mode = 'raw'), (rawText = detail?.prompt || '')">原文</button>
          </span>
        </div>

        <div v-if="detail" class="prompt-pane">
          <div class="edit-hint">悬停任意字段可直接改——改过的内容标记为<b>人改</b>，服务端不再覆盖；或点「让 AI 改」看建议再应用</div>
          <template v-if="mode === 'structured'">
            <div v-if="promptHead" class="prompt-head num">{{ promptHead }}</div>
            <div v-for="f in promptFields" :key="f.k" class="sec" :class="{ editing: editingKey === f.k }">
              <span class="tag-k">{{ f.k }}</span>
              <span v-if="editingKey !== f.k" class="v">{{ f.v }}</span>
              <textarea v-else v-model="editValue" class="sec-edit" @keydown.ctrl.enter="saveField"></textarea>
              <button v-if="editingKey !== f.k" class="edit-enter" type="button" @click="startEdit(f.k)">点击编辑</button>
              <button v-if="editingKey !== f.k" class="ai-fix" type="button" @click="aiFix(f.k)">让 AI 改</button>
              <span v-if="editingKey === f.k" class="sec-acts">
                <button class="ok-b" type="button" :disabled="busy" @click="saveField">保存</button>
                <button class="no-b" type="button" @click="editingKey = ''">取消</button>
              </span>
            </div>
          </template>

          <template v-else>
            <textarea v-model="rawText" class="sec-edit raw" spellcheck="false"></textarea>
            <div class="raw-acts">
              <button class="btn sm primary" type="button" :disabled="busy" @click="saveRaw">保存全文</button>
              <span class="tip">保存后整段转为人改</span>
            </div>
          </template>

          <!-- 分镜表（只读：分镜编辑后端无写入端点） -->
          <div v-if="detail.shots?.length" class="shot-tbl">
            <div class="cap" style="padding-bottom: 8px">分镜表 · {{ detail.shots.length }} 镜（只读，改动走重新制作/画布）</div>
            <div v-for="shot in detail.shots" :key="shot.no" class="shot-row">
              <span class="sn num">{{ shot.no }}</span>
              <span class="pill">{{ shot.shot_size || "—" }}</span>
              <span class="pill">{{ shot.camera_move || shot.camera || "固定" }}</span>
              <span v-if="shot.duration_s || shot.duration" class="pill num">{{ shot.duration_s || shot.duration }}s</span>
              <span class="snd">{{ shot.sound || (shot.dialogue?.line ?? "") }}</span>
            </div>
          </div>

          <!-- 硬校验 -->
          <div v-if="lint && !lint.blocking" class="lint-line">
            硬校验通过——时长 {{ detail.duration_s ?? "—" }}s · 镜头 {{ lint.shot_count }} · 台词 {{ lint.dialogue_chars }} 字
          </div>
          <div v-if="lintBlock || lint?.blocking" class="lint-line bad">
            {{ lintBlock || lintItems.map((f) => f.message).join("；") }}
            <button class="fix" type="button" :disabled="busy" @click="generate(true)">改好后强制生成</button>
          </div>

          <!-- 生成参数条（吸底：长提示词滚动时操作始终可达） -->
          <div class="gen-bar">
            <span class="sel num">{{ genParams }}</span>
            <span class="sel num">{{ detail.duration_s ? `${detail.duration_s}s` : "时长自适应" }}</span>
            <span class="hint">重生成不抢占当前采用版 · 单次上限 30s</span>
            <button class="btn primary sm" type="button" :disabled="busy || genBusy" @click="generate(false)">
              {{ genBusy ? "生成中…" : "生成" }}
            </button>
          </div>

          <!-- 迭代版本 -->
          <div v-if="takes.length" class="vers">
            <div class="cap" style="padding: var(--sp-4) 0 var(--sp-1)">迭代版本 · 当前采用不因重生成而改变</div>
            <div class="ver-row">
              <div v-for="t in takes" :key="t.take_id" class="ver-card" :class="{ cur: t.take_id === detail.current_take_id }">
                <div class="im" :style="t.take_id === detail.current_take_id && detail.thumbnail ? { backgroundImage: `url(${mediaUrl(detail.thumbnail)})` } : {}">
                  <span class="badge" :class="t.take_id === detail.current_take_id ? 'ok' : 'mute'">
                    {{ t.take_id === detail.current_take_id ? "采用中" : "历史" }}
                  </span>
                </div>
                <div class="tm num">{{ String(t.take_id || "").slice(0, 12) }}<template v-if="t.duration_s"> · {{ t.duration_s }}s</template></div>
                <button
                  v-if="t.take_id !== detail.current_take_id"
                  class="adopt"
                  type="button"
                  @click="selectTake(detail.node_id, t.take_id).then(() => toast('已采用 · 成片将使用此版本'))"
                >
                  采用此版
                </button>
              </div>
            </div>
          </div>
        </div>
        <div v-else class="prompt-pane">
          <div class="ui-empty">
            <span class="ico">✍️</span>
            <span class="tt">选择左侧片段查看提示词</span>
            <span class="ds">每个片段一条结构化提示词——人物、场景、镜头、台词分字段可改，AI 修改需你确认</span>
          </div>
        </div>
      </div>

      <!-- ── 视频预览 ── -->
      <div class="col">
        <div class="col-cap cap">视频预览 <span class="grow"></span><span style="letter-spacing: 0">当前采用版</span></div>
        <div class="video-pane">
          <div v-if="detail?.video_path" class="vplayer">
            <video :src="mediaUrl(detail.video_path)" controls playsinline></video>
            <span v-if="detail.lint?.dialogue?.length" class="cc">{{ detail.lint.dialogue[0].text || detail.lint.dialogue[0].line }}</span>
          </div>
          <div v-else class="vplayer empty-v">还没有出片</div>

          <div class="v-acts">
            <a v-if="detail?.video_path" class="btn sm" :href="mediaUrl(detail.video_path)" download>下载</a>
            <button class="btn sm" type="button" :disabled="busy || genBusy" @click="generate(false)">再次生成</button>
          </div>

          <div>
            <div class="cap" style="padding-bottom: 8px">标记本版问题 · 下版本规避</div>
            <div class="mk-chips">
              <button
                v-for="t in TAGS"
                :key="t"
                class="mk-chip"
                :class="{ on: tags.includes(t) }"
                type="button"
                @click="toggleTag(t)"
              >
                {{ t }}
              </button>
            </div>
          </div>

          <div>
            <div class="cap" style="padding-bottom: 8px">
              首帧 · {{ detail?.keyframe_stale ? "过期——分镜/形象已变更" : detail?.keyframe?.path ? "与分镜一致" : "还没有首帧" }}
            </div>
            <div class="kf-box" :class="{ stale: detail?.keyframe_stale }">
              <img v-if="detail?.keyframe?.path" :src="mediaUrl(detail.keyframe.path)" alt="首帧" />
            </div>
            <button class="btn sm" style="margin-top: 8px" type="button" :disabled="busy" @click="redoKeyframe">
              {{ detail?.keyframe?.path ? "重出首帧" : "生成首帧" }}
            </button>
          </div>

          <div v-if="detail" class="meta num">{{ detail.segment_no }} · {{ detail.status }} · {{ studio.project?.video_model || "mock" }} · {{ detail.duration_s ?? "—" }}s</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.prod { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.prod, .prod * { box-sizing: border-box; }

.prod-top {
  display: flex; align-items: center; flex-wrap: wrap; gap: var(--sp-2) var(--sp-4);
  padding: 6px var(--sp-5); min-height: 46px; border-bottom: 1px solid var(--line); flex: none;
}
.ep-sel { font-size: var(--fs-13); color: var(--text-2); padding: 7px 12px; border-radius: var(--r-1); background: var(--card); border: 1px solid var(--line); }
.ep-sel option { background: var(--panel); }
.count { font-size: var(--fs-12); color: var(--text-2); }
.prod-top .bar { width: 110px; height: 2px; background: var(--card-3); }
.prod-top .bar i { display: block; height: 100%; background: var(--ok); }
.notice { color: var(--warn); font-size: var(--fs-12); max-width: 40%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.grow { flex: 1; }

/* 四列工作台：给每列下限，窄视口宁可让 1fr 到下限也不压成 1px（断点见样式表末尾） */
.prod4 { display: grid; grid-template-columns: minmax(150px, 190px) minmax(200px, 236px) minmax(340px, 1fr) minmax(270px, 330px); flex: 1; overflow: hidden; min-height: 0; }
.col { overflow-y: auto; display: flex; flex-direction: column; min-height: 0; }
.col + .col { border-left: 1px solid var(--line); }
.col-cap { padding: var(--sp-3) var(--sp-4); position: sticky; top: 0; background: var(--bg); z-index: 2; display: flex; align-items: baseline; gap: 8px; }
.col-cap .t { font-family: var(--serif); font-size: var(--fs-14); font-weight: 600; }
.summary { margin: 0 var(--sp-3) var(--sp-3); padding: var(--sp-4); border: 1px solid var(--line); border-radius: var(--r-2); background: var(--card); display: flex; flex-direction: column; gap: 10px; font-size: var(--fs-12); color: var(--text-3); line-height: 1.8; }
.summary .s-title { color: var(--text); font-weight: 600; font-family: var(--serif); font-size: var(--fs-16); line-height: 1.4; }
.summary .dim { color: var(--text-3); opacity: .75; font-size: var(--fs-11); }
.seg-empty { padding: 48px 16px; }
.seg-empty .act { width: 100%; }
.seg-empty .act .ui-btn { width: 100%; }

.st { display: inline-flex; align-items: center; gap: 6px; font-size: var(--fs-11); }
.st i { width: 6px; height: 6px; border-radius: 50%; flex: none; }
.st.ok { color: var(--ok); } .st.ok i { background: var(--ok); }
.st.warn { color: var(--warn); } .st.warn i { background: var(--warn); }
.st.mute { color: var(--text-3); } .st.mute i { background: var(--text-3); }

/* 队列 */
.seg-scroll { flex: 1; overflow-y: auto; }
.seg-item { display: flex; align-items: baseline; gap: 10px; padding: 10px var(--sp-4); cursor: pointer; border-left: 1px solid transparent; color: var(--text-3); font-size: var(--fs-13); }
.seg-item:hover { background: var(--card); color: var(--text-2); }
.seg-item.cur { background: var(--card); border-left-color: var(--accent); color: var(--text); }
.seg-item .no { font-family: var(--mono); font-size: var(--fs-11); width: 32px; flex: none; }
.seg-item .dur { margin-left: auto; font-family: var(--mono); font-size: var(--fs-11); color: var(--text-3); }
.empty { padding: var(--sp-4); color: var(--text-3); font-size: var(--fs-12); line-height: 1.8; }
.side-acts { padding: var(--sp-2) var(--sp-3) var(--sp-3); display: flex; flex-direction: column; gap: var(--sp-1); }
.side-act { text-align: left; padding: 11px 12px; border-radius: var(--r-1); font-size: var(--fs-13); color: var(--text-2); border: 1px solid var(--line); background: none; cursor: pointer; }
.side-act:hover { border-color: var(--line-3); color: var(--text); }
.side-act.primary { background: var(--accent); border-color: var(--accent); color: #FFF7F0; text-align: center; }
.side-act.primary:hover { background: var(--accent-hi); }
.side-act:disabled { opacity: .5; pointer-events: none; }

/* 提示词编辑器 */
.main-col { overflow-y: auto; }
.prompt-pane { padding: var(--sp-2) var(--sp-5) 96px; max-width: 820px; }
.edit-hint { font-size: var(--fs-11); color: var(--text-3); margin-bottom: var(--sp-3); }
.edit-hint b { color: var(--gold); font-weight: 500; }
.prompt-head { font-family: var(--mono); font-size: var(--fs-11); color: var(--text-3); padding-bottom: 6px; }
.sec { padding: 7px 0; border-bottom: 1px solid var(--line); display: flex; gap: 12px; align-items: baseline; position: relative; font-size: var(--fs-13); line-height: 2.05; color: var(--text-2); }
.sec:hover { background: rgba(255, 251, 243, .015); }
.sec .tag-k { color: var(--text-3); flex: none; width: 58px; font-size: var(--fs-12); letter-spacing: .06em; }
.sec .v { flex: 1; white-space: pre-wrap; }
.sec .edit-enter, .sec .ai-fix { position: absolute; right: 0; top: 9px; opacity: 0; font-size: var(--fs-11); color: var(--text-3); background: none; border: none; cursor: pointer; }
.sec .edit-enter { right: 58px; }
.sec:hover .edit-enter, .sec:hover .ai-fix { opacity: 1; }
.sec .ai-fix:hover { color: var(--gold); }
.sec .edit-enter:hover { color: var(--accent-hi); }
.sec-edit { flex: 1; background: var(--card); border: 1px solid rgba(201, 85, 61, .4); border-radius: var(--r-1); color: var(--text); font: inherit; font-size: var(--fs-13); padding: 6px 10px; resize: vertical; min-height: 54px; }
.sec-edit:focus { outline: none; }
.sec-edit.raw { width: 100%; min-height: 260px; line-height: 2; }
.sec-acts { position: absolute; right: 0; top: -24px; display: flex; gap: 8px; }
.sec-acts .ok-b { color: var(--ok); font-size: var(--fs-12); background: none; border: none; cursor: pointer; }
.sec-acts .no-b { color: var(--text-3); font-size: var(--fs-12); background: none; border: none; cursor: pointer; }
.raw-acts { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
.raw-acts .tip { color: var(--text-3); font-size: var(--fs-11); }

.mode-sw { display: flex; border: 1px solid var(--line-2); border-radius: var(--r-1); overflow: hidden; }
.mode-sw button { padding: 6px 14px; font-size: var(--fs-12); color: var(--text-3); background: none; border: none; cursor: pointer; }
.mode-sw button.on { background: var(--card-3); color: var(--text); }

.shot-tbl { margin-top: var(--sp-4); }
.shot-row { display: flex; align-items: center; gap: 10px; padding: 8px 0; border-top: 1px solid var(--line); font-size: var(--fs-12); color: var(--text-2); }
.shot-row .sn { font-family: var(--mono); color: var(--text-3); width: 22px; }
.shot-row .pill { border: 1px solid var(--line); border-radius: var(--r-1); padding: 3px 10px; color: var(--text-2); font-size: var(--fs-12); }
.shot-row .snd { flex: 1; color: var(--text-3); }

.lint-line { display: flex; align-items: center; gap: 7px; font-size: var(--fs-12); color: var(--ok); margin-top: var(--sp-3); }
.lint-line::before { content: "✓"; font-size: var(--fs-11); }
.lint-line.bad { color: var(--bad); }
.lint-line.bad::before { content: "✕"; }
.lint-line .fix { margin-left: auto; color: var(--warn); background: none; border: 1px solid var(--line-2); border-radius: var(--r-1); padding: 4px 12px; font-size: var(--fs-11); cursor: pointer; }
.lint-line .fix:hover { border-color: var(--warn); }

.gen-bar {
  display: flex; align-items: center; gap: var(--sp-2); margin-top: var(--sp-4);
  position: sticky; bottom: 0; padding: var(--sp-3) 0;
  background: linear-gradient(to top, var(--bg) 75%, transparent);
}
.gen-bar .sel { padding: 7px 14px; border: 1px solid var(--line); border-radius: var(--r-1); font-size: var(--fs-13); color: var(--text-2); font-family: var(--mono); }
.gen-bar .hint { margin-left: auto; color: var(--text-3); font-size: var(--fs-11); }

.vers .ver-row { display: flex; gap: var(--sp-3); margin-top: var(--sp-2); flex-wrap: wrap; }
.ver-card { width: 108px; }
.ver-card .im { aspect-ratio: 9/16; border-radius: var(--r-1); border: 1px solid var(--line); background: linear-gradient(165deg, #23201B, #14110E); position: relative; background-size: cover; background-position: center; }
.ver-card.cur .im { border-color: var(--ok); }
.ver-card .tm { font-size: 10px; color: var(--text-3); font-family: var(--mono); margin-top: 5px; }
.badge { position: absolute; top: 5px; left: 5px; font-size: 9px; padding: 0 6px; border-radius: 3px; }
.badge.ok { background: var(--ok-soft); color: var(--ok); }
.badge.mute { background: rgba(255, 251, 243, .08); color: var(--text-3); }
.adopt { display: block; margin-top: 4px; width: 100%; padding: 5px 0; border: 1px solid var(--line-2); border-radius: var(--r-1); font-size: 11px; color: var(--text-2); background: none; cursor: pointer; }
.adopt:hover { border-color: var(--ok); color: var(--ok); }

/* 右栏预览（底部留出 AI 浮球空间） */
.video-pane { padding: var(--sp-4) var(--sp-4) 96px; display: flex; flex-direction: column; gap: var(--sp-5); }
.video-pane .cap { padding-bottom: 8px; }
.vplayer { aspect-ratio: 9/16; max-height: 380px; margin: 0 auto; border-radius: var(--r-2); background: linear-gradient(165deg, #23201B, #14110E); position: relative; border: 1px solid var(--line); overflow: hidden; display: grid; place-items: center; }
.vplayer video { width: 100%; height: 100%; object-fit: contain; }
.vplayer.empty-v { color: var(--text-3); font-size: var(--fs-12); }
.vplayer .cc { position: absolute; bottom: 10px; left: 12px; right: 12px; text-align: center; font-family: var(--serif); font-size: var(--fs-13); color: #F2EDE4; text-shadow: 0 1px 6px rgba(0, 0, 0, .9); pointer-events: none; }
.v-acts { display: flex; gap: var(--sp-2); }
.v-acts .btn { flex: 1; text-decoration: none; }
.mk-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.mk-chip { font-size: var(--fs-12); padding: 5px 14px; border-radius: 999px; border: 1px solid var(--line-2); color: var(--text-3); background: none; cursor: pointer; }
.mk-chip.on { border-color: var(--warn); color: var(--warn); background: var(--warn-soft); }
.kf-box { width: 100%; aspect-ratio: 9/16; max-height: 170px; border-radius: var(--r-1); border: 1px solid rgba(111, 162, 132, .4); background: linear-gradient(165deg, #1B2220, #0F1412); overflow: hidden; }
.kf-box.stale { border-color: rgba(201, 138, 75, .55); }
.kf-box img { width: 100%; height: 100%; object-fit: cover; }
.video-pane .meta { font-size: var(--fs-11); color: var(--text-3); font-family: var(--mono); }

.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; padding: 9px 20px; border-radius: var(--r-1); font-size: var(--fs-14); color: var(--text-2); cursor: pointer; background: none; border: none; }
.btn:hover { color: var(--text); background: var(--card-2); }
.btn.primary { background: var(--accent); color: #FFF7F0; }
.btn.primary:hover { background: var(--accent-hi); }
.btn.sm { padding: 6px 14px; font-size: var(--fs-13); }
.btn:disabled { opacity: .5; pointer-events: none; }

/* 断点降级（必须在基础规则之后：同特异性时靠源码顺序取胜）：
 * ≤1200 撤概要列（信息顶栏/项目信息弹窗都有），≤940 单列纵向滚动。 */
@media (max-width: 1200px) {
  .prod4 { grid-template-columns: minmax(200px, 236px) minmax(400px, 1fr) minmax(260px, 300px); }
  .col-overview { display: none; }
}
@media (max-width: 940px) {
  .prod4 { display: flex; flex-direction: column; overflow-y: auto; }
  .col { overflow: visible; flex: none; }
  .col + .col { border-left: none; border-top: 1px solid var(--line); }
  .seg-scroll { overflow-y: auto; max-height: 320px; }
}
</style>
