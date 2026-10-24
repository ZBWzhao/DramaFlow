<script setup>
/* 管线大纲（只读）：渲染导入的 outline.json 五件套。
 *
 * 数据来自 /api/pipeline 导入的产物，不是画布——这里纯展示零编辑，
 * 修订语义在阶段 2（重导入 + 体检模式）。上游技能产物是「尽力而为」的
 * JSON：所有字段访问全部防御（缺键渲染空），绝不让整块渲染崩掉。
 */
import { computed } from "vue";

const props = defineProps({ outline: { type: Object, default: null } });

/* 逐层防御：顶层 / params / adaptation 缺键或形状不对都按空处理 */
const o = computed(() => (props.outline && typeof props.outline === "object" ? props.outline : {}));
const params = computed(() => (o.value.params && typeof o.value.params === "object" ? o.value.params : {}));
const adaptation = computed(() => (o.value.adaptation && typeof o.value.adaptation === "object" ? o.value.adaptation : {}));
const characters = computed(() => (Array.isArray(o.value.characters) ? o.value.characters : []));
const beats = computed(() => (Array.isArray(o.value.beats) ? o.value.beats : []));
const episodes = computed(() => (Array.isArray(o.value.episodes) ? o.value.episodes : []));
const scenes = computed(() => (Array.isArray(o.value.scenes) ? o.value.scenes : []));
const propsList = computed(() => (Array.isArray(o.value.props) ? o.value.props : []));
const preferences = computed(() => (Array.isArray(params.value.preferences) ? params.value.preferences : []));

const TIER_LABELS = { lead: "主角", support: "配角", functional: "功能性" };
const WEIGHT_LABELS = { major: "重大", minor: "普通" };
const tierLabel = (t) => TIER_LABELS[t] || t || "";
const weightLabel = (w) => WEIGHT_LABELS[w] || w || "普通";

/* 取舍记录分组：keep/cut/merge 每条 {what, why}；risks 每条 {what, plan}（按 why 展示） */
const GROUP_DEFS = [
  { key: "keep", label: "保留" },
  { key: "cut", label: "舍弃" },
  { key: "merge", label: "合并" },
  { key: "risks", label: "风险" },
];
const adaptGroups = computed(() =>
  GROUP_DEFS.map(({ key, label }) => ({
    label,
    items: (Array.isArray(adaptation.value[key]) ? adaptation.value[key] : []).map((it) => ({
      what: it?.what || "",
      why: it?.why || it?.plan || "",
    })),
  })).filter((g) => g.items.length),
);

/* 资产清单：场景/道具登记 + 各集引用计数聚合（逐集标签在分集梗概，这里只给汇总行） */
const primarySceneCount = computed(() => scenes.value.filter((s) => s?.primary).length);
const usage = computed(() =>
  episodes.value.map((ep, i) => ({
    ep: ep?.ep ?? `第${i + 1}条`,
    scenes: Array.isArray(ep?.sceneIds) ? ep.sceneIds.length : 0,
    chars: Array.isArray(ep?.characterIds) ? ep.characterIds.length : 0,
    props: Array.isArray(ep?.propIds) ? ep.propIds.length : 0,
  })),
);
</script>

