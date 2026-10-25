<script setup>
/* 分镜（只读）：storyboard.json 产物 → 集 → 段 → 切 两级表。
 *
 * 字段事实（docs/pipeline/data-contract.md · storyboard 节）：
 * episodes[]{ep, segments[]}；段 segments[]：id（段号/素材文件名）/sceneIndex/cuts[]/
 *   h3Prompt（MiniMax H3 视频提示词）/blocking（开场构图，必填）/soundscape/music/note；
 * 切 cuts[]：beats（[起,止] 认领剧本节拍）/seconds/size（景别枚举）/camera（运镜）/
 *   characters[]（画内 C 编号）/frame（分镜图提示词）/shot（镜头正文）/
 *   lens/cameraPosition/composition/eyeline/focus/stability（构图六字段，每镜必填）。
 * 段时长契约无现成字段 = 段内切秒数求和（展示值）。全部防御取值。
 */
import { computed, ref } from "vue";
import { ElMessage } from "element-plus";

const props = defineProps({
  content: { type: Object, default: null },
  // 面板状态（Task 6，容器 PipelineView 传入）：idle/running/done/failed；
  // "" = 无活动 run 的纯只读现状。running 时内容是上一版，降透明度示意。
  state: { type: String, default: "" },
});

const c = computed(() => (props.content && typeof props.content === "object" ? props.content : {}));
const episodes = computed(() => (Array.isArray(c.value.episodes) ? c.value.episodes : []));
const aspect = computed(() => str(c.value.aspectRatio));

const str = (v) => String(v ?? "");
const list = (v) => (Array.isArray(v) ? v : []);

const SIZE_LABELS = { "extreme-wide": "大远景", wide: "远景", medium: "中景", close: "近景", "extreme-close": "特写" };
const sizeLabel = (s) => SIZE_LABELS[s] || str(s);

const cuts = (seg) => list(seg?.cuts);
/* 段时长 = 段内切秒数求和（切缺 seconds 不计入；全缺显示 —） */
function segSeconds(seg) {
  const total = cuts(seg).reduce((n, cut) => n + (Number(cut?.seconds) || 0), 0);
  if (!total) return "";
  const rounded = Math.round(total * 10) / 10;
  return `${Number.isInteger(rounded) ? rounded : rounded.toFixed(1)}s`;
}
const beatsLabel = (beats) => {
  if (!Array.isArray(beats) || !beats.length) return "—";
  return `拍 ${beats.map((b) => str(b)).join("–")}`;
};

/* 构图六字段（+音效/光照）收成镜头下的一行微注，缺哪段省哪段 */
const lensMeta = (cut) =>
  [
    [cut?.lens, "镜头"],
    [cut?.cameraPosition, "机位"],
    [cut?.composition, "构图"],
    [cut?.eyeline, "视线"],
    [cut?.focus, "焦点"],
    [cut?.stability, "稳定"],
    [cut?.sfx, "音效"],
    [cut?.lighting, "光照"],
  ]
    .filter(([v]) => str(v))
    .map(([v, label]) => `${label} ${str(v)}`)
    .join(" · ");

/* 分镜图提示词很长：单元格里截断，完整文本进悬浮与复制 */
const SHORT = 42;
const shortText = (t) => {
  const s = str(t);
  return s.length > SHORT ? `${s.slice(0, SHORT)}…` : s;
};

/* h3Prompt 折叠块：默认收起，一段一条 */
const openMap = ref({});
const isOpen = (key) => Boolean(openMap.value[key]);
const toggle = (key) => (openMap.value[key] = !openMap.value[key]);

async function copy(text) {
  const s = str(text);
  if (!s) return;
  try {
    await navigator.clipboard.writeText(s);
    ElMessage.success("已复制");
  } catch {
    ElMessage.error("复制失败");
  }
}
</script>

