<script setup>
/* 完成卡（spec §2 done 行）：全部阶段正常 → 绿；有 degraded 阶段（失败免疫兜底
 * 通过，stage_done 帧 degraded/blocking 已由 store 落进 run.stages）→ 黄，
 * 计数在头部，明细指向产物卡。无 props——数据全部 store 直读。 */
import { studio } from "../../../studio/studioStore.js";

/* degraded 派生（简报口径）：done 是终态，卡片挂载时 stages 已定型，无需 computed */
const degradedStages = Object.entries(studio.run.stages).filter(([, s]) => s.degraded);
</script>

<template>
  <div class="done" :class="{ degraded: degradedStages.length }">
    <template v-if="degradedStages.length">本轮运行完成 · {{ degradedStages.length }} 个阶段带 ⛔ 阻断标记（产物卡内有明细）</template>
    <template v-else>本轮运行完成，产物已导入，可在下方各页签查看。</template>
  </div>
</template>

<style scoped>
.done {
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-2);
  border: 1px solid color-mix(in srgb, var(--ok) 40%, transparent);
  background: var(--card);
  color: var(--ok);
  font-size: var(--fs-13);
  line-height: 1.7;
}
.done.degraded {
  border-color: color-mix(in srgb, var(--warn) 45%, transparent);
  color: var(--warn);
}
</style>
