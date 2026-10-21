<script setup>
const TOOLS = [
  ["人像质感调节", "NEW"],
  ["全景"],
  ["多角度"],
  ["打光"],
  ["九宫格", "▾"],
  ["高清", "▾"],
  ["元素编辑"],
  ["图层分离"],
  ["宫格切分"],
];
</script>

<template>
  <div class="node-toolbar">
    <button v-for="[label, badge] in TOOLS" :key="label" class="tool-btn">
      {{ label }}
      <em v-if="badge" :class="badge === 'NEW' ? 'badge-new' : 'badge-caret'">{{ badge }}</em>
    </button>
    <span class="tool-divider"></span>
    <button class="tool-btn icon">↕</button>
    <button class="tool-btn icon">⤓</button>
    <button class="tool-btn icon">⤢</button>
  </div>
</template>