<template>
  <div class="po">
    <div class="po-notice">来自管线产物，只读；修订走重导入（阶段 2 接体检模式）</div>

    <!-- ── 改编说明：改编参数 + 取舍记录 ── -->
    <section class="po-card">
      <div class="po-cap">改编说明</div>
      <div class="po-stats">
        <span v-if="params.episodes" class="stat num">{{ params.episodes }} 集</span>
        <span v-if="params.minutesPerEpisode" class="stat num">单集 {{ params.minutesPerEpisode }} 分钟</span>
        <span v-if="params.genre" class="stat">{{ params.genre }}</span>
        <span v-if="params.adaptMode" class="stat">{{ params.adaptMode }}</span>
      </div>
      <p v-if="preferences.length" class="po-line muted">改编偏好：{{ preferences.join("；") }}</p>
      <p v-if="adaptation.core" class="po-line core">{{ adaptation.core }}</p>
      <div v-for="g in adaptGroups" :key="g.label" class="po-group">
        <div class="po-group-cap">{{ g.label }}</div>
        <div v-for="(it, i) in g.items" :key="i" class="po-rec">
          <b>{{ it.what }}</b><span v-if="it.why" class="why">——{{ it.why }}</span>
        </div>
      </div>
      <div v-if="!params.episodes && !adaptation.core && !adaptGroups.length && !preferences.length" class="po-none">—</div>
    </section>

    <!-- ── 人物表 ── -->
    <section class="po-card">
      <div class="po-cap">人物表<span class="cnt num">{{ characters.length }} 人</span></div>
      <table v-if="characters.length" class="po-table">
        <thead>
          <tr><th>编号</th><th>姓名</th><th>定位</th><th>档位</th><th>人物弧线</th></tr>
        </thead>
        <tbody>
          <tr v-for="(c, i) in characters" :key="c?.id || `c${i}`">
            <td class="num">{{ c?.id || "" }}</td>
            <td>{{ c?.name || "" }}</td>
            <td>{{ c?.role || "" }}</td>
            <td>{{ tierLabel(c?.tier) }}</td>
            <td class="wrap">{{ c?.arc || "" }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="po-none">—</div>
    </section>

    <!-- ── 爽点表 ── -->
    <section class="po-card">
      <div class="po-cap">爽点表<span class="cnt num">{{ beats.length }} 个</span></div>
      <table v-if="beats.length" class="po-table">
        <thead>
          <tr><th>编号</th><th>类型</th><th>量级</th><th>落集</th><th>铺垫</th><th>兑现</th></tr>
        </thead>
        <tbody>
          <tr v-for="(b, i) in beats" :key="b?.id || `b${i}`">
            <td class="num">{{ b?.id || "" }}</td>
            <td>{{ b?.type || "" }}</td>
            <td>{{ weightLabel(b?.weight) }}</td>
            <td class="num">{{ b?.episode ?? "" }}</td>
            <td class="wrap">{{ b?.setup || "" }}</td>
            <td class="wrap">{{ b?.payoff || "" }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="po-none">—</div>
    </section>

    <!-- ── 分集梗概 ── -->
    <section class="po-card">
      <div class="po-cap">分集梗概<span class="cnt num">{{ episodes.length }} 集</span></div>
      <div v-if="episodes.length">
        <div v-for="(ep, i) in episodes" :key="ep?.ep ?? `e${i}`" class="po-ep">
          <div class="po-ep-head"><b>第 {{ ep?.ep ?? "?" }} 集</b></div>
          <p class="po-line">{{ ep?.synopsis || "" }}</p>
          <p v-if="ep?.hook" class="po-line muted"><b>开场钩子</b>：{{ ep.hook }}</p>
          <p v-if="ep?.suspense" class="po-line muted"><b>结尾悬念</b>：{{ ep.suspense }}</p>
        </div>
      </div>
      <div v-else class="po-none">—</div>
    </section>

    <!-- ── 资产清单：场景/道具登记 + 各集引用聚合 ── -->
    <section class="po-card">
      <div class="po-cap">
        资产清单
        <span class="cnt">场景 {{ scenes.length }} 处（主场景 {{ primarySceneCount }}）· 道具 {{ propsList.length }} 件</span>
      </div>
      <table v-if="scenes.length" class="po-table">
        <thead>
          <tr><th>编号</th><th>场景</th><th>主场景</th><th>复用安排</th></tr>
        </thead>
        <tbody>
          <tr v-for="(s, i) in scenes" :key="s?.id || `s${i}`">
            <td class="num">{{ s?.id || "" }}</td>
            <td>{{ s?.name || "" }}</td>
            <td>{{ s?.primary ? "是" : "" }}</td>
            <td class="wrap">{{ s?.reusePlan || "" }}</td>
          </tr>
        </tbody>
      </table>
      <table v-if="propsList.length" class="po-table" style="margin-top: 10px">
        <thead>
          <tr><th>编号</th><th>道具</th><th>戏剧功能</th></tr>
        </thead>
        <tbody>
          <tr v-for="(p, i) in propsList" :key="p?.id || `p${i}`">
            <td class="num">{{ p?.id || "" }}</td>
            <td>{{ p?.name || "" }}</td>
            <td class="wrap">{{ p?.function || "" }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="usage.length" class="po-usage">
        <span v-for="u in usage" :key="u.ep" class="usage-it num">
          第 {{ u.ep }} 集：场景 {{ u.scenes }} · 人物 {{ u.chars }} · 道具 {{ u.props }}
        </span>
      </div>
      <div v-if="!scenes.length && !propsList.length && !usage.length" class="po-none">—</div>
    </section>
  </div>
</template>

<style scoped>
.po { max-width: 860px; margin: 0 auto; padding: var(--sp-5) var(--sp-6) 96px; display: flex; flex-direction: column; gap: var(--sp-4); }
.po-notice {
  border: 1px dashed rgba(194, 163, 107, .45); border-radius: var(--r-2);
  padding: 8px 14px; color: var(--gold); font-size: var(--fs-12); text-align: center;
}
.po-card { border: 1px solid var(--line); border-radius: var(--r-2); background: var(--card); padding: var(--sp-3) var(--sp-4); }
.po-cap { font-family: var(--serif); font-size: var(--fs-14); margin-bottom: var(--sp-2); display: flex; align-items: baseline; gap: 8px; }
.po-cap .cnt { font-family: var(--sans); font-size: var(--fs-11); color: var(--text-3); }

.po-stats { display: flex; flex-wrap: wrap; gap: var(--sp-2); margin-bottom: var(--sp-2); }
.stat { background: var(--card-2); border-radius: var(--r-pill, 99px); padding: 2px 12px; font-size: var(--fs-12); color: var(--text-2); }
.po-line { margin: 0 0 var(--sp-2); font-size: var(--fs-13); color: var(--text-2); line-height: 1.9; }
.po-line b { color: var(--text); font-weight: 600; }
.po-line.muted { color: var(--text-3); font-size: var(--fs-12); }
.po-line.core { color: var(--text); }
.po-group { margin-top: var(--sp-2); }
.po-group-cap { font-size: var(--fs-11); color: var(--text-3); letter-spacing: .1em; margin-bottom: 2px; }
.po-rec { font-size: var(--fs-12); color: var(--text-2); line-height: 1.9; }
.po-rec b { color: var(--text); font-weight: 600; }
.po-rec .why { color: var(--text-3); }

.po-table { width: 100%; border-collapse: collapse; font-size: var(--fs-12); }
.po-table th { text-align: left; color: var(--text-3); font-weight: 400; padding: 4px 8px; border-bottom: 1px solid var(--line-2); white-space: nowrap; }
.po-table td { padding: 6px 8px; border-bottom: 1px solid var(--line); color: var(--text-2); vertical-align: top; line-height: 1.7; }
.po-table tr:last-child td { border-bottom: none; }
.po-table .num { white-space: nowrap; color: var(--text-3); }
.po-table .wrap { min-width: 180px; }

.po-ep { padding: var(--sp-2) 0; border-bottom: 1px solid var(--line); }
.po-ep:last-child { border-bottom: none; padding-bottom: 0; }
.po-ep-head b { font-family: var(--serif); font-size: var(--fs-13); }
.po-ep .po-line:last-child { margin-bottom: 0; }
.po-usage { display: flex; flex-wrap: wrap; gap: 4px 18px; margin-top: var(--sp-2); }
.usage-it { font-size: var(--fs-11); color: var(--text-3); }
.po-none { color: var(--text-3); font-size: var(--fs-12); }
</style>
