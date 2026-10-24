<script setup>
/* 制片台 · 管线产物（只读）：角色 / 美术 / 站位 / 分镜四类产物的只读视图。
 *
 * 数据全部来自 /api/pipeline 导入的产物详情（studioStore.loadPipeline 缓存在
 * studio.pipeline.detail），不是画布节点——纯展示零编辑。画布老项目或 file 后端
 * 没有产物 → 列表为空 → 四类全是空态，页面本身不报错。
 * 上游产物是「尽力而为」的 JSON：各面板内部对所有字段防御取值（缺键渲染空）。
 * 注意：script 详情顶层另有响应视图 episodes[]，本页不消费 script，四类面板
 * 一律只从 detail.content 渲染。
 */
import { computed, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { loadPipeline, studio } from "../studio/studioStore.js";
import CastPanel from "./pipeline/CastPanel.vue";
import ArtPanel from "./pipeline/ArtPanel.vue";
import StagingPanel from "./pipeline/StagingPanel.vue";
import StoryboardPanel from "./pipeline/StoryboardPanel.vue";

const route = useRoute();

/* 四类产物一个子 tab 一个面板；type = 落库 artifact_type（详情缓存的键） */
const tabs = [
  { key: "cast", label: "角色设定", type: "characters", comp: CastPanel },
  { key: "art", label: "美术设定", type: "art", comp: ArtPanel },
  { key: "staging", label: "开场站位", type: "staging", comp: StagingPanel },
  { key: "storyboard", label: "分镜", type: "storyboard", comp: StoryboardPanel },
];
const tab = ref("cast");
const cur = computed(() => tabs.find((t) => t.key === tab.value) || tabs[0]);

/* 详情在手才透传 content；缺详情（未导入 / 读取失败）→ 空态 */
const content = computed(() => {
  const c = studio.pipeline?.detail?.[cur.value.type]?.content;
  return c && typeof c === "object" ? c : null;
});

/* 元信息一行：schema_version · 导入时间 · 来源批次；都缺就不显示 */
const meta = computed(() => {
  const d = studio.pipeline?.detail?.[cur.value.type];
  if (!d) return "";
  const at = Number(d.updated_at);
  const when = Number.isFinite(at) && at > 0 ? new Date(at * 1000).toLocaleString() : "";
  return [d.schema_version && `v${d.schema_version}`, when, d.source_run_id && `批次 ${d.source_run_id}`]
    .filter(Boolean)
    .join(" · ");
});

const imported = computed(() => Boolean(studio.pipeline?.artifacts?.[cur.value.type]));

onMounted(async () => {
  const name = String(route.params.name || "");
  if (name && studio.canvas !== name) studio.canvas = name;
  await loadPipeline();
});
</script>

<template>
  <div class="pv">
    <div class="pv-in">
      <div class="pv-head">
        管线产物
        <span class="sub">来自管线导入，只读 · 修订走重导入</span>
      </div>

      <div class="pv-tabs">
        <button
          v-for="t in tabs"
          :key="t.key"
          class="pt"
          :class="{ on: tab === t.key, has: Boolean(studio.pipeline?.artifacts?.[t.type]) }"
          type="button"
          @click="tab = t.key"
        >
          {{ t.label }}
        </button>
      </div>

      <div v-if="meta" class="pv-meta num">{{ meta }}</div>

      <!-- 面板：有 content 才渲染；没有 → 统一空态（Postgres 项目未导入 / file 后端 503 都落到这里） -->
      <component :is="cur.comp" v-if="content" :content="content" />
      <div v-else class="pv-none">
        该项目无此管线产物
        <span v-if="!imported" class="tip">——在管线导入流程写入 characters / art / staging / storyboard 产物后这里会显示（需 Postgres 后端）</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pv { overflow-y: auto; flex: 1; height: 100%; }
.pv, .pv * { box-sizing: border-box; }
.pv-in { max-width: 980px; margin: 0 auto; padding: var(--sp-6) var(--sp-6) 96px; }
.pv-head { font-family: var(--serif); font-size: var(--fs-20); font-weight: 600; padding-bottom: var(--sp-4); }
.pv-head .sub { font-family: var(--sans); font-size: var(--fs-12); color: var(--text-3); font-weight: 400; margin-left: 12px; }

.pv-tabs { display: flex; gap: var(--sp-1); border-bottom: 1px solid var(--line-2); }
.pt {
  position: relative; padding: 8px 16px; background: none; border: none; cursor: pointer;
  color: var(--text-3); font-size: var(--fs-13); font-family: inherit;
}
.pt:hover { color: var(--text-2); }
.pt.on { color: var(--text); }
.pt.on::after { content: ""; position: absolute; left: 12px; right: 12px; bottom: -1px; height: 2px; background: var(--accent); }
/* 有产物的 tab 加一个可读的小点：没导入的类型一眼可辨，不必点进去看空态 */
.pt.has::before { content: ""; display: inline-block; width: 5px; height: 5px; border-radius: 50%; background: var(--accent); margin-right: 7px; vertical-align: 2px; }

.pv-meta { margin: var(--sp-2) 0 0; color: var(--text-3); font-size: var(--fs-11); }
.pv-none {
  margin-top: var(--sp-6); padding: var(--sp-6) 0; text-align: center;
  color: var(--text-3); font-size: var(--fs-13);
  border: 1px dashed var(--line-2); border-radius: var(--r-2);
}
.pv-none .tip { display: block; margin-top: 8px; font-size: var(--fs-11); color: var(--text-3); opacity: .75; }
</style>
