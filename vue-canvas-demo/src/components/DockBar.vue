<script setup>
defineProps({ empty: { type: Boolean, default: false } });
defineEmits(["add", "toggle-empty"]);
</script>

<template>
  <div class="dock">
    <button class="dock-add" title="添加节点" @click="$emit('add')">＋</button>
    <button class="dock-btn on" title="选择">⌖</button>
    <button class="dock-btn" title="组件">⧉</button>
    <button class="dock-btn" title="表情">☺</button>
    <button class="dock-btn" title="历史">◷</button>
    <button class="dock-btn" title="命令">⌨</button>
    <button class="dock-btn" :title="empty ? '回到画布' : '空画布演示'" @click="$emit('toggle-empty')">?</button>
  </div>
</template>
