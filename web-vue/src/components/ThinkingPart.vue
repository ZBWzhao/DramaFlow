<script setup>
/* 思考块（对应 Toonflow 的 chat-reasoning + parts[].type === "thinking"）。
 *
 * 折叠策略照抄 Toonflow：**流式时自动展开、正文/工具一来就自动收起**。
 * 信息"来时可见、过时隐形"——这是它界面干净的真正原因。
 * 注意：只有推理模型（如 deepseek-reasoner）才会产生思考块。
 */
import { computed } from "vue";
import MarkdownText from "./MarkdownText.vue";

const props = defineProps({ part: { type: Object, required: true } });

const label = computed(() => {
  if (props.part.streaming) return "思考中…";
  const seconds = props.part.duration ? `${props.part.duration.toFixed(1)} 秒` : "";
  return seconds ? `思考 · ${seconds}` : "思考";
});
</script>

<template>
  <div class="part part-thinking" :class="{ streaming: part.streaming }">
    <button class="part-head" @click="part.expanded = !part.expanded">
      <el-icon class="chev" :class="{ open: part.expanded }"><ArrowRight /></el-icon>
      <el-icon class="icon" :class="{ spin: part.streaming }">
        <Loading v-if="part.streaming" />
        <MagicStick v-else />
      </el-icon>
      <span class="title">{{ label }}</span>
      <span class="dim">{{ part.text.length }} 字</span>
    </button>
    <div v-if="part.expanded" class="part-body">
      <MarkdownText :content="part.text" :streaming="part.streaming" />
    </div>
  </div>
</template>
