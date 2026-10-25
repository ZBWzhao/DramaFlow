<script setup>
/* 美术设定（只读）：art.json 产物 → 场景表 + 道具表。
 *
 * 字段事实（docs/pipeline/data-contract.md · art 节）：
 * scenes[]：id/name/primary/summary（设计意图）/anchors[]（一致性锚点 {name, desc}）/lighting[]...
 * props[]：id/name/scale（手持级/桌面级/家具级）/summary（戏剧功能）/states[]（状态变体 {state, prompt}）...
 * 顶层还有 dynasty/lang（样例常缺）。全部防御取值，缺键渲染空。
 */
import { computed } from "vue";

const props = defineProps({
  content: { type: Object, default: null },
  // 面板状态（Task 6，容器 PipelineView 传入）：idle/running/done/failed；
  // "" = 无活动 run 的纯只读现状。running 时内容是上一版，降透明度示意。
  state: { type: String, default: "" },
});

const c = computed(() => (props.content && typeof props.content === "object" ? props.content : {}));
const scenes = computed(() => (Array.isArray(c.value.scenes) ? c.value.scenes : []));
const propsList = computed(() => (Array.isArray(c.value.props) ? c.value.props : []));

const str = (v) => String(v ?? "");
const list = (v) => (Array.isArray(v) ? v : []);

/* 一致性锚点：单元格里只报名（3–5 个），desc 收进悬浮提示 */
const anchorNames = (a) => list(a).map((x) => str(x?.name)).filter(Boolean);
const anchorTitle = (a) =>
  list(a)
    .map((x) => (str(x?.name) ? `${str(x.name)}：${str(x?.desc)}` : ""))
    .filter(Boolean)
    .join("\n");
</script>

<template>
  <div class="ap" :class="{ stale: state === 'running' }">
    <!-- ── 场景设定 ── -->
    <section class="ap-card">
      <div class="ap-cap">场景设定<span class="cnt num">{{ scenes.length }} 处（主场景 {{ scenes.filter((s) => s?.primary).length }}）</span></div>
      <table v-if="scenes.length" class="ap-table">
        <thead>
          <tr><th>编号</th><th>场景</th><th>主场景</th><th>设计意图</th><th>一致性锚点</th></tr>
        </thead>
        <tbody>
          <tr v-for="(s, i) in scenes" :key="str(s?.id) || `s${i}`">
            <td class="num">{{ str(s?.id) }}</td>
            <td>{{ str(s?.name) }}</td>
            <td>{{ s?.primary ? "是" : "" }}</td>
            <td class="wrap">{{ str(s?.summary) }}</td>
            <td class="wrap anchors" :title="anchorTitle(s?.anchors)">{{ anchorNames(s?.anchors).join("、") }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="ap-none">—</div>
    </section>

    <!-- ── 叙事道具 ── -->
    <section class="ap-card">
      <div class="ap-cap">叙事道具<span class="cnt num">{{ propsList.length }} 件</span></div>
      <table v-if="propsList.length" class="ap-table">
        <thead>
          <tr><th>编号</th><th>道具</th><th>级别</th><th>戏剧功能</th><th>状态变体 → 提示词</th></tr>
        </thead>
        <tbody>
          <tr v-for="(p, i) in propsList" :key="str(p?.id) || `p${i}`">
            <td class="num">{{ str(p?.id) }}</td>
            <td>{{ str(p?.name) }}</td>
            <td class="num">{{ str(p?.scale) }}</td>
            <td class="wrap">{{ str(p?.summary) }}</td>
            <td class="wrap states">
              <span v-for="(st, j) in list(p?.states)" :key="j" class="state">
                <b>{{ str(st?.state) }}</b>：{{ str(st?.prompt) }}
              </span>
              <span v-if="!list(p?.states).length">—</span>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="ap-none">—</div>
    </section>
  </div>
</template>

<style scoped>
/* running：显示的是上一版产物，压暗示意（状态条在容器上） */
.stale { opacity: .55; transition: opacity .2s; }
.ap { display: flex; flex-direction: column; gap: var(--sp-3); margin-top: var(--sp-3); }
.ap-card { border: 1px solid var(--line); border-radius: var(--r-2); background: var(--card); padding: var(--sp-3) var(--sp-4); }
.ap-cap { font-family: var(--serif); font-size: var(--fs-14); margin-bottom: var(--sp-2); display: flex; align-items: baseline; gap: 8px; }
.ap-cap .cnt { font-family: var(--sans); font-size: var(--fs-11); color: var(--text-3); }

.ap-table { width: 100%; border-collapse: collapse; font-size: var(--fs-12); }
.ap-table th { text-align: left; color: var(--text-3); font-weight: 400; padding: 4px 8px; border-bottom: 1px solid var(--line-2); white-space: nowrap; }
.ap-table td { padding: 6px 8px; border-bottom: 1px solid var(--line); color: var(--text-2); vertical-align: top; line-height: 1.7; }
.ap-table tr:last-child td { border-bottom: none; }
.ap-table .num { white-space: nowrap; color: var(--text-3); }
.ap-table .wrap { min-width: 160px; }
.anchors { cursor: default; }
.states .state { display: block; }
.states .state b { color: var(--text); font-weight: 600; }
.ap-none { color: var(--text-3); font-size: var(--fs-12); }
</style>
