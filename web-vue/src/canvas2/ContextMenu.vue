<script setup>
/* 画布右键菜单。只放真能执行的动作——没有后端支持的（比如删除节点）就明确置灰并说明原因，
 * 不做「点了没反应」的假菜单。 */
import { onBeforeUnmount, onMounted } from "vue";

const props = defineProps({
  menu: { type: Object, required: true }, // { x, y, title, items: [{ key, label, hint?, disabled? }] }
});
const emit = defineEmits(["select", "close"]);

function onClick(item) {
  if (item.disabled) return;
  emit("select", item.key);
}

function onGlobalPointer(event) {
  if (!event.target.closest?.(".cv2 .context-menu")) emit("close");
}
function onKey(event) {
  if (event.key === "Escape") emit("close");
}

onMounted(() => {
  window.addEventListener("pointerdown", onGlobalPointer, true);
  window.addEventListener("keydown", onKey);
});
onBeforeUnmount(() => {
  window.removeEventListener("pointerdown", onGlobalPointer, true);
  window.removeEventListener("keydown", onKey);
});
</script>

<template>
  <div
    class="context-menu"
    role="menu"
    :style="{ left: menu.x + 'px', top: menu.y + 'px' }"
    @contextmenu.prevent
  >
    <div v-if="menu.title" class="ctx-title">{{ menu.title }}</div>
    <button
      v-for="item in menu.items"
      :key="item.key"
      type="button"
      class="ctx-item"
      :class="{ disabled: item.disabled }"
      :disabled="item.disabled"
      @click.stop="onClick(item)"
    >
      <span class="ctx-label">{{ item.label }}</span>
      <span v-if="item.hint" class="ctx-hint">{{ item.hint }}</span>
    </button>
  </div>
</template>
