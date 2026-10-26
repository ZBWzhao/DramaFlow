<script setup>
/* 开场站位（只读）：staging.json 产物 → 每场一段：走位句全文 + 在位人物表。
 *
 * 字段事实（docs/pipeline/data-contract.md · staging 节）：
 * locations[]：sceneId/name/readFrom/items[]（4–20 件，从北到南排）；
 * scenes[]：ep/scene/sceneId/lighting/cast[]/{id,name}/places[]（每人正好一条）/
 *   extras/extrasWhere/why；
 * places[]：id/name/present/item/side（north/east/south/west/on/null）/
 *   along（沿侧 0–1）/posture（sit/stand）/facing（八方位）。
 * 走位句是前端按这些字段拼的展示文本（上游没有现成句子字段）。全部防御取值。
 */
import { computed } from "vue";
import GateMarkers from "./GateMarkers.vue";

const props = defineProps({
  content: { type: Object, default: null },
  // 面板状态（Task 6，容器 PipelineView 传入）：idle/running/done/failed；
  // "" = 无活动 run 的纯只读现状。running 时内容是上一版，降透明度示意。
  state: { type: String, default: "" },
  // advisory 质量门标记（Task 4，容器 PipelineView 传入）：详情顶层 findings；
  // 空数组 / 缺键 → GateMarkers 不渲染，面板零视觉差异。
  findings: { type: Array, default: () => [] },
});

const c = computed(() => (props.content && typeof props.content === "object" ? props.content : {}));
const locations = computed(() => (Array.isArray(c.value.locations) ? c.value.locations : []));
const scenes = computed(() => (Array.isArray(c.value.scenes) ? c.value.scenes : []));

const str = (v) => String(v ?? "");
const list = (v) => (Array.isArray(v) ? v : []);

const SIDE_LABELS = { north: "北侧", east: "东侧", south: "南侧", west: "西侧", on: "道具上" };
const FACING_LABELS = {
  north: "正北", northeast: "东北", east: "正东", southeast: "东南",
  south: "正南", southwest: "西南", west: "正西", northwest: "西北",
};
const sideLabel = (s) => SIDE_LABELS[s] || "";
const facingLabel = (f) => FACING_LABELS[f] || str(f);
const postureLabel = (p) => (p === "sit" ? "坐" : p === "stand" ? "站" : "");

const locationName = (sceneId) => str(locations.value.find((l) => l?.sceneId === sceneId)?.name);

/* 走位句：一条 place 一句。present=false / side=null / 字段异常都有兜底措辞 */
function placeSentence(p) {
  const name = str(p?.name) || str(p?.id) || "（未名角色）";
  if (!p?.present) return `${name}：本场不在场`;
  const pos = postureLabel(p?.posture);
  const side = sideLabel(p?.side);
  if (!pos || !side) return `${name}：开场在场，位置未定`;
  const along = p?.side === "on" ? "" : Number.isFinite(Number(p?.along)) ? `（沿 ${p.along}）` : "";
  const at = p?.side === "on" && str(p?.item) ? `在「${str(p.item)}」上` : side;
  const facing = facingLabel(p?.facing) ? `，面朝${facingLabel(p.facing)}` : "";
  const item = p?.side !== "on" && str(p?.item) ? `，落于「${str(p.item)}」` : "";
  return `${name}：${pos}${at}${along}${facing}${item}`;
}
</script>

