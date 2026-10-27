<script setup>
/* 产物卡（对话流内全宽卡，spec §3/§6）：头部「{产物名} · 已落库」+ 一句摘要 + ▸ 折叠开关；
 * 内容区复用现有面板组件——四类制片台产物走四页签面板（content/findings props 原样），
 * outline 走 PipelineOutlinePanel + GateMarkers（CreateRunPanel gated 大纲区同构）。
 * detail 单例直读 studio.pipeline.detail[type]（store 在 stage_done/gate/done 时已强制重拉，
 * 与 CreateRunPanel 读 outline 缓存同模式）。outline 确认卡内嵌时传 open=true 默认展开。 */
import { computed, ref } from "vue";
import { studio } from "../../../studio/studioStore.js";
import { STAGE_LABELS } from "./useRunFeed.js";
import GateMarkers from "../GateMarkers.vue";
import PipelineOutlinePanel from "../../script/PipelineOutlinePanel.vue";
import CastPanel from "../CastPanel.vue";
import ArtPanel from "../ArtPanel.vue";
import StagingPanel from "../StagingPanel.vue";
import StoryboardPanel from "../StoryboardPanel.vue";

const props = defineProps({
  type: { type: String, required: true }, // outline | characters | art | staging | storyboard
  open: { type: Boolean, default: false },
});

const openRef = ref(props.open);
const label = STAGE_LABELS[props.type] || props.type;

const detail = computed(() => studio.pipeline?.detail?.[props.type] || null);
const content = computed(() => {
  const c = detail.value?.content;
  return c && typeof c === "object" ? c : null;
});
const findings = computed(() => {
  const f = detail.value?.findings;
  return Array.isArray(f) ? f : [];
});

/* 一句摘要（简报口径）：防御取值，取不到数字 → 「详情展开查看」 */
const summary = computed(() => {
  const c = content.value;
  const pick = (n, unit) => (Number(n) > 0 ? `${n} ${unit}` : "详情展开查看");
  if (!c) return "详情展开查看";
  if (props.type === "characters") return pick(c.characters?.length ?? 0, "个角色");
  if (props.type === "art") return pick(Object.keys(c?.images || {}).length || c?.art?.length || 0, "项设定");
  if (props.type === "staging") return pick(c.locations?.length || c.scenes?.length || 0, "处站位");
  if (props.type === "storyboard") return pick(c.episodes?.length || 0, "集分镜");
  if (props.type === "outline") return pick(c.episodes?.length || 0, "集");
  return "详情展开查看";
});

/* 「查看大纲并确认」的滚动目标（GateConfirmCard 内嵌 outline 卡时用）：
 * script setup 组件默认封闭，暴露同签名方法让父级调用与 CreateRunPanel :241-243 逐字一致。 */
const rootEl = ref(null);
defineExpose({
  scrollIntoView(options) {
    rootEl.value?.scrollIntoView(options);
  },
});
</script>

<template>
  <div ref="rootEl" class="acard">
    <button class="acard-head" type="button" @click="openRef = !openRef">
      <span class="tri" :class="{ on: openRef }">▸</span>
      <b class="acard-label">{{ label }} · 已落库</b>
      <span class="acard-sum">{{ summary }}</span>
    </button>
    <div v-if="openRef" class="acard-body">
      <div v-if="!content" class="acard-none">产物加载中，请稍候……若长时间为空请刷新页面。</div>
      <template v-else>
        <CastPanel v-if="type === 'characters'" :content="content" :findings="findings" />
        <ArtPanel v-else-if="type === 'art'" :content="content" :findings="findings" />
        <StagingPanel v-else-if="type === 'staging'" :content="content" :findings="findings" />
        <StoryboardPanel v-else-if="type === 'storyboard'" :content="content" :findings="findings" />
        <template v-else-if="type === 'outline'">
          <PipelineOutlinePanel :outline="content" />
          <GateMarkers :findings="findings" />
        </template>
      </template>
    </div>
  </div>
</template>

<style scoped>
.acard {
  border: 1px solid var(--line);
  border-radius: var(--r-2);
  background: var(--card);
  padding: var(--sp-2) var(--sp-3);
  scroll-margin-top: 60px; /* 「查看大纲并确认」滚动落点留出页头高度（原 outline-wrap 同值） */
}
.acard, .acard * { box-sizing: border-box; }
.acard-head {
  display: flex; align-items: center; gap: 8px; width: 100%; padding: 2px 0;
  background: none; border: none; cursor: pointer; text-align: left; font-family: inherit;
}
.tri { display: inline-block; flex: none; transition: transform 160ms var(--ease, ease); font-size: 10px; color: var(--text-3); }
.tri.on { transform: rotate(90deg); }
.acard-label { font-family: var(--serif); font-size: var(--fs-13); font-weight: 600; color: var(--text); }
.acard-head:hover .acard-label { color: var(--text-2); }
.acard-sum { font-size: var(--fs-11); color: var(--text-3); }
.acard-body { margin-top: var(--sp-2); border-top: 1px solid var(--line); padding-top: var(--sp-2); }
.acard-none { font-size: var(--fs-12); color: var(--text-3); padding: var(--sp-3) 0; text-align: center; }
</style>
