<script setup>
/* stage_gate 状态行（stage 消息内原子件）。row = {kind:"gate", ok, round, advisory, fallback}。
 * 未过 = 红「第 N 轮质量门：未过」；通过 = 绿；通过且 advisory>0 = 鎏金「…通过 · N 条内容标记」；
 * fallback 时行尾追加灰字「（兜底门）」。 */
import { computed } from "vue";

const props = defineProps({ row: { type: Object, required: true } });

const text = computed(() => {
  const base = `第 ${props.row.round ?? "?"} 轮质量门：${props.row.ok ? "通过" : "未过"}`;
  return props.row.ok && Number(props.row.advisory) > 0
    ? `${base} · ${Number(props.row.advisory)} 条内容标记`
    : base;
});

const tone = computed(() => {
  if (!props.row.ok) return "fail";
  return Number(props.row.advisory) > 0 ? "marked" : "pass";
});
</script>

<template>
  <div class="gate" :class="tone">
    <span class="txt">{{ text }}</span>
    <span v-if="row.fallback" class="fb">（兜底门）</span>
  </div>
</template>

<style scoped>
.gate {
  display: flex;
  align-items: baseline;
  gap: 2px;
  font-size: var(--fs-12);
  line-height: 1.6;
  padding: 1px 0;
}
.gate.fail { color: var(--danger); }
.gate.pass { color: var(--ok); }
.gate.marked { color: var(--gold); }
.fb { color: var(--text-3); font-size: var(--fs-11); flex-shrink: 0; }
</style>
