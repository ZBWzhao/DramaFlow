<script setup>
/* 卡下提示词框（B 版）。
 *
 * 与 demo 的差别：这里的输入是**能真的打字、真的保存**的。
 * demo 版只是好看的壳；现在：
 *   · 文字类卡（提示词/文本/分镜）→ 保存进 assets/<id>/content.md
 *   · 生成类卡（图片/视频/旁白生成）→ 保存成 data.prompt，点 ↑ 只跑这一张
 *   · 素材类卡（资产/图片/视频/音频）→ 保存成 data.prompt（这张卡要什么），上传走工具条
 */
import { computed, nextTick, ref, watch } from "vue";

const props = defineProps({ node: { type: Object, required: true } });
const emit = defineEmits(["send", "change"]);

const text = ref(props.node.prompt || "");
const editing = ref(false);
const box = ref(null);

watch(
  () => [props.node.id, props.node.prompt],
  () => {
    if (!editing.value) text.value = props.node.prompt || "";
  },
);

const canSend = computed(() => text.value.trim().length > 0 || Boolean(props.node.generator));
const placeholder = computed(() => {
  if (props.node.plain) return "写下这张卡要什么；也可以写 {{node:x}} 引用其它卡";
  if (props.node.generator) return "这一张要什么？写完点 ↑ 只跑这一张";
  return "可以直接上传，或在这里写一句要求";
});

function onInput() {
  editing.value = true;
  emit("change", { node: props.node, text: text.value });
}

function send() {
  editing.value = false;
  emit("send", { node: props.node, text: text.value.trim() });
}

function focusBox() {
  nextTick(() => box.value?.querySelector("textarea")?.focus());
}

defineExpose({ focusBox });
</script>

<template>
  <div ref="box" class="prompt-box nodrag" @click.stop @pointerdown.stop>
    <div class="prompt-chips">
      <button class="chip" type="button">＋ 参考</button>
      <button class="chip" type="button">◷ 标记</button>
      <button class="chip" type="button">❖ 风格</button>
      <span class="grow"></span>
      <button class="chip ghost" type="button" title="展开">⤢</button>
    </div>

    <textarea
      v-model="text"
      rows="3"
      :placeholder="placeholder"
      spellcheck="false"
      @input="onInput"
      @keydown.enter.exact.prevent="send"
    ></textarea>

    <div class="prompt-foot">
      <span class="model-chip">❖ {{ node.model || "默认模型" }}</span>
      <span class="sep"></span>
      <span class="size-chip">{{ node.size || "—" }}</span>
      <span class="grow"></span>
      <span class="prompt-icons"><b>{{ node.textCount || 0 }}</b></span>
      <button class="send" :disabled="!canSend" title="发送（Enter）" @click="send">↑</button>
    </div>
  </div>
</template>