<template>
  <div class="sb" :class="{ stale: state === 'running' }">
    <div v-if="aspect" class="sb-spec num">画幅 {{ aspect }}</div>

    <div v-if="episodes.length">
      <section v-for="(ep, i) in episodes" :key="str(ep?.ep) || `e${i}`" class="sb-ep">
        <div class="sb-ep-head">第 {{ str(ep?.ep) || "?" }} 集<span class="cnt num">{{ list(ep?.segments).length }} 段</span></div>

        <section v-for="(seg, j) in list(ep?.segments)" :key="str(seg?.id) || `s${j}`" class="sb-seg">
          <header class="sb-seg-head">
            <b class="num">{{ str(seg?.id) }}</b>
            <span v-if="Number.isFinite(Number(seg?.sceneIndex))" class="dim">第 {{ seg.sceneIndex }} 场</span>
            <span v-if="segSeconds(seg)" class="dim">{{ segSeconds(seg) }}</span>
            <span v-if="str(seg?.blocking)" class="dim" :title="'开场构图（必填）'">构图：{{ str(seg.blocking) }}</span>
            <span v-if="str(seg?.soundscape)" class="dim">音景：{{ str(seg.soundscape) }}</span>
            <span v-if="str(seg?.music)" class="dim">配乐：{{ str(seg.music) }}</span>
          </header>

          <!-- 一段一条 H3 视频提示词：可折叠 + 复制 -->
          <div v-if="str(seg?.h3Prompt)" class="sb-fold">
            <button class="fold-head" type="button" @click="toggle(`${i}-${j}`)">
              <span class="tri" :class="{ on: isOpen(`${i}-${j}`) }">▸</span>H3 视频提示词
              <span class="cp" @click.stop="copy(seg?.h3Prompt)">复制</span>
            </button>
            <p v-if="isOpen(`${i}-${j}`)" class="fold-body mono">{{ str(seg.h3Prompt) }}</p>
          </div>

          <table v-if="cuts(seg).length" class="sb-table">
            <thead>
              <tr><th>时点</th><th>时长</th><th>景别</th><th>运镜</th><th>画内</th><th>镜头正文</th><th>分镜图提示词</th></tr>
            </thead>
            <tbody>
              <tr v-for="(cut, k) in cuts(seg)" :key="k">
                <td class="num nowrap">{{ beatsLabel(cut?.beats) }}</td>
                <td class="num nowrap">{{ Number(cut?.seconds) ? `${cut.seconds}s` : "—" }}</td>
                <td class="nowrap">{{ sizeLabel(cut?.size) || "—" }}</td>
                <td class="wrap">{{ str(cut?.camera) || "—" }}</td>
                <td class="num nowrap">{{ list(cut?.characters).join("、") || "—" }}</td>
                <td class="wrap shot">
                  <span class="shot-text">{{ str(cut?.shot) || "—" }}</span>
                  <span v-if="lensMeta(cut)" class="shot-meta">{{ lensMeta(cut) }}</span>
                  <span v-if="str(cut?.note)" class="shot-meta">注 {{ str(cut.note) }}</span>
                </td>
                <td class="wrap frame">
                  <span class="frame-text" :title="str(cut?.frame)">{{ shortText(cut?.frame) || "—" }}</span>
                  <span v-if="str(cut?.frame)" class="cp" title="复制完整提示词" @click="copy(cut?.frame)">复制</span>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-else class="sb-none">该段没有切镜</div>

          <p v-if="str(seg?.note)" class="sb-note">段注：{{ str(seg.note) }}</p>
        </section>
      </section>
    </div>
    <div v-else class="sb-none">—</div>
  </div>
</template>

<style scoped>
/* running：显示的是上一版产物，压暗示意（状态条在容器上） */
.stale { opacity: .55; transition: opacity .2s; }
.sb { margin-top: var(--sp-3); }
.sb-spec { margin-bottom: var(--sp-2); color: var(--text-3); font-size: var(--fs-11); }
.sb-ep { margin-bottom: var(--sp-4); }
.sb-ep-head { font-family: var(--serif); font-size: var(--fs-16); margin-bottom: var(--sp-2); display: flex; align-items: baseline; gap: 8px; }
.sb-ep-head .cnt { font-family: var(--sans); font-size: var(--fs-11); color: var(--text-3); }

.sb-seg { border: 1px solid var(--line); border-radius: var(--r-2); background: var(--card); padding: var(--sp-2) var(--sp-3); margin-bottom: var(--sp-2); }
.sb-seg-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.sb-seg-head b { font-family: var(--mono); font-size: var(--fs-13); color: var(--gold); }
.sb-seg-head .dim { font-size: var(--fs-11); color: var(--text-3); }

.sb-fold { border-top: 1px solid var(--line); margin-top: 6px; }
.fold-head {
  display: flex; align-items: center; gap: 6px; width: 100%; padding: 6px 0;
  background: none; border: none; cursor: pointer; text-align: left;
  color: var(--text-3); font-size: var(--fs-12); font-family: inherit;
}
.fold-head:hover { color: var(--text-2); }
.tri { display: inline-block; transition: transform 160ms var(--ease, ease); font-size: 10px; }
.tri.on { transform: rotate(90deg); }
.fold-head .cp { margin-left: auto; color: var(--gold); font-size: var(--fs-11); padding: 0 4px; }
.fold-head .cp:hover { text-decoration: underline; }
.fold-body { margin: 0 0 8px; font-family: var(--mono); font-size: var(--fs-11); color: var(--text-2); line-height: 1.8; word-break: break-all; white-space: pre-wrap; }

.sb-table { width: 100%; border-collapse: collapse; font-size: var(--fs-12); margin-top: 6px; }
.sb-table th { text-align: left; color: var(--text-3); font-weight: 400; padding: 4px 8px; border-bottom: 1px solid var(--line-2); white-space: nowrap; }
.sb-table td { padding: 6px 8px; border-bottom: 1px solid var(--line); color: var(--text-2); vertical-align: top; line-height: 1.7; }
.sb-table tr:last-child td { border-bottom: none; }
.sb-table .num { color: var(--text-3); }
.sb-table .nowrap { white-space: nowrap; }
.sb-table .wrap { min-width: 140px; }
.shot-text { display: block; }
.shot-meta { display: block; margin-top: 3px; font-size: var(--fs-11); color: var(--text-3); }
.frame-text { cursor: default; }
.sb-table .cp { margin-left: 6px; color: var(--gold); font-size: var(--fs-11); cursor: pointer; }
.sb-table .cp:hover { text-decoration: underline; }
.sb-note { margin: 6px 0 0; font-size: var(--fs-11); color: var(--text-3); }
.sb-none { color: var(--text-3); font-size: var(--fs-12); padding: 6px 0; }
</style>
