<script setup>
/* 项目设定四卡：世界观 / 故事梗概 / 分集大纲 / 视听风格。
 *
 * 数据只读 studio.storyAnalysis（后端归一后的全形状，未完成的卡是空值）；
 * 运行态在 studio.analysis.cards（逐卡：idle/running/done/failed）。
 * 结构化产物走按钮触发的 pipeline；自由修改走右栏 AI 助手（聊天改字段）。
 */
import { computed, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { cancelStoryAnalysis, getStoryPrompts, runStoryAnalysis, saveStoryPrompt, studio } from "../../studio/studioStore.js";

const CARDS = [
  { key: "worldview", title: "世界观" },
  { key: "synopsis", title: "故事梗概" },
  { key: "episode_outline", title: "分集大纲" },
  { key: "audiovisual_style", title: "视听风格" },
];
const STYLE_LABELS = { visual: "画面基调", camera: "镜头语言", pacing: "节奏" };

const analysis = computed(() => studio.storyAnalysis || {});
const running = computed(() => studio.analysis.running);
const hasAny = computed(() => Boolean(analysis.value.worldview || analysis.value.synopsis));
const skillTag = computed(() => {
  const s = analysis.value.skill || {};
  return s.name ? `${s.name}@${s.version}` : "";
});

function cardState(key) {
  return studio.analysis.cards[key] || { status: analysis.value[key] && (!Array.isArray(analysis.value[key]) || analysis.value[key].length) ? "done" : "idle" };
}

function stateLabel(status) {
  return { running: "生成中…", done: "✓", failed: "失败" }[status] || "";
}

const outline = computed(() => (Array.isArray(analysis.value.episode_outline) ? analysis.value.episode_outline : []));
const styleObj = computed(() => (analysis.value.audiovisual_style && typeof analysis.value.audiovisual_style === "object" ? analysis.value.audiovisual_style : {}));

/* 文本卡：解析中优先显示流式文本（card_delta 实时累积），否则显示落库产物 */
function cardText(key) {
  const live = studio.analysis.cards[key];
  if (live?.status === "running" && live.text) return live.text;
  return String(analysis.value[key] || "");
}
/* 长文折叠：默认收起 8 行，展开后全文；手动展开状态按卡记忆 */
const LONG = 160;
const expanded = ref({});
const isLong = (key) => cardText(key).length > LONG;
const clamped = (key) => !expanded.value[key] && isLong(key) && cardState(key).status !== "running";

/* 失败必须看得见：studio.error 没有任何地方渲染，全靠这里弹 toast */
function reportError(action, err) {
  ElMessage.error(`${action}失败：${err?.message || String(err)}`);
}

async function start() {
  try {
    await runStoryAnalysis();
  } catch (err) {
    reportError("解析", err);
  }
}

/* 单卡重生成：后端 cards 参数只跑这张卡，其余卡保留旧产物；收起浮窗避免「改了没存」的歧义 */
async function regen(key) {
  promptOpenKey.value = "";
  try {
    await runStoryAnalysis({ cards: [key] });
  } catch (err) {
    reportError("重生成", err);
  }
}

/* ---------- 每卡解析指令（提示词浮窗） ---------- */
const promptOpenKey = ref("");
const promptDraft = ref("");
const promptSaving = ref(false);
const promptCache = ref({}); // key -> {custom, default}，进 tab 时拉一次
async function loadPrompts() {
  try {
    const data = await getStoryPrompts();
    promptCache.value = data.cards || {};
  } catch {
    promptCache.value = {}; // 拉不到就用内置默认（浮窗里显示空再保存即自定义）
  }
}
function openPrompt(key) {
  // 再点一次同一张卡的按钮 = 收起
  promptOpenKey.value = promptOpenKey.value === key ? "" : key;
  const entry = promptCache.value[key] || {};
  promptDraft.value = entry.custom || entry.default || "";
}
const hasCustom = (key) => Boolean(promptCache.value[key]?.custom);
async function savePrompt(key) {
  promptSaving.value = true;
  try {
    await saveStoryPrompt(key, promptDraft.value);
    promptCache.value[key] = { ...(promptCache.value[key] || {}), custom: promptDraft.value.trim() };
    promptOpenKey.value = "";
  } catch (err) {
    reportError("保存提示词", err);
  } finally {
    promptSaving.value = false;
  }
}
async function resetPrompt(key) {
  promptSaving.value = true;
  try {
    await saveStoryPrompt(key, "");
    promptCache.value[key] = { ...(promptCache.value[key] || {}), custom: "" };
    promptDraft.value = promptCache.value[key]?.default || "";
  } catch (err) {
    reportError("恢复默认", err);
  } finally {
    promptSaving.value = false;
  }
}
onMounted(loadPrompts);
</script>

<template>
  <div class="sa">
    <div class="sa-head">
      <span class="cap">项目设定 · 四件套</span>
      <span v-if="skillTag" class="tag num">{{ skillTag }} 解析</span>
      <span style="flex: 1"></span>
      <button v-if="!running" class="btn sm primary" type="button" @click="start">
        {{ hasAny ? "重新解析" : "开始解析" }}
      </button>
      <button v-else class="btn sm" type="button" @click="cancelStoryAnalysis()">取消解析</button>
    </div>

    <div class="sa-scroll">
      <div v-if="!hasAny && !running" class="sa-empty">
        还没有解析过剧本。<b>开始解析</b>让 AI 通读全本产出四件套——
        资产表和单集制作都会引用这里的口径；想改某张卡，直接在右栏 AI 助手里说。
      </div>

      <section
        v-for="c in CARDS"
        :key="c.key"
        class="card"
        :class="{ run: cardState(c.key).status === 'running', fail: cardState(c.key).status === 'failed' }"
      >
        <header>
          <span class="ct">{{ c.title }}</span>
          <span class="stx" :class="cardState(c.key).status">{{ stateLabel(cardState(c.key).status) }}</span>
          <span style="flex: 1"></span>
          <button v-if="!running" class="regen" :class="{ 'has-custom': hasCustom(c.key) }" type="button" :title="hasCustom(c.key) ? '当前使用自定义指令，点击查看/修改' : '查看/修改这张卡的解析指令'" @click="openPrompt(c.key)">✎ 提示词</button>
          <button
            v-if="!running"
            class="regen"
            type="button"
            :title="`只重新生成「${c.title}」，其余卡不动`"
            @click="regen(c.key)"
          >↻ 重生成</button>
        </header>

        <!-- 提示词浮窗：显示当前正在使用的指令；保存后点「↻ 重生成」按它重跑该卡 -->
        <div v-if="promptOpenKey === c.key" class="pw-pop" @click.stop>
          <div class="pw-cap">
            {{ c.title }} · 解析指令（当前生效）
            <span class="pw-tag" :class="{ custom: hasCustom(c.key) }">{{ hasCustom(c.key) ? "自定义" : "默认" }}</span>
          </div>
          <textarea v-model="promptDraft" class="pw-ta" spellcheck="false"></textarea>
          <div class="pw-foot">
            <span class="pw-hint">保存后点「↻ 重生成」按此指令重跑该卡</span>
            <button v-if="hasCustom(c.key)" class="pw-btn" type="button" @click="resetPrompt(c.key)">恢复默认</button>
            <button class="pw-btn ghost" type="button" @click="promptOpenKey = ''">取消</button>
            <button class="pw-btn primary" type="button" :disabled="promptSaving" @click="savePrompt(c.key)">
              {{ promptSaving ? "保存中…" : "保存" }}
            </button>
          </div>
        </div>

        <div v-if="c.key === 'episode_outline'" class="outline">
          <div v-for="ep in outline" :key="ep.no" class="ep">
            <div class="ep-head"><b>第{{ ep.no }}集 {{ ep.title }}</b><span class="hook">{{ ep.hook }}</span></div>
            <ul v-if="(ep.beats || []).length"><li v-for="(b, i) in ep.beats" :key="i">{{ b }}</li></ul>
          </div>
          <div v-if="!outline.length" class="none">{{ cardState(c.key).status === 'running' ? '生成中…' : '—' }}</div>
        </div>

        <div v-else-if="c.key === 'audiovisual_style'" class="style-line">
          <template v-if="Object.keys(styleObj).length">
            <p v-for="(v, k) in styleObj" :key="k"><b>{{ STYLE_LABELS[k] || k }}</b>：{{ v }}</p>
          </template>
          <div v-else class="none">{{ cardState(c.key).status === 'running' ? '生成中…' : '—' }}</div>
        </div>

        <template v-else>
          <p class="text" :class="{ clamp: clamped(c.key) }">{{ cardText(c.key) || (cardState(c.key).status === 'running' ? '生成中…' : '—') }}</p>
          <button v-if="isLong(c.key)" class="fold" type="button" @click="expanded[c.key] = !expanded[c.key]">
            {{ expanded[c.key] ? "收起 ▲" : `展开全文（${cardText(c.key).length} 字）▼` }}
          </button>
        </template>
      </section>
    </div>
  </div>
</template>

<style scoped>
.sa { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.sa-head { display: flex; align-items: center; gap: var(--sp-3); padding: 0 0 var(--sp-3); }
.sa-head .tag { font-size: var(--fs-11); color: var(--text-3); }
.sa-scroll { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: var(--sp-3); padding-bottom: var(--sp-4); }
.sa-empty { padding: var(--sp-4); border: 1px dashed var(--line-2); border-radius: var(--r-2); color: var(--text-2); font-size: var(--fs-13); line-height: 1.8; }

.card { border: 1px solid var(--line); border-radius: var(--r-2); padding: var(--sp-3) var(--sp-4); background: var(--card); position: relative; }
.card.run { border-color: rgba(194, 163, 107, .55); }
.card.fail { border-color: rgba(196, 74, 58, .55); }
.card header { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 6px; }
.card .ct { font-family: var(--serif); font-size: var(--fs-14); }
.card .stx { font-size: var(--fs-11); color: var(--text-3); }
.card .stx.running { color: var(--gold); }
.card .stx.failed { color: #c44a3a; }
.card .text { white-space: pre-wrap; color: var(--text-2); font-size: var(--fs-13); line-height: 1.9; }
.card .text.clamp { display: -webkit-box; -webkit-line-clamp: 8; -webkit-box-orient: vertical; overflow: hidden; }
.fold { border: none; background: none; color: var(--gold); font-size: var(--fs-12); padding: 4px 0 0; cursor: pointer; font-family: inherit; }
.fold:hover { color: var(--text); }
.regen { border: 1px solid var(--line); background: none; color: var(--text-3); font-size: var(--fs-11); padding: 3px 9px; border-radius: var(--r-1); cursor: pointer; font-family: inherit; }
.regen:hover { color: var(--gold); border-color: rgba(194, 163, 107, .5); }
/* 这张卡正在用自定义指令：提示词按钮亮金，不开浮窗也看得出 */
.regen.has-custom { color: var(--gold); border-color: rgba(194, 163, 107, .4); }

/* 提示词浮窗（提层样式与历史下拉同族） */
.pw-pop {
  position: absolute; top: 40px; right: var(--sp-4); z-index: 30;
  width: min(520px, 90%);
  background: #1e1d1b; border: 1px solid rgba(255, 251, 243, .16); border-radius: 12px;
  box-shadow: 0 20px 56px rgba(0, 0, 0, .72); padding: 10px;
}
.pw-cap { display: flex; align-items: center; gap: 8px; color: var(--text-3); font-size: var(--fs-11); margin-bottom: 7px; }
.pw-tag { padding: 1px 8px; border-radius: var(--r-pill); background: var(--card-2); }
.pw-tag.custom { color: var(--gold); }
.pw-ta {
  width: 100%; min-height: 150px; max-height: 280px; overflow-y: auto;
  background: var(--card); border: 1px solid var(--line-2); border-radius: 8px;
  color: var(--text); font-family: var(--mono, monospace); font-size: 12px; line-height: 1.7;
  padding: 9px 11px; resize: vertical; box-sizing: border-box;
}
.pw-ta:focus { outline: none; border-color: rgba(194, 163, 107, .5); }
.pw-foot { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
.pw-hint { margin-right: auto; color: var(--text-3); font-size: var(--fs-11); }
.pw-btn { border: 1px solid var(--line-2); background: none; color: var(--text-2); font-size: var(--fs-12); padding: 4px 12px; border-radius: 8px; cursor: pointer; font-family: inherit; }
.pw-btn:hover { color: var(--text); }
.pw-btn.ghost { border-color: transparent; color: var(--text-3); }
.pw-btn.primary { background: var(--accent); border-color: transparent; color: #FFF7F0; }
.pw-btn.primary:hover { background: var(--accent-hi); color: #FFF7F0; }
.pw-btn:disabled { opacity: .5; cursor: not-allowed; }
.card .none { color: var(--text-3); font-size: var(--fs-12); }

.outline .ep { padding: 6px 0; border-bottom: 1px solid var(--line); }
.outline .ep:last-child { border-bottom: none; }
.outline .ep-head { display: flex; gap: 10px; align-items: baseline; font-size: var(--fs-13); }
.outline .hook { color: var(--gold); font-size: var(--fs-12); }
.outline ul { margin: 4px 0 0; padding-left: 18px; color: var(--text-2); font-size: var(--fs-12); line-height: 1.8; }

.style-line p { margin: 2px 0; font-size: var(--fs-13); color: var(--text-2); line-height: 1.8; }
.style-line b { color: var(--text); font-weight: 600; }
</style>
