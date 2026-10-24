<script setup>
/* 剧本页（demo v4 屏1）：集列表 + 宋体正文 + 停靠 AI。
 *
 * 数据：studioStore.loadAll（project/script/segments 一次到位），集切换只重拉
 * script + segments（都按 ?episode 收窄）。编辑保存走 importScript（重新分集
 * 即保存语义，服务端会告警异常集）；「开始制作」复用 produceEpisode。
 * 管线剧本（阶段 1 Task 5）：script 导入产物在手时，集列表/正文/保存整体切到
 * 产物路径（正文取产物逐集 text、保存逐集落库），画布路径保留为 fallback。
 */
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessageBox } from "element-plus";
import AiPanel from "../ai/AiPanel.vue";
import { errorText, getJSON } from "../api.js";
import { hasPipelineScript, importScript, loadAll, loadPipeline, loadProject, loadScript, loadSegments, pollProduceJob, startProduce, studio } from "../studio/studioStore.js";
import StoryAnalysisCards from "./script/StoryAnalysisCards.vue";
import AssetTablesPanel from "./script/AssetTablesPanel.vue";
import PipelineOutlinePanel from "./script/PipelineOutlinePanel.vue";

const route = useRoute();
const router = useRouter();

const editing = ref(false);
const editText = ref("");
/* 中列 tab：剧本正文 / 项目设定（四件套）/ 资产表（草稿区）/ 管线大纲（只读，导入产物才有） */
const tab = ref("script");
async function switchTab(next) {
  tab.value = next;
  if (next !== "script" && !studio.storyAnalysis) await loadProject(); // 产物随 project 下发
}
const busy = ref(false);
const notice = ref("");
const produceOpen = ref(false);
const produceInstruction = ref("");
const aiPanel = ref(null); // 制作跟单：attachProduce 挂到右栏 AI 助手

const script = computed(() => studio.script);
/* 管线剧本数据源（Task 5）：产物详情在手且认得出集列表才走管线，否则画布 fallback */
const pipelineDetail = computed(() => studio.pipeline?.detail?.script || null);
const pipelineEpisodes = computed(() => {
  const list = pipelineDetail.value?.episodes;
  return Array.isArray(list) ? list : [];
});
const usePipelineScript = computed(() => hasPipelineScript.value && pipelineEpisodes.value.length > 0);
const localEp = ref(null); // 管线路径的当前集号：不写画布 project，切集纯前端
const episodes = computed(() => {
  if (usePipelineScript.value) {
    // 左栏行沿用画布路径形状（no/title/chars）；hook 挂条目上做悬浮提示
    return pipelineEpisodes.value.map((e) => ({
      no: e.ep,
      title: `第${e.ep}集`,
      chars: e.chars || 0,
      hook: e.hook || "",
    }));
  }
  return script.value?.episodes || [];
});
const currentEp = computed(() => {
  if (usePipelineScript.value) return localEp.value || pipelineEpisodes.value[0]?.ep || 1;
  return Number(studio.project?.episode || episodes.value[0]?.no || 1);
});
const currentPipelineEp = computed(() =>
  usePipelineScript.value ? pipelineEpisodes.value.find((e) => e.ep === currentEp.value) || null : null,
);
const segTotal = computed(() => studio.segments.length);
const segDone = computed(() => studio.segments.filter((s) => s.status === "done").length);
const assetsAll = computed(() => {
  const a = script.value?.assets || {};
  return (a.character || 0) + (a.scene || 0) + (a.prop || 0);
});

const producing = computed(() => ["queued", "running"].includes(studio.produceJob?.status));

/* 管线大纲 tab 开关：导入了 outline 产物才出现，画布老项目零变化 */
const hasPipelineOutline = computed(() => Boolean(studio.pipeline?.detail?.outline));
const producePct = computed(() => {
  const p = studio.produceJob?.progress;
  return producing.value && p?.total ? p.percent : null;
});

/* 正文文本：管线路径取当前集产物 text（后端已按 flow 派生 / 带本地改稿），
 * 画布路径取画布剧本全文——行格式一致，下面的行解析器两路共用 */
const scriptText = computed(() => {
  if (currentPipelineEp.value) return currentPipelineEp.value.text || "";
  return script.value?.script || "";
});

