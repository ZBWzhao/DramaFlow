<script setup>
/* 内容标记（advisory 质量门标记，只读）：随产物详情顶层 findings 字段到达面板，
 * 挂在产物内容下方折叠展示。四类制片台面板共用（Task 4）；outline/script 的
 * 标记 v1 只走事件流，不进面板。
 *
 * 字段事实（Task 3 契约）：findings[] = {gate, rule, severity: "advisory", detail}。
 * 空数组 / 缺键不渲染——没有标记的面板与过去零视觉差异。
 * 展示：折叠区默认收起，标题为规格逐字文案；展开后每条 rule + detail，
 * detail 默认截 200 字，点击展开全文。
 */
import { computed, ref } from "vue";

const props = defineProps({
  findings: { type: Array, default: () => [] },
});

const items = computed(() => (Array.isArray(props.findings) ? props.findings : []));

/* ⛔ blocking 变体（失败免疫）：findings 里有 severity=blocking 的条目 → 头部换
 * ⛔ 文案 + 根元素加 .blocking（fold-head / rule 标签转 --danger），提示人工复核；
 * 无 blocking → 维持原 ⚠️ advisory 文案，零视觉差异。 */
const blockingCount = computed(() => items.value.filter((f) => f?.severity === "blocking").length);

const str = (v) => String(v ?? "");

/* 开关表：key = "head"（折叠区本体）/ 序号（单条 detail 全文展开），照抄面板折叠块模式 */
const openMap = ref({});

/* detail 预览截断：默认 200 字，点击展开全文；不超长则原样（不可点） */
const SHORT = 200;
const isLong = (d) => str(d).length > SHORT;
const shownDetail = (f, i) => {
  const s = str(f?.detail);
  return openMap.value[i] || !isLong(s) ? s : `${s.slice(0, SHORT)}…`;
};
</script>

<template>
  <section v-if="items.length" class="gm" :class="{ blocking: blockingCount > 0 }">
    <button class="fold-head" type="button" @click="openMap.head = !openMap.head">
      <span class="tri" :class="{ on: openMap.head }">▸</span><template v-if="blockingCount">⛔ {{ items.length }} 条内容标记（{{ blockingCount }} 条阻断 · 请人工复核或重跑本阶段）</template><template v-else>⚠️ {{ items.length }} 条内容标记（不阻塞 · 可人工修改或重跑本阶段）</template>
    </button>
    <div v-if="openMap.head" class="fold-body">
      <p v-for="(f, i) in items" :key="i" class="gm-item">
        <b class="gm-rule">{{ str(f?.rule) }}</b>
        <span
          class="gm-detail"
          :class="{ expandable: isLong(f?.detail) }"
          :title="openMap[i] || !isLong(f?.detail) ? undefined : '点击展开全文'"
          @click="isLong(f?.detail) && (openMap[i] = !openMap[i])"
        >{{ shownDetail(f, i) }}</span>
      </p>
    </div>
  </section>
</template>

<style scoped>
/* 样式照抄面板既有卡片段落：折叠头/三角 = CastPanel 折叠块同款；条目标签 = .cp-arc b 同款 */
.gm { border-top: 1px solid var(--line); }
.fold-head {
  display: flex; align-items: center; gap: 6px; width: 100%; padding: 7px 0;
  background: none; border: none; cursor: pointer; text-align: left;
  color: var(--text-3); font-size: var(--fs-12); font-family: inherit;
}
.fold-head:hover { color: var(--text-2); }
.tri { display: inline-block; transition: transform 160ms var(--ease, ease); font-size: 10px; }
.tri.on { transform: rotate(90deg); }
.fold-body { padding: 0 0 8px; display: flex; flex-direction: column; gap: 6px; }
.gm-item { margin: 0; font-size: var(--fs-12); color: var(--text-2); line-height: 1.8; }
.gm-rule { color: var(--text-3); font-weight: 400; margin-right: 8px; }
.gm-detail { white-space: pre-wrap; word-break: break-word; }
.gm-detail.expandable { cursor: pointer; }
.gm-detail.expandable:hover { color: var(--text); }
/* ⛔ blocking 变体：整行头部与条目 rule 标签转危险色 */
.gm.blocking .fold-head { color: var(--danger); }
.gm.blocking .gm-rule { color: var(--danger); }
</style>
