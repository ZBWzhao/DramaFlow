<script setup>
/* 项目设定四卡：世界观 / 故事梗概 / 分集大纲 / 视听风格。
 *
 * 数据只读 studio.storyAnalysis（后端归一后的全形状，未完成的卡是空值）；
 * 运行态在 studio.analysis.cards（逐卡：idle/running/done/failed）。
 * 结构化产物走按钮触发的 pipeline；自由修改走右栏 AI 助手（聊天改字段）。
 */
import { computed, ref } from "vue";
import { cancelStoryAnalysis, runStoryAnalysis, studio } from "../../studio/studioStore.js";

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

async function start() {
  try {
    await runStoryAnalysis();
  } catch (err) {
    studio.error = err?.message || String(err);
  }
}

/* 单卡重生成：后端 cards 参数只跑这张卡，其余卡保留旧产物 */
async function regen(key) {
  try {
    await runStoryAnalysis({ cards: [key] });
  } catch (err) {
    studio.error = err?.message || String(err);
  }
}
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
          <button
            v-if="!running"
            class="regen"
            type="button"
            :title="`只重新生成「${c.title}」，其余卡不动`"
            @click="regen(c.key)"
          >↻ 重生成</button>
        </header>

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

.card { border: 1px solid var(--line); border-radius: var(--r-2); padding: var(--sp-3) var(--sp-4); background: var(--card); }
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
.card .none { color: var(--text-3); font-size: var(--fs-12); }

.outline .ep { padding: 6px 0; border-bottom: 1px solid var(--line); }
.outline .ep:last-child { border-bottom: none; }
.outline .ep-head { display: flex; gap: 10px; align-items: baseline; font-size: var(--fs-13); }
.outline .hook { color: var(--gold); font-size: var(--fs-12); }
.outline ul { margin: 4px 0 0; padding-left: 18px; color: var(--text-2); font-size: var(--fs-12); line-height: 1.8; }

.style-line p { margin: 2px 0; font-size: var(--fs-13); color: var(--text-2); line-height: 1.8; }
.style-line b { color: var(--text); font-weight: 600; }
</style>