/* 标题栏统计：画布路径 全文字数/段数/对白数；管线路径只有当前集正文字数 */
const headMeta = computed(() => {
  if (usePipelineScript.value) return `${currentPipelineEp.value?.chars || 0} 字 · 管线产物`;
  return `${script.value?.chars || 0} 字 · ${segTotal.value} 段 · ${script.value?.dialogue_count || 0} 句对白`;
});

/* 正文渲染：五类行——场景 / 引导块 / 分隔线 / 动作行 / 对白。
 * AI 剧本里常见的 markdown 痕迹（*强调*、> 引用、--- 分隔）不再裸奔；
 * 对白行说话人加粗、台词鎏金，动作行弱化——这页最有价值的视觉资产。 */
const paragraphs = computed(() =>
  String(scriptText.value || "")
    .split(/\n+/)
    .map((raw) => raw.trim())
    .filter(Boolean)
    .map((line, index) => {
      if (/^(-{3,}|\*{3,}|_{3,})$/.test(line)) return { kind: "hr", index };
      if (/^#{1,4}\s*/.test(line) || /^第[一二三四五六七八九十0-9]+[场幕]/.test(line))
        return { kind: "scene", index, text: line.replace(/^#{1,4}\s*/, "") };
      if (/^>\s?/.test(line)) return { kind: "quote", index, text: line.replace(/^>\s?/, "") };
      // 说话人独立行：**教授**（看了一圈）——markdown 加粗的说话人不再裸奔
      let m = line.match(/^\*\*([^\s：:*]{1,12})\*\*\s*(（[^）]*）)?\s*$/);
      if (m) return { kind: "speaker", index, speaker: m[1], aside: (m[2] || "").replace(/[（）]/g, "") };
      // **说话人**：台词
      m = line.match(/^\*\*([^\s：:*]{1,12})\*\*\s*[：:]\s*([\s\S]+)$/);
      if (m) return { kind: "line", index, speaker: m[1], text: m[2].replace(/[」”]\s*$/, "") };
      const act = line.match(/^\*([^*].*)\*$/);
      if (act) return { kind: "action", index, text: act[1].replace(/\*\*([^*]+)\*\*/g, "$1").trim() };
      const d = line.match(/^([^\s：:*]{1,10})[：:]\s*[「“]?([\s\S]+)[」”]?$/);
      if (d) return { kind: "line", index, speaker: d[1], text: d[2].replace(/^\*+/, "").replace(/\*+$/, "") };
      return { kind: "p", index, text: line.replace(/\*\*([^*]+)\*\*/g, "$1").replace(/^\*([^*]+)\*$/g, "$1") };
    }),
);

/* 场景跳转轨（DeepSeek 式大纲）：点场景名平滑跳转，滚动时反向高亮当前场 */
const scenes = computed(() => paragraphs.value.filter((p) => p.kind === "scene"));
const sceneLoc = (text) => text.replace(/^场景\s*\d*\s*[｜|]?\s*/, "").trim() || text;
const activeScene = ref(-1);
const scrollEl = ref(null);
let railLockUntil = 0;
/* 轨显隐看实际中栏宽度（拖拽列宽后媒体查询就不够准了）：700 正文 + 左侧 190 轨位 */
const winW = ref(window.innerWidth);
const railOk = computed(() => winW.value - leftW.value - rightW.value - 24 >= 950);
function jumpScene(p) {
  const el = document.getElementById(`scene-${p.index}`);
  if (!el) return;
  const i = scenes.value.indexOf(p);
  if (i >= 0) activeScene.value = i; // 立即高亮，不等平滑滚动触发 scroll
  railLockUntil = Date.now() + 900; // 平滑滚动期间不让 scroll 抢高亮
  el.scrollIntoView({ behavior: "smooth", block: "start" });
}
function onBodyScroll() {
  const root = scrollEl.value;
  if (!root || Date.now() < railLockUntil) return;
  const rootTop = root.getBoundingClientRect().top;
  let cur = -1;
  scenes.value.forEach((s, i) => {
    const el = document.getElementById(`scene-${s.index}`);
    if (el && el.getBoundingClientRect().top - rootTop <= 110) cur = i;
  });
  activeScene.value = cur;
}

/* 三列宽度可拖：左（集列表）/右（AI 面板）记住宽度，中间 1fr 归正文 */
const COL_KEY = "df.script.cols";
const savedCols = (() => {
  try {
    return JSON.parse(localStorage.getItem(COL_KEY) || "{}");
  } catch {
    return {};
  }
})();
const leftW = ref(Number(savedCols.left) || 232);
const rightW = ref(Number(savedCols.right) || 340);
let dragCol = "";
function startColDrag(which) {
  dragCol = which;
  document.body.style.cursor = "col-resize";
  document.body.style.userSelect = "none";
  window.addEventListener("pointermove", onColDrag);
  window.addEventListener("pointerup", endColDrag, { once: true });
}
function onColDrag(e) {
  if (dragCol === "left") leftW.value = Math.min(420, Math.max(180, e.clientX));
  if (dragCol === "right") rightW.value = Math.min(620, Math.max(300, window.innerWidth - e.clientX));
}
function endColDrag() {
  window.removeEventListener("pointermove", onColDrag);
  document.body.style.cursor = "";
  document.body.style.userSelect = "";
  if (dragCol) localStorage.setItem(COL_KEY, JSON.stringify({ left: leftW.value, right: rightW.value }));
  dragCol = "";
}
onBeforeUnmount(() => {
  window.removeEventListener("pointermove", onColDrag);
  document.body.style.cursor = "";
  document.body.style.userSelect = "";
});

/* 窄屏（≤1000px）：放弃三列，正文全宽 + AI 面板落底部一行 */
const narrow = ref(false);
let narrowMq = null;
function onNarrowChange(e) {
  narrow.value = e.matches;
}
onMounted(() => {
  narrowMq = window.matchMedia("(max-width: 1000px)");
  narrow.value = narrowMq.matches;
  narrowMq.addEventListener("change", onNarrowChange);
  window.addEventListener("resize", onResize);
});
const onResize = () => (winW.value = window.innerWidth);
onBeforeUnmount(() => {
  narrowMq?.removeEventListener("change", onNarrowChange);
  window.removeEventListener("resize", onResize);
});

function pickEpisode(no) {
  if (Number(no) === currentEp.value) return;
  if (usePipelineScript.value) {
    // 管线路径：产物详情已在手，切集纯前端换正文（复位与画布路径切集后一致）
    localEp.value = Number(no);
    notice.value = "";
    editing.value = false;
    if (scrollEl.value) scrollEl.value.scrollTop = 0;
    activeScene.value = -1;
    return;
  }
  studio.project.episode = Number(no);
  busy.value = true;
  Promise.all([loadScript(), loadSegments()])
    .then(() => {
      notice.value = "";
      editing.value = false;
      if (scrollEl.value) scrollEl.value.scrollTop = 0;
      activeScene.value = -1;
    })
    .catch((err) => (notice.value = errorText(err)))
    .finally(() => (busy.value = false));
}

function startEdit() {
  editText.value = scriptText.value || "";
  editing.value = true;
}

/* 编辑安全：改过没保存就不许静默丢弃 */
const editDirty = computed(() => editing.value && editText.value !== (scriptText.value || ""));
async function cancelEdit() {
  if (editDirty.value) {
    try {
      await ElMessageBox.confirm("正文已经改过但还没保存，放弃这些修改？", "未保存的修改", {
        type: "warning",
        confirmButtonText: "放弃修改",
        cancelButtonText: "继续编辑",
      });
    } catch {
      return; // 用户选择继续编辑
    }
  }
  editing.value = false;
}
function onEditKey(e) {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    if (!busy.value && editText.value.trim()) saveViaResplit();
  }
}

async function saveViaResplit() {
  if (!editText.value.trim()) return;
  if (usePipelineScript.value) return savePipelineEpisodeText();
  busy.value = true;
  notice.value = "";
  try {
    const data = await importScript(editText.value, true);
    const warnings = (data.warnings || episodes.value.map((e) => e.warning).filter(Boolean)) || [];
    notice.value = warnings.length ? `已重新分集：${warnings.join("；")}` : "已保存并重新分集";
    editing.value = false;
  } catch (err) {
    notice.value = errorText(err);
  } finally {
    busy.value = false;
  }
}

/* 管线路径保存：只落当前集（PUT edited_text），不再"整本重新分集" */
async function savePipelineEpisodeText() {
  busy.value = true;
  notice.value = "";
  try {
    await savePipelineEpisode(currentEp.value, editText.value);
    notice.value = `第 ${currentEp.value} 集已保存（管线剧本）`;
    editing.value = false;
  } catch (err) {
    notice.value = errorText(err);
  } finally {
    busy.value = false;
  }
}

async function doProduce() {
  produceOpen.value = false;
  busy.value = true;
  notice.value = "";
  try {
    const job = await startProduce(currentEp.value, produceInstruction.value.trim());
    // run_id 在后台任务里异步登记：短轮询拿到后让 AI 助手跟单（事件流从头补放，不丢过程）
    let run = null;
    for (let i = 0; i < 24 && !run; i++) {
      await new Promise((r) => setTimeout(r, 500));
      try {
        const j = await getJSON(`/api/studio/jobs/${job.job_id}`);
        if (j.run_id && j.session_id) run = j;
        else if (!["queued", "running"].includes(j.status)) break; // 已经终态就没得跟了
      } catch { /* 任务中心尚未登记，下一轮再试 */ }
    }
    if (run?.session_id && run?.run_id) {
      aiPanel.value?.attachProduce(run.session_id, run.run_id);
    }
    const final = await pollProduceJob(job.job_id);
    if (final.status === "succeeded") {
      notice.value = `第 ${currentEp.value} 集制作完成，共 ${segTotal.value} 段`;
    } else if (final.status === "limited") {
      notice.value = `第 ${currentEp.value} 集制作暂停：已完成的片段已保留，可再次「开始制作」继续`;
    } else {
      notice.value = `制作失败：${final.error || final.status}`;
    }
    produceInstruction.value = "";
  } catch (err) {
    notice.value = errorText(err);
  } finally {
    busy.value = false;
  }
}

async function refresh() {
  const name = String(route.params.name || "");
  if (name && studio.canvas !== name) studio.canvas = name;
  busy.value = true;
  try {
    // loadPipeline 内部吞掉一切错误（无产物/无 DB/断网只记 console），不会拖垮 loadAll
    await Promise.all([loadAll(), loadPipeline()]);
  } finally {
    busy.value = false;
  }
}

onMounted(refresh);
</script>

<template>
  <div
    class="script-lay"
    :class="{ narrow }"
    :style="narrow ? null : { gridTemplateColumns: `${leftW}px 6px minmax(0,1fr) 6px ${rightW}px` }"
  >
    <!-- ── 左：集列表 + 制作准备 ── -->
    <aside class="ep-side">
      <div class="side-cap cap">剧本列表</div>
      <div class="ep-scroll">
        <div
          v-for="e in episodes"
          :key="e.no"
          class="ep-item"
          :class="{ cur: Number(e.no) === currentEp }"
          :title="e.hook || ''"
          @click="pickEpisode(e.no)"
        >
          <span class="no">{{ e.title || `第${e.no}集` }}</span>
          <span class="t">{{ e.chars || 0 }} 字</span>
          <span class="stx num" :title="e.warning || ''">{{ e.warning ? "⚠" : e.segment_total ? `${e.segment_done}/${e.segment_total}` : "" }}</span>
        </div>
        <div v-if="!episodes.length" class="ep-empty">还没有剧本——点右下「编辑剧本」粘贴全文</div>
      </div>
      <div class="flow">
        <div class="cap" style="padding-bottom: 8px">制作准备</div>
        <div class="it" :class="(script?.script || usePipelineScript) ? 'done' : 'todo'"><span class="n">{{ (script?.script || usePipelineScript) ? "✓" : "1" }}</span>导入剧本 · 自动分集</div>
        <div class="it" :class="segTotal ? 'done' : 'todo'"><span class="n">{{ segTotal ? "✓" : "2" }}</span>Agent 制作 · 分镜与提示词</div>
        <div class="it" :class="segTotal ? 'done' : 'todo'"><span class="n">{{ segTotal ? "✓" : "3" }}</span>资产就绪 {{ assetsAll }} 项</div>
        <div class="it todo" style="cursor: pointer" @click="router.push(`/project/${route.params.name}/production`)">
          <span class="n num">4</span>补齐首帧 → 批量出片
        </div>
      </div>
    </aside>

    <!-- 列宽拖拽把手（左） -->
    <span class="drag-col" aria-hidden="true" @pointerdown.prevent="startColDrag('left')"></span>

    <!-- ── 中：正文 ── -->
    <div class="script-main">
      <div class="script-head">
        <div class="doc-tabs">
          <button class="dt" :class="{ on: tab === 'script' }" type="button" @click="switchTab('script')">剧本</button>
          <span class="dt-sep" aria-hidden="true"></span>
          <span class="dt-group">项目</span>
          <button class="dt" :class="{ on: tab === 'analysis' }" type="button" @click="switchTab('analysis')">
            项目设定<span v-if="studio.analysis.running" class="dt-spin" title="正在解析"></span>
          </button>
          <button class="dt" :class="{ on: tab === 'tables' }" type="button" @click="switchTab('tables')">
            资产表<span v-if="studio.parsing.running" class="dt-spin" title="正在解析资产表"></span>
          </button>
          <button v-if="hasPipelineOutline" class="dt" :class="{ on: tab === 'pipeline' }" type="button" @click="switchTab('pipeline')">管线大纲</button>
        </div>
        <template v-if="tab === 'script'">
          <h2>{{ episodes.find((e) => Number(e.no) === currentEp)?.title || `第 ${currentEp} 集` }}</h2>
          <div class="grow"></div>
          <span class="vm num">{{ headMeta }}</span>
        </template>
      </div>

      <div ref="scrollEl" class="script-scroll" @scroll="onBodyScroll">
        <StoryAnalysisCards v-if="tab === 'analysis'" />
        <AssetTablesPanel v-else-if="tab === 'tables'" />
        <PipelineOutlinePanel v-else-if="tab === 'pipeline'" :outline="studio.pipeline?.detail?.outline?.content" />
        <template v-else>
          <div class="script-inner">
            <!-- 场景跳转轨：点场景名直达，滚动时反向高亮当前场（中栏不够宽时藏） -->
            <aside v-if="scenes.length > 1 && !editing && railOk" class="scene-rail">
              <div class="sr-stick">
                <div class="sr-cap">场景大纲</div>
                <button
                  v-for="(s, i) in scenes"
                  :key="s.index"
                  class="sr-it"
                  :class="{ on: i === activeScene }"
                  type="button"
                  @click="jumpScene(s)"
                >
                  <span class="n num">{{ i + 1 }}</span>
                  <span class="loc">{{ sceneLoc(s.text) }}</span>
                </button>
              </div>
            </aside>
            <div v-if="editing" class="script-doc editing">
              <textarea v-model="editText" class="edit-area" spellcheck="false" @keydown="onEditKey"></textarea>
              <div class="edit-tip">
                <span :class="{ warn: editDirty }">{{ editText.length }} 字</span>
                · {{ usePipelineScript ? "Ctrl+S 保存 = 只保存当前集（管线剧本）" : "Ctrl+S 保存 = 按新全文重新分集（单集异常会告警）" }}
              </div>
            </div>
            <article v-else class="script-doc">
              <template v-for="(p, i) in paragraphs" :key="i">
                <div v-if="p.kind === 'hr'" class="rule" aria-hidden="true"></div>
                <div v-else-if="p.kind === 'scene'" :id="`scene-${p.index}`" class="scene">{{ p.text }}</div>
                <blockquote v-else-if="p.kind === 'quote'" class="quote">{{ p.text }}</blockquote>
                <div v-else-if="p.kind === 'action'" class="action">{{ p.text }}</div>
                <div v-else-if="p.kind === 'speaker'" class="speaker">
                  <b>{{ p.speaker }}</b><span v-if="p.aside" class="aside">（{{ p.aside }}）</span>
                </div>
                <p v-else-if="p.kind === 'line'"><b>{{ p.speaker }}</b>：{{ p.text }}</p>
                <p v-else>{{ p.text }}</p>
              </template>
              <div v-if="!paragraphs.length" class="empty">这一集还没有正文</div>
            </article>
          </div>
        </template>
      </div>

      <div v-if="tab === 'script'" class="script-foot">
        <template v-if="editing">
          <button class="btn sm" :disabled="busy" @click="cancelEdit">取消</button>
          <button class="btn primary sm" :disabled="busy" @click="saveViaResplit">{{ usePipelineScript ? "保存本集" : "保存并重新分集" }}</button>
          <span v-if="editDirty" class="dirty-hint">有未保存的修改</span>
        </template>
        <template v-else>
          <button class="btn sm" @click="startEdit">编辑剧本</button>
          <button class="btn sm" :disabled="busy || producing || !script?.script" @click="produceOpen = true">
            {{ producing ? (producePct == null ? "Agent 制作中…" : `Agent 制作中… ${producePct}%`) : "开始制作" }}
          </button>
          <span v-if="notice" class="notice">{{ notice }}</span>
          <span style="flex: 1"></span>
          <button class="btn primary sm" @click="router.push(`/project/${route.params.name}/production`)">进入制作</button>
        </template>
      </div>
    </div>

    <!-- 列宽拖拽把手（右） -->
    <span class="drag-col" aria-hidden="true" @pointerdown.prevent="startColDrag('right')"></span>

    <!-- ── 右：停靠 AI ── -->
    <AiPanel
      ref="aiPanel"
      class="dock"
      mode="dock"
      assistant-mode="script_qa"
      :canvas="String(route.params.name)"
      :chips="['解析出资产表', '细化林小满的人物小传', '把世界观改得更黑暗', '分析本集对白节奏']"
      @canvas-changed="refresh"
    />

    <!-- ── 开始制作确认 ── -->
    <div v-if="produceOpen" class="mask" @click.self="produceOpen = false">
      <div class="box">
        <h3>让 Agent 开始制作 · 第 {{ currentEp }} 集</h3>
        <div class="box-body">
          ① 按「一条分镜 = 8-15 秒片段」拆开这一集<br />
          ② 提取角色 / 场景 / 道具，写好外观锚点与一致性标签<br />
          ③ 为每个片段写出带时间轴的视频提示词<br />
          ④ 建好生成槽位——<b>不生成任何画面，不花钱</b>
          <textarea v-model="produceInstruction" class="ins" placeholder="有额外要求可以在这里补一句（选填）"></textarea>
        </div>
        <div class="box-foot">
          <span class="tip">台词与时长由系统校验，不合格的片段会拦在生成之前</span>
          <button class="btn sm" @click="produceOpen = false">取消</button>
          <button class="btn primary sm" :disabled="busy" @click="doProduce">开始</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.doc-tabs { display: flex; gap: var(--sp-1); align-items: stretch; }
.dt { border: none; background: none; color: var(--text-3); font-size: var(--fs-14); font-family: var(--serif); letter-spacing: .05em; padding: 5px 14px; cursor: pointer; border-radius: var(--r-1); display: inline-flex; align-items: center; gap: 6px; }
.dt:hover { color: var(--text-2); }
.dt.on { color: var(--text); background: var(--card-2); }
/* 项目级 tab 与本集 tab 之间的分界 */
.dt-sep { width: 1px; background: var(--line-2); margin: 5px 8px; }
.dt-group { align-self: center; color: var(--text-3); font-size: 10px; letter-spacing: .14em; }
/* 解析运行徽标：跨 tab 可见（不用切过去才知道在跑） */
.dt-spin { width: 8px; height: 8px; border-radius: 50%; border: 1.5px solid rgba(194, 163, 107, .3); border-top-color: var(--gold); animation: dt-rot 0.9s linear infinite; }
@keyframes dt-rot { to { transform: rotate(360deg); } }
.script-lay { display: grid; grid-template-columns: 232px 6px minmax(0, 1fr) 6px 340px; height: 100%; min-height: 0; }
.script-lay, .script-lay * { box-sizing: border-box; }

/* 列宽拖拽把手：平时隐形，悬停露出金线，拖动改对应列宽 */
.drag-col { cursor: col-resize; background: transparent; transition: background 120ms; z-index: 5; }
.drag-col:hover, .drag-col:active { background: rgba(194, 163, 107, .28); }

/* 窄屏堆叠：集列表让位（制作准备流程在制作页还有），AI 面板落到正文下方 */
.script-lay.narrow { grid-template-columns: minmax(0, 1fr); grid-template-rows: minmax(0, 1fr) 340px; grid-template-areas: "main" "ai"; }
.script-lay.narrow .ep-side, .script-lay.narrow .drag-col { display: none; }
.script-lay.narrow .script-main { grid-area: main; }
.script-lay.narrow .dock { grid-area: ai; border-left: none; border-top: 1px solid var(--line); }

/* 左栏 */
.ep-side { background: var(--bg-2); border-right: 1px solid var(--line); display: flex; flex-direction: column; min-height: 0; }
.side-cap { padding: var(--sp-4) var(--sp-4) var(--sp-2); }
.ep-scroll { flex: 1; overflow-y: auto; }
.ep-item { display: flex; align-items: baseline; gap: 10px; padding: 11px var(--sp-4); cursor: pointer; border-left: 1px solid transparent; color: var(--text-3); font-size: var(--fs-13); }
.ep-item:hover { color: var(--text-2); background: var(--card); }
.ep-item.cur { background: var(--card); border-left-color: var(--accent); color: var(--text); }
.ep-item .no { font-size: var(--fs-11); color: var(--text-3); width: 52px; flex: none; }
.ep-item.cur .no { color: var(--text-2); }
.ep-item .t { flex: 1; font-size: var(--fs-11); color: var(--text-3); }
.ep-item .stx { font-size: var(--fs-11); color: var(--ok); }
.ep-empty { padding: var(--sp-4); color: var(--text-3); font-size: var(--fs-12); line-height: 1.8; }
.flow { border-top: 1px solid var(--line); padding: var(--sp-3) var(--sp-4) var(--sp-4); }
.flow .it { display: flex; align-items: center; gap: 9px; padding: 6px 0; font-size: var(--fs-12); color: var(--text-3); }
.flow .it.todo { color: var(--text-2); }
.flow .it .n { width: 15px; height: 15px; border-radius: 50%; display: grid; place-items: center; font-size: 9px; flex: none; }
.flow .it.done .n { background: var(--ok-soft); color: var(--ok); }
.flow .it.todo .n { border: 1px solid var(--line-3); color: var(--text-3); }

/* 中栏 */
.script-main { display: flex; flex-direction: column; overflow: hidden; min-height: 0; }
.script-head { display: flex; align-items: baseline; gap: var(--sp-4); padding: var(--sp-4) var(--sp-6); border-bottom: 1px solid var(--line); flex: none; }
.script-head h2 { font-family: var(--serif); font-size: var(--fs-16); font-weight: 600; }
.script-head .vm { font-size: var(--fs-11); color: var(--text-3); }
.script-scroll { flex: 1; overflow-y: auto; }
.script-inner { max-width: 700px; margin: 0 auto; position: relative; }
.script-doc { max-width: 100%; padding: var(--sp-6) var(--sp-6) 96px; font-family: var(--serif); font-size: 16px; line-height: 2.15; color: var(--text); }
.script-doc p { margin-bottom: var(--sp-3); text-align: justify; }
.script-doc p b { color: var(--gold); font-weight: 600; }
.script-doc .scene { color: var(--text-3); font-size: var(--fs-13); letter-spacing: .12em; margin: var(--sp-5) 0 var(--sp-3); }
.script-doc .scene::before { content: "◇ "; color: var(--accent); font-size: 10px; }
/* 动作行（舞台指示 / 画外音）：弱化、sans、略小——和对白拉开层次 */
.script-doc .action { color: var(--text-3); font-family: var(--sans); font-size: 14px; line-height: 1.9; margin-bottom: var(--sp-3); }
/* 说话人独立行：鎏金加粗 + 括号动作弱化，紧邻台词 */
.script-doc .speaker { margin: var(--sp-4) 0 var(--sp-1); }
.script-doc .speaker b { color: var(--gold); font-weight: 600; }
.script-doc .speaker .aside { color: var(--text-3); font-size: 14px; margin-left: 6px; }
/* 引导块（> 开头的制作说明）：左边线 + 弱色 */
.script-doc .quote { margin: 0 0 var(--sp-3); padding: 2px 0 2px 14px; border-left: 2px solid var(--line-2); color: var(--text-3); font-family: var(--sans); font-size: var(--fs-13); line-height: 1.8; }
/* 分隔线 */
.script-doc .rule { display: flex; align-items: center; margin: var(--sp-4) 0; }
.script-doc .rule::before, .script-doc .rule::after { content: ""; flex: 1; height: 1px; background: var(--line-2); }

/* 场景跳转轨（正文左缘，DeepSeek 式大纲；视口不够宽时藏） */
.scene-rail { position: absolute; left: -192px; top: var(--sp-6); bottom: 96px; width: 176px; }
.sr-stick { position: sticky; top: 12px; display: flex; flex-direction: column; gap: 1px; max-height: calc(100vh - 220px); overflow-y: auto; }
.sr-cap { font-size: 10px; letter-spacing: .1em; color: var(--text-3); padding: 0 0 6px 11px; }
.sr-it { display: flex; gap: 7px; align-items: baseline; text-align: left; border: none; background: none; color: var(--text-3); font-size: var(--fs-12); padding: 5px 10px; cursor: pointer; border-left: 2px solid var(--line); font-family: inherit; }
.sr-it:hover { color: var(--text-2); }
.sr-it.on { color: var(--gold); border-left-color: var(--accent); }
.sr-it .n { flex: none; font-size: 10px; opacity: .7; }
.sr-it .loc { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }

.script-doc .empty, .script-doc .edit-tip { font-family: var(--sans); color: var(--text-3); font-size: var(--fs-13); padding: var(--sp-5) 0; }
.edit-tip .warn { color: var(--warn); }
.dirty-hint { color: var(--warn); font-size: var(--fs-12); margin-left: var(--sp-2); }
.edit-area { width: 100%; min-height: 60vh; background: var(--card); border: 1px solid var(--line-2); border-radius: var(--r-2); color: var(--text); font-family: var(--serif); font-size: 15px; line-height: 2; padding: var(--sp-4); resize: vertical; }
.edit-area:focus { outline: none; border-color: rgba(201, 85, 61, .5); }

.script-foot { display: flex; gap: var(--sp-2); padding: var(--sp-3) var(--sp-6); border-top: 1px solid var(--line); flex: none; background: var(--bg-2); align-items: center; }
.notice { color: var(--warn); font-size: var(--fs-12); margin-left: var(--sp-3); max-width: 46%; }

.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; padding: 9px 20px; border-radius: var(--r-1); font-size: var(--fs-14); color: var(--text-2); cursor: pointer; background: none; border: none; }
.btn:hover { color: var(--text); background: var(--card-2); }
.btn.primary { background: var(--accent); color: #FFF7F0; }
.btn.primary:hover { background: var(--accent-hi); }
.btn.sm { padding: 6px 14px; font-size: var(--fs-13); }
.btn:disabled { opacity: .5; pointer-events: none; }

/* AI 停靠栏（AiPanel 自带内部样式，这里只定格子位置） */
.dock { border-left: 1px solid var(--line); min-height: 0; }

/* 制作确认弹窗 */
.mask { position: fixed; inset: 0; background: rgba(5, 4, 3, .7); z-index: 50; display: grid; place-items: center; }
.box { background: var(--panel); border: 1px solid var(--line-2); border-radius: var(--r-3); width: 620px; max-width: 94vw; padding: var(--sp-5); }
.box h3 { font-family: var(--serif); font-size: var(--fs-16); font-weight: 600; margin-bottom: var(--sp-4); }
.box-body { font-size: var(--fs-13); color: var(--text-2); line-height: 2.1; }
.box-body b { color: var(--gold); }
.ins { width: 100%; margin-top: var(--sp-4); background: var(--card); border: 1px solid var(--line); border-radius: var(--r-1); padding: 9px 12px; color: var(--text); font: inherit; font-size: var(--fs-13); min-height: 64px; resize: vertical; }
.ins:focus { outline: none; border-color: rgba(201, 85, 61, .5); }
.box-foot { display: flex; align-items: center; gap: var(--sp-2); margin-top: var(--sp-4); }
.box-foot .tip { margin-right: auto; color: var(--text-3); font-size: var(--fs-11); }
</style>
