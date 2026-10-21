<script setup>
/* 剧本页（demo v4 屏1）：集列表 + 宋体正文 + 停靠 AI。
 *
 * 数据：studioStore.loadAll（project/script/segments 一次到位），集切换只重拉
 * script + segments（都按 ?episode 收窄）。编辑保存走 importScript（重新分集
 * 即保存语义，服务端会告警异常集）；「开始制作」复用 produceEpisode。
 */
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import AiPanel from "../ai/AiPanel.vue";
import { errorText } from "../api.js";
import { importScript, loadAll, loadScript, loadSegments, produceEpisode, studio } from "../studio/studioStore.js";
import StoryAnalysisCards from "./script/StoryAnalysisCards.vue";
import AssetTablesPanel from "./script/AssetTablesPanel.vue";

const route = useRoute();
const router = useRouter();

const editing = ref(false);
const editText = ref("");
/* 中列 tab：剧本正文 / 项目设定（四件套）/ 资产表（草稿区） */
const tab = ref("script");
async function switchTab(next) {
  tab.value = next;
  if (next !== "script" && !studio.storyAnalysis) await loadScript();
}
const busy = ref(false);
const notice = ref("");
const produceOpen = ref(false);
const produceInstruction = ref("");

const script = computed(() => studio.script);
const episodes = computed(() => script.value?.episodes || []);
const currentEp = computed(() => Number(studio.project?.episode || episodes.value[0]?.no || 1));
const segTotal = computed(() => studio.segments.length);
const segDone = computed(() => studio.segments.filter((s) => s.status === "done").length);
const assetsAll = computed(() => {
  const a = script.value?.assets || {};
  return (a.character || 0) + (a.scene || 0) + (a.prop || 0);
});

const producing = computed(() => ["queued", "running"].includes(studio.produceJob?.status));

