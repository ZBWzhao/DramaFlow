<script setup>
const PILLS = [
  ["image", "▣", "图片生成"],
  ["video", "▷", "视频生成"],
  ["audio", "♪", "音频生成"],
  ["text", "✎", "剧本生成"],
  ["timeline", "⧗", "智能剪辑"],
];
defineEmits(["pick"]);
</script>

<template>
  <div class="hero">
    <p class="hero-hint"><span class="hero-cursor">➤</span> 双击画布 自由生成节点</p>
    <div class="hero-pills">
      <button
        v-for="[kind, glyph, label] in PILLS"
        :key="kind"
        class="hero-pill"
        @click="$emit('pick', kind)"
      >
        <span>{{ glyph }}</span>
        {{ label }}
      </button>
    </div>
  </div>
</template>