<template>
  <div class="sp" :class="{ stale: state === 'running' }">
    <div v-if="scenes.length">
      <section v-for="(sc, i) in scenes" :key="`${str(sc?.ep)}-${str(sc?.scene) || i}`" class="sp-card">
        <header class="sp-head">
          <b>第 {{ str(sc?.ep) || "?" }} 集 · 第 {{ str(sc?.scene) || "?" }} 场</b>
          <span class="loc">{{ str(sc?.sceneId) }}<template v-if="locationName(sc?.sceneId)"> · {{ locationName(sc.sceneId) }}</template></span>
          <span v-if="str(sc?.lighting)" class="tag">{{ str(sc.lighting) }}</span>
          <span v-if="Number(sc?.extras) > 0" class="tag">群演 {{ sc.extras }}<template v-if="str(sc?.extrasWhere)"> · {{ str(sc.extrasWhere) }}</template></span>
        </header>
        <p v-if="str(sc?.why)" class="sp-why">{{ str(sc.why) }}</p>

        <!-- 走位句整段展示 -->
        <div v-if="list(sc?.places).length" class="sp-text">
          <p v-for="(p, j) in list(sc.places)" :key="str(p?.id) || j" class="sentence">{{ placeSentence(p) }}</p>
        </div>

        <!-- 在位人物表（present=true 的走位） -->
        <table v-if="list(sc?.places).some((p) => p?.present)" class="sp-table">
          <thead>
            <tr><th>角色</th><th>姿态</th><th>位置</th><th>沿侧</th><th>朝向</th><th>所在物</th></tr>
          </thead>
          <tbody>
            <tr v-for="(p, j) in list(sc.places).filter((p) => p?.present)" :key="str(p?.id) || j">
              <td>{{ str(p?.name) || str(p?.id) }}</td>
              <td>{{ postureLabel(p?.posture) }}</td>
              <td>{{ sideLabel(p?.side) || "—" }}</td>
              <td class="num">{{ p?.side !== "on" && Number.isFinite(Number(p?.along)) ? p.along : "—" }}</td>
              <td>{{ facingLabel(p?.facing) || "—" }}</td>
              <td>{{ str(p?.item) || "—" }}</td>
            </tr>
          </tbody>
        </table>

        <p v-if="list(sc?.places).some((p) => !p?.present)" class="sp-absent">
          不在场：{{ list(sc.places).filter((p) => !p?.present).map((p) => str(p?.name) || str(p?.id)).filter(Boolean).join("、") }}
        </p>
      </section>
    </div>
    <div v-else class="sp-none">—</div>

    <!-- advisory 质量门标记（Task 4）：产物内容下方折叠展示；findings 为空不渲染 -->
    <GateMarkers :findings="findings" />
  </div>
</template>

<style scoped>
/* running：显示的是上一版产物，压暗示意（状态条在容器上） */
.stale { opacity: .55; transition: opacity .2s; }
.sp { display: flex; flex-direction: column; gap: var(--sp-3); margin-top: var(--sp-3); }
.sp-card { border: 1px solid var(--line); border-radius: var(--r-2); background: var(--card); padding: var(--sp-3) var(--sp-4); }
.sp-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.sp-head b { font-family: var(--serif); font-size: var(--fs-14); }
.sp-head .loc { font-size: var(--fs-12); color: var(--text-2); }
.sp-head .tag { background: var(--card-2); border-radius: 99px; padding: 1px 10px; font-size: var(--fs-11); color: var(--text-3); }
.sp-why { margin: 6px 0 0; font-size: var(--fs-12); color: var(--text-3); }

.sp-text { margin-top: 8px; padding: var(--sp-2) var(--sp-3); background: var(--card-2); border-radius: var(--r-1); }
.sp-text .sentence { margin: 0; font-size: var(--fs-12); color: var(--text-2); line-height: 2; white-space: pre-wrap; }

.sp-table { width: 100%; border-collapse: collapse; font-size: var(--fs-12); margin-top: 8px; }
.sp-table th { text-align: left; color: var(--text-3); font-weight: 400; padding: 4px 8px; border-bottom: 1px solid var(--line-2); white-space: nowrap; }
.sp-table td { padding: 5px 8px; border-bottom: 1px solid var(--line); color: var(--text-2); line-height: 1.7; }
.sp-table tr:last-child td { border-bottom: none; }
.sp-table .num { white-space: nowrap; color: var(--text-3); }
.sp-absent { margin: 8px 0 0; font-size: var(--fs-11); color: var(--text-3); }
.sp-none { color: var(--text-3); font-size: var(--fs-12); }
</style>
