<script setup>
/* 用户小说气泡（对话流第一条，靠右）：全文默认折 6 行，超长时给「展开全文/收起」文字开关。
 * 气泡规格（spec §3）：padding 9px 13px、圆角 10px（贴头像的右上侧 4px）、max-width 68%、
 * color-mix(in srgb, var(--accent) 12%, var(--card))、--fs-13 / 1.65。
 * 本组件只渲染气泡本体；头像与行排布由容器（RunChat）负责。 */
import { nextTick, onMounted, ref, watch } from "vue";

const props = defineProps({ text: { type: String, default: "" } });

const bodyEl = ref(null);
const open = ref(false);
const overflow = ref(false); /* 内容确实超过 6 行才显示开关，短文不出现多余控件 */

function measure() {
  const el = bodyEl.value;
  overflow.value = !!el && el.scrollHeight > el.clientHeight + 1;
}

onMounted(measure);
watch(() => props.text, () => nextTick(measure));
</script>

<template>
  <div class="bubble">
    <p ref="bodyEl" class="body" :class="{ open }">{{ text }}</p>
    <button
      v-if="overflow"
      class="toggle"
      type="button"
      @click="open = !open"
    >{{ open ? "收起" : "展开全文" }}</button>
  </div>
</template>

<style scoped>
.bubble {
  align-self: flex-end;
  max-width: 68%;
  padding: 9px 13px;
  border-radius: 10px 4px 10px 10px; /* 右上小角贴「你」头像 */
  background: color-mix(in srgb, var(--accent) 12%, var(--card));
  color: var(--text);
  font-size: var(--fs-13);
  line-height: 1.65;
}
.body {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 6;
  overflow: hidden;
}
.body.open {
  display: block;
  -webkit-line-clamp: unset;
}
.toggle {
  display: inline-block;
  margin-top: 2px;
  padding: 0;
  background: none;
  border: none;
  color: var(--accent);
  font-size: var(--fs-12);
  font-family: inherit;
  line-height: 1.6;
}
.toggle:hover { color: var(--accent-hi); }
</style>
