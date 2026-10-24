<script setup>
/* 角色设定（只读）：cast.json 产物 → 角色卡。
 *
 * 字段事实（docs/pipeline/data-contract.md · characters 节）：
 * 顶层 source/lang/summary/characters[]；卡上 name/aliases[]/importance
 * （protagonist/major/supporting/minor）/oneLiner/persona/image/voice，
 * 身份 = persona.identity、弧线 = persona.arc；形象提示词 = image.prompt（全英文），
 * 音色提示词 = voice.prompt（英文，给 TTS 音色引擎）。全部防御取值，缺键渲染空。
 */
import { computed, ref } from "vue";
import { ElMessage } from "element-plus";

const props = defineProps({ content: { type: Object, default: null } });

const c = computed(() => (props.content && typeof props.content === "object" ? props.content : {}));
const summary = computed(() => String(c.value.summary || ""));
const cast = computed(() => (Array.isArray(c.value.characters) ? c.value.characters : []));

const IMPORTANCE_LABELS = { protagonist: "主角", major: "重要", supporting: "配角", minor: "次要" };
const importanceLabel = (i) => IMPORTANCE_LABELS[i] || i || "";
const str = (v) => String(v ?? "");
const list = (v) => (Array.isArray(v) ? v : []);

/* 形象/音色提示词折叠块：默认收起（提示词很长，展开才占空间），键 = `卡序-块名` */
const openMap = ref({});
const isOpen = (key) => Boolean(openMap.value[key]);
const toggle = (key) => (openMap.value[key] = !openMap.value[key]);

/* 复制：沿用 ExportDialog/ToolPart 的 ElMessage 反馈模式（全局已注册 element-plus） */
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

/* voice 的本地语说明一行：timbre/pitch/pace/accent/emotion 有啥拼啥（契约：全可缺） */
const VOICE_META_FIELDS = [
  ["timbre", "音色"],
  ["pitch", "音高"],
  ["pace", "语速"],
  ["accent", "口音"],
  ["emotion", "情绪"],
];
function voiceMeta(v) {
  if (!v || typeof v !== "object") return "";
  return VOICE_META_FIELDS.map(([key, label]) => (str(v[key]) ? `${label}：${str(v[key])}` : ""))
    .filter(Boolean)
    .join(" · ");
}
</script>

<template>
  <div class="cp">
    <p v-if="summary" class="cp-summary">{{ summary }}</p>

    <div v-if="cast.length" class="cp-cards">
      <article v-for="(ch, i) in cast" :key="str(ch?.name) || `c${i}`" class="cp-card">
        <header class="cp-head">
          <b class="cp-name">{{ str(ch?.name) }}</b>
          <span v-if="list(ch?.aliases).length" class="cp-alias">／{{ list(ch.aliases).join("／") }}</span>
          <span v-if="importanceLabel(ch?.importance)" class="cp-tier">{{ importanceLabel(ch.importance) }}</span>
        </header>

        <p v-if="str(ch?.oneLiner)" class="cp-line">{{ str(ch.oneLiner) }}</p>

        <div class="cp-facts">
          <span v-if="str(ch?.persona?.identity)" class="fact"><i>身份</i>{{ str(ch.persona.identity) }}</span>
          <span v-if="str(ch?.persona?.gender) || str(ch?.persona?.ageRange)" class="fact">
            <i>性别 / 年龄</i>{{ [str(ch?.persona?.gender), str(ch?.persona?.ageRange)].filter(Boolean).join(" · ") }}
          </span>
          <span v-if="list(ch?.persona?.personality).length" class="fact">
            <i>性格</i>{{ list(ch.persona.personality).join("、") }}
          </span>
          <span v-if="str(ch?.persona?.motivation)" class="fact"><i>动机</i>{{ str(ch.persona.motivation) }}</span>
        </div>

        <p v-if="str(ch?.persona?.arc)" class="cp-arc">
          <b>弧线</b>{{ str(ch.persona.arc) }}
        </p>

        <!-- 形象提示词（image，全英文）：prompt 可复制；negativePrompt / 译文 / 标签随块展示 -->
        <div v-if="str(ch?.image?.prompt) || str(ch?.image?.negativePrompt)" class="cp-fold">
          <button class="fold-head" type="button" @click="toggle(`i${i}`)">
            <span class="tri" :class="{ on: isOpen(`i${i}`) }">▸</span>形象提示词
            <span v-if="str(ch?.image?.prompt)" class="cp" @click.stop="copy(ch?.image?.prompt)">复制</span>
          </button>
          <div v-if="isOpen(`i${i}`)" class="fold-body">
            <p class="mono">{{ str(ch?.image?.prompt) }}</p>
            <p v-if="str(ch?.image?.promptLocal)" class="dim">译文：{{ str(ch.image.promptLocal) }}</p>
            <p v-if="str(ch?.image?.negativePrompt)" class="dim">负面：{{ str(ch.image.negativePrompt) }}</p>
            <p v-if="list(ch?.image?.tags).length" class="dim">标签：{{ list(ch.image.tags).join("、") }}</p>
          </div>
        </div>

        <!-- 音色提示词（voice）：prompt 是给 TTS 的英文（契约有意不做本地对照）；timbre 等是本地语说明 -->
        <div v-if="str(ch?.voice?.prompt) || str(ch?.voice?.timbre)" class="cp-fold">
          <button class="fold-head" type="button" @click="toggle(`v${i}`)">
            <span class="tri" :class="{ on: isOpen(`v${i}`) }">▸</span>音色提示词
            <span v-if="str(ch?.voice?.prompt)" class="cp" @click.stop="copy(ch?.voice?.prompt)">复制</span>
          </button>
          <div v-if="isOpen(`v${i}`)" class="fold-body">
            <p class="mono">{{ str(ch?.voice?.prompt) }}</p>
            <p v-if="voiceMeta(ch?.voice)" class="dim">{{ voiceMeta(ch.voice) }}</p>
            <p v-if="str(ch?.voice?.referenceHint)" class="dim">参考：{{ str(ch.voice.referenceHint) }}</p>
          </div>
        </div>
      </article>
    </div>
    <div v-else class="cp-none">—</div>
  </div>
