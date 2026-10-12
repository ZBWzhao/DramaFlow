<script setup>
/* Markdown 渲染。流式时节流（60ms）重解析 —— Toonflow 是 50ms 节流，
 * 目的相同：每来一个字就重排整段会把界面拖卡，尤其长回答。
 */
import { onUnmounted, ref, watch } from "vue";
import { renderMarkdown } from "../markdown.js";

const props = defineProps({
  content: { type: String, default: "" },
  streaming: { type: Boolean, default: false },
});

const html = ref(renderMarkdown(props.content));
let timer = null;
let dirty = false;

function flush() {
  html.value = renderMarkdown(props.content);
}

function schedule() {
  if (!props.streaming) {
    flush(); // 流式结束：内容不再变，立刻出最终版
    return;
  }
  if (timer) {
    dirty = true;
    return;
  }
  timer = setTimeout(() => {
    timer = null;
    flush();
    if (dirty) {
      dirty = false;
      schedule();
    }
  }, 60);
}

watch(() => props.content, schedule);
watch(
  () => props.streaming,
  (value) => {
    if (!value) flush();
  }
);
onUnmounted(() => {
  if (timer) clearTimeout(timer);
});
</script>

<template>
  <div class="md" v-html="html" />
</template>
