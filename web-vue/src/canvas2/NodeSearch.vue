<script setup>
/* 命令面板式节点搜索：Ctrl/Cmd + K 打开，输入标题/类型过滤，↑↓ 选择，回车聚焦。
 * 数据直接用画布上的节点，不额外请求。 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

const props = defineProps({
  open: { type: Boolean, default: false },
  nodes: { type: Array, default: () => [] }, // [{ id, title, tag, icon }]
});
const emit = defineEmits(["close", "pick"]);

const keyword = ref("");
const active = ref(0);
const inputEl = ref(null);

const results = computed(() => {
  const q = keyword.value.trim().toLowerCase();
  const list = !q
    ? props.nodes
    : props.nodes.filter((node) => `${node.title} ${node.tag}`.toLowerCase().includes(q));
  return list.slice(0, 40);
});

watch(results, () => { active.value = 0; });
watch(() => props.open, async (open) => {
  if (!open) return;
  keyword.value = "";
  active.value = 0;
  await nextTick();
  inputEl.value?.focus();
});

function move(step) {
  const total = results.value.length;
  if (!total) return;
  active.value = (active.value + step + total) % total;
}

function pick(node) {
  if (!node) return;
  emit("pick", node.id);
  emit("close");
}

function onKeydown(event) {
  if (event.key === "ArrowDown") { event.preventDefault(); move(1); }
  else if (event.key === "ArrowUp") { event.preventDefault(); move(-1); }
  else if (event.key === "Enter") { event.preventDefault(); pick(results.value[active.value]); }
  else if (event.key === "Escape") { event.preventDefault(); emit("close"); }
}

function onGlobalKey(event) {
  if (event.key === "Escape" && props.open) emit("close");
}

onMounted(() => window.addEventListener("keydown", onGlobalKey));
onBeforeUnmount(() => window.removeEventListener("keydown", onGlobalKey));
</script>

<template>
  <div v-if="open" class="search-backdrop" @pointerdown.self="emit('close')">
    <div class="search-panel" role="dialog" aria-label="搜索节点">
      <div class="search-input-row">
        <span class="search-icon" aria-hidden="true">⌕</span>
        <input
          ref="inputEl"
          v-model="keyword"
          class="search-input"
          type="text"
          placeholder="搜索节点：标题或类型，回车聚焦"
          spellcheck="false"
          @keydown="onKeydown"
        />
        <span class="search-esc">Esc</span>
      </div>
      <ul v-if="results.length" class="search-list">
        <li
          v-for="(node, index) in results"
          :key="node.id"
          :class="{ active: index === active }"
          @pointerenter="active = index"
          @click="pick(node)"
        >
          <span class="search-glyph">{{ node.icon }}</span>
          <span class="search-title">{{ node.title }}</span>
          <span class="search-tag">{{ node.tag }}</span>
        </li>
      </ul>
      <p v-else class="search-empty">没有匹配的节点</p>
    </div>
  </div>
</template>