</template>

<style scoped>
.cp { display: flex; flex-direction: column; gap: var(--sp-3); margin-top: var(--sp-3); }
.cp-summary { margin: 0; font-size: var(--fs-13); color: var(--text-2); line-height: 1.9; }

.cp-cards { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-3); }
@media (max-width: 900px) { .cp-cards { grid-template-columns: 1fr; } }
.cp-card { border: 1px solid var(--line); border-radius: var(--r-2); background: var(--card); padding: var(--sp-3) var(--sp-4); }

.cp-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.cp-name { font-family: var(--serif); font-size: var(--fs-16); }
.cp-alias { font-size: var(--fs-11); color: var(--text-3); }
.cp-tier { flex: none; background: var(--card-2); border-radius: 99px; padding: 1px 10px; font-size: var(--fs-11); color: var(--gold); }
.cp-line { margin: 6px 0 0; font-size: var(--fs-12); color: var(--text-2); line-height: 1.8; }

.cp-facts { display: flex; flex-direction: column; gap: 2px; margin-top: 8px; }
.fact { font-size: var(--fs-12); color: var(--text-2); line-height: 1.8; }
.fact i { font-style: normal; color: var(--text-3); margin-right: 8px; }

.cp-arc { margin: 8px 0 0; font-size: var(--fs-12); color: var(--text-2); line-height: 1.8; }
.cp-arc b { color: var(--text-3); font-weight: 400; margin-right: 8px; }

.cp-fold { margin-top: 8px; border-top: 1px solid var(--line); }
.fold-head {
  display: flex; align-items: center; gap: 6px; width: 100%; padding: 7px 0;
  background: none; border: none; cursor: pointer; text-align: left;
  color: var(--text-3); font-size: var(--fs-12); font-family: inherit;
}
.fold-head:hover { color: var(--text-2); }
.tri { display: inline-block; transition: transform 160ms var(--ease, ease); font-size: 10px; }
.tri.on { transform: rotate(90deg); }
.fold-head .cp { margin-left: auto; color: var(--gold); font-size: var(--fs-11); padding: 0 4px; }
.fold-head .cp:hover { text-decoration: underline; }
.fold-body { padding: 0 0 8px; }
.fold-body .mono { margin: 0; font-family: var(--mono); font-size: var(--fs-11); color: var(--text-2); line-height: 1.8; word-break: break-all; white-space: pre-wrap; }
.fold-body .dim { margin: 6px 0 0; font-size: var(--fs-11); color: var(--text-3); line-height: 1.7; }
.cp-none { color: var(--text-3); font-size: var(--fs-12); }
</style>
