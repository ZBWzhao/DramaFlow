<script setup>
/* 卡下提示词框（阶段 2 重构）。
 *
 * 交互：默认收起成 32px 单行摘要（只露出「这张卡要什么」），
 * 聚焦后向下展开到 ~132px，露出字符数与发送提示；失焦收回。
 *
 * 为什么展开是「向下」而不是「盖住卡片」：
 *   连线锚点只在卡片左右两侧的中点（geometry.js: nodeAnchor = node.y + h/2），
 *   卡片下方是空白带，展开不会遮挡任何连线路径。
 *
 * 这里不做任何智能能力：没有「参考 / 标记 / 风格」按钮（后端没有对应契约），
 * 也不显示假的模型/尺寸 chip（真实参数在审批面板里，来自 generation-plan）。
 */
import { computed, nextTick, ref, watch } from "vue";

const props = defineProps({ node: { type: Object, required: true } });
const emit = defineEmits(["send", "change"]);

const text = ref(props.node.prompt || "");
const open = ref(false);
const box = ref(null);

watch(
  () => [props.node.id, props.node.prompt],
  () => {
    if (!open.value) text.value = props.node.prompt || "";
  },
);

const canSend = computed(() => text.value.trim().length > 0);
const placeholder = computed(() => {
  if (props.node.plain) return "写下这张卡要什么；{{node:x}} 可引用其它卡";
  if (props.node.canGenerate) return "这一张要什么？写完点 ↑";
  return "可以直接上传，或在这里写一句要求";
});

function onInput() {
  emit("change", { node: props.node, text: text.value });
}

function send() {
  open.value = false;
  emit("send", { node: props.node, text: text.value.trim() });
}

function focusBox() {
  open.value = true;
  nextTick(() => box.value?.querySelector("textarea")?.focus());
}

defineExpose({ focusBox });
</script>

<template>
  <div ref="box" class="prompt-box nodrag" :class="{ open }" @click.stop @pointerdown.stop>
    <div class="pb-line">
      <textarea
        v-model="text"
        rows="1"
        :placeholder="placeholder"
        spellcheck="false"
        @input="onInput"
        @focus="open = true"
        @blur="open = false"
        @keydown.enter.exact.prevent="send"
        @keydown.esc="open = false"
      ></textarea>
      <button class="send" :disabled="!canSend" title="发送（Enter）" @click="send">↑</button>
    </div>
    <div class="pb-foot">
      <span class="pb-hint">Enter 发送</span>
      <span class="pb-count">{{ text.length }} 字</span>
    </div>
  </div>
</template>
