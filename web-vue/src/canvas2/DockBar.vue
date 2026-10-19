<script setup>
/* 底部浮动工具条（spec §2：46 高 / 右 88 / 下 16 / r12 / 半透明灰 + blur）。
 *
 * 只放**真的调得动**的 5 个动作：添加节点、适应窗口、搜索节点、看历史、空画布演示。
 * 以前那排「选择/组件/表情/命令」是静态 glyph，点不动，已经删掉——宁可少一个按钮，
 * 也不要摆一个假开关。
 */
defineProps({ empty: { type: Boolean, default: false } });
defineEmits(["add", "fit", "search", "history", "toggle-empty"]);
</script>

<template>
  <div class="dock">
    <button class="dock-add" type="button" title="添加节点" @click="$emit('add')">＋</button>
    <button class="dock-btn" type="button" title="适应窗口（F）" @click="$emit('fit')">⤢</button>
    <button class="dock-btn" type="button" title="搜索节点（⌘K）" @click="$emit('search')">⌕</button>
    <button class="dock-btn" type="button" title="历史版本" @click="$emit('history')">◷</button>
    <button
      class="dock-btn"
      type="button"
      :title="empty ? '回到画布' : '空画布演示'"
      @click="$emit('toggle-empty')"
    >?</button>
  </div>
</template>
