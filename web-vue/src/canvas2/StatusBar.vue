<script setup>
/* 画布状态栏：一行只读事实，不抢视觉。
 * 节点数 / 选中数 / 缩放 / 最后同步时间 / 快捷键提示。
 */
import { computed } from "vue";

const props = defineProps({
  count: { type: Number, default: 0 },
  selectedCount: { type: Number, default: 0 },
  zoom: { type: Number, default: 1 },
  syncedAt: { type: Number, default: 0 },
  empty: { type: Boolean, default: false },
});

const syncText = computed(() => {
  if (!props.syncedAt) return "—";
  const date = new Date(props.syncedAt);
  const pad = (value) => String(value).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
});
</script>

<template>
  <footer class="cv2-statusbar">
    <span class="sb-item">节点 <b class="tabular">{{ count }}</b></span>
    <span v-if="selectedCount" class="sb-item accent">选中 <b class="tabular">{{ selectedCount }}</b></span>
    <span class="sb-item">缩放 <b class="tabular">{{ Math.round(zoom * 100) }}%</b></span>
    <span class="sb-sep" aria-hidden="true"></span>
    <span class="sb-item">最后同步 <b class="tabular">{{ syncText }}</b></span>
    <span v-if="empty" class="sb-item warn">空画布</span>
    <span class="sb-hint">⌘K 搜索 · F 适应 · Space 拖动 · Del 删除</span>
  </footer>
</template>

<style scoped>
.cv2-statusbar {
  grid-area: status;
  display: flex;
  align-items: center;
  gap: var(--sp-4);
  height: var(--h-statusbar);
  padding: 0 var(--sp-4);
  border-top: 1px solid var(--line);
  background: var(--bg-2);
  color: var(--text-3);
  font-size: var(--fs-11);
  white-space: nowrap;
  overflow: hidden;
}
.sb-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.sb-item b {
  color: var(--text-2);
  font-weight: 500;
}
.sb-item.accent b {
  color: var(--accent);
}
.sb-item.warn {
  color: var(--warn);
}
.sb-sep {
  width: 1px;
  height: 12px;
  background: var(--line-2);
}
.sb-hint {
  margin-left: auto;
  color: var(--text-3);
  opacity: 0.8;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