/* 正文渲染：对白行（说话人：「…」）说话人加粗、台词鎏金；场景行弱化 */
const paragraphs = computed(() =>
  String(script.value?.script || "")
    .split(/\n+/)
    .map((raw) => raw.trim())
    .filter(Boolean)
    .map((line) => {
      const m = line.match(/^(#{1,4}\s*.+|第[一二三四五六七八九十0-9]+[场幕].*)$/);
      if (m) return { kind: "scene", text: line.replace(/^#{1,4}\s*/, "") };
      const d = line.match(/^([^\s：:]{1,10})[：:]\s*[「“]?([\s\S]+)[」”]?$/);
      if (d) return { kind: "line", speaker: d[1], text: d[2] };
      return { kind: "p", text: line };
    }),
);

function pickEpisode(no) {
  if (Number(no) === currentEp.value) return;
  studio.project.episode = Number(no);
  busy.value = true;
  Promise.all([loadScript(), loadSegments()])
    .then(() => (notice.value = ""))
    .catch((err) => (notice.value = errorText(err)))
    .finally(() => (busy.value = false));
}

function startEdit() {
  editText.value = script.value?.script || "";
  editing.value = true;
}

async function saveViaResplit() {
  if (!editText.value.trim()) return;
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

async function doProduce() {
  produceOpen.value = false;
  busy.value = true;
  notice.value = "";
  try {
    const job = await produceEpisode(currentEp.value, produceInstruction.value.trim());
    notice.value = job.status === "succeeded" ? `第 ${currentEp.value} 集制作完成，共 ${segTotal.value} 段` : `制作未完成：${job.status}`;
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
    await loadAll();
  } finally {
    busy.value = false;
  }
}

onMounted(refresh);
</script>

<template>
  <div class="script-lay">
    <!-- ── 左：集列表 + 制作准备 ── -->
    <aside class="ep-side">
      <div class="side-cap cap">剧本列表</div>
      <div class="ep-scroll">
        <div
          v-for="e in episodes"
          :key="e.no"
          class="ep-item"
          :class="{ cur: Number(e.no) === currentEp }"
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
        <div class="it" :class="script?.script ? 'done' : 'todo'"><span class="n">{{ script?.script ? "✓" : "1" }}</span>导入剧本 · 自动分集</div>
        <div class="it" :class="segTotal ? 'done' : 'todo'"><span class="n">{{ segTotal ? "✓" : "2" }}</span>Agent 制作 · 分镜与提示词</div>
        <div class="it" :class="segTotal ? 'done' : 'todo'"><span class="n">{{ segTotal ? "✓" : "3" }}</span>资产就绪 {{ assetsAll }} 项</div>
        <div class="it todo" style="cursor: pointer" @click="router.push(`/project/${route.params.name}/production`)">
          <span class="n num">4</span>补齐首帧 → 批量出片
        </div>
      </div>
    </aside>

    <!-- ── 中：正文 ── -->
    <div class="script-main">
      <div class="script-head">
        <div class="doc-tabs">
          <button class="dt" :class="{ on: tab === 'script' }" type="button" @click="switchTab('script')">剧本</button>
          <button class="dt" :class="{ on: tab === 'analysis' }" type="button" @click="switchTab('analysis')">项目设定</button>
          <button class="dt" :class="{ on: tab === 'tables' }" type="button" @click="switchTab('tables')">资产表</button>
        </div>
        <template v-if="tab === 'script'">
          <h2>{{ episodes.find((e) => Number(e.no) === currentEp)?.title || `第 ${currentEp} 集` }}</h2>
          <div class="grow"></div>
          <span class="vm num">{{ script?.chars || 0 }} 字 · {{ segTotal }} 段 · {{ script?.dialogue_count || 0 }} 句对白</span>
        </template>
      </div>

      <div class="script-scroll">
        <StoryAnalysisCards v-if="tab === 'analysis'" />
        <AssetTablesPanel v-else-if="tab === 'tables'" />
        <template v-else>
        <div v-if="editing" class="script-doc editing">
          <textarea v-model="editText" class="edit-area" spellcheck="false"></textarea>
          <div class="edit-tip">保存 = 按新全文重新分集（单集异常会告警）</div>
        </div>
        <article v-else class="script-doc">
          <template v-for="(p, i) in paragraphs" :key="i">
            <div v-if="p.kind === 'scene'" class="scene">{{ p.text }}</div>
            <p v-else-if="p.kind === 'line'"><b>{{ p.speaker }}</b>：{{ p.text }}</p>
            <p v-else>{{ p.text }}</p>
          </template>
          <div v-if="!paragraphs.length" class="empty">这一集还没有正文</div>
        </article>
        </template>
      </div>

      <div v-if="tab === 'script'" class="script-foot">
        <template v-if="editing">
          <button class="btn sm" :disabled="busy" @click="editing = false">取消</button>
          <button class="btn primary sm" :disabled="busy" @click="saveViaResplit">保存并重新分集</button>
        </template>
        <template v-else>
          <button class="btn sm" @click="startEdit">编辑剧本</button>
          <button class="btn sm" :disabled="busy || producing || !script?.script" @click="produceOpen = true">
            {{ producing ? "Agent 制作中…" : "开始制作" }}
          </button>
          <span v-if="notice" class="notice">{{ notice }}</span>
          <span style="flex: 1"></span>
          <button class="btn primary sm" @click="router.push(`/project/${route.params.name}/production`)">进入制作</button>
        </template>
      </div>
    </div>

    <!-- ── 右：停靠 AI ── -->
    <AiPanel
      class="dock"
      mode="dock"
      :canvas="String(route.params.name)"
      :chips="['分析本集对白节奏', '检查穿帮风险', '下一集拆分镜']"
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
.doc-tabs { display: flex; gap: var(--sp-1); }
.dt { border: none; background: none; color: var(--text-3); font-size: var(--fs-14); font-family: var(--serif); letter-spacing: .05em; padding: 5px 14px; cursor: pointer; border-radius: var(--r-1); }
.dt:hover { color: var(--text-2); }
.dt.on { color: var(--text); background: var(--card-2); }
.script-lay { display: grid; grid-template-columns: 232px 1fr 340px; height: 100%; min-height: 0; }
.script-lay, .script-lay * { box-sizing: border-box; }

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
.script-doc { max-width: 700px; margin: 0 auto; padding: var(--sp-6) var(--sp-6) 96px; font-family: var(--serif); font-size: 16px; line-height: 2.15; color: var(--text); }
.script-doc p { margin-bottom: var(--sp-3); text-align: justify; }
.script-doc p b { color: var(--gold); font-weight: 600; }
.script-doc .scene { color: var(--text-3); font-size: var(--fs-13); letter-spacing: .12em; margin: var(--sp-5) 0 var(--sp-3); }
.script-doc .scene::before { content: "◇ "; color: var(--accent); font-size: 10px; }
.script-doc .empty, .script-doc .edit-tip { font-family: var(--sans); color: var(--text-3); font-size: var(--fs-13); padding: var(--sp-5) 0; }
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
