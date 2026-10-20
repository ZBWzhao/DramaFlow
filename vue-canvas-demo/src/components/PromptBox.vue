<script setup>
import { ref, watch } from "vue";

const props = defineProps({ node: { type: Object, required: true } });
const emit = defineEmits(["send"]);

const text = ref(props.node.prompt || "");
watch(
  () => [props.node.id, props.node.prompt],
  () => {
    text.value = props.node.prompt || "";
  },
);
</script>

<template>
  <div class="prompt-box nodrag" @click.stop>
    <div class="prompt-chips">
      <button class="chip">＋ 参考</button>
      <button class="chip">◷ 标记</button>
      <button class="chip">❖ 风格</button>
      <span class="grow"></span>
      <button class="chip ghost">⤢</button>
    </div>

    <textarea
      v-model="text"
      rows="3"
      placeholder="描述这一张要什么，或用 {{node:x}} 引用其它卡片"
    ></textarea>

    <div class="prompt-foot">
      <span class="model-chip">❖ {{ node.model || "Lib Image 2.5 Pro" }} ▾</span>
      <span class="sep"></span>
      <span class="size-chip">{{ node.size || "16:9 · 标准画质 · 2K · 1张" }} ▾</span>
      <span class="grow"></span>
      <span class="prompt-icons"><b>A</b><b>⌗</b><b>15</b></span>
      <button class="send" title="发送" @click="emit('send', node)">↑</button>
    </div>
  </div>
</template>
