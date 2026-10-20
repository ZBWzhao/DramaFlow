<script setup>
import { computed } from "vue";
import { SIDEBAR_ITEMS } from "../data.js";

const props = defineProps({
  tab: { type: String, default: "canvas" },
  keyword: { type: String, default: "" },
  selectedId: { type: String, default: null },
  total: { type: Number, default: 0 },
  hidden: { type: Boolean, default: false },
});
const emit = defineEmits(["update:tab", "update:keyword", "pick"]);

const items = computed(() => {
  if (props.hidden) return [];
  const key = props.keyword.trim().toLowerCase();
  return SIDEBAR_ITEMS.filter((item) => {
    if (props.tab === "assets" && item.tag !== "资产") return false;
    if (!key) return true;
    return (item.title + item.tag).toLowerCase().includes(key);
  });
});
</script>

<template>
  <aside class="sidebar">
    <div class="seg">
      <button :class="{ on: tab === 'canvas' }" @click="emit('update:tab', 'canvas')">画布</button>
      <button :class="{ on: tab === 'assets' }" @click="emit('update:tab', 'assets')">资产</button>
    </div>

    <div class="side-search">
      <span>⌕</span>
      <input
        :value="keyword"
        placeholder="搜索节点"
        @input="emit('update:keyword', $event.target.value)"
      />
    </div>

    <div class="side-filter">
      <span>⚗</span>
      <span>所有评级</span>
      <span class="grow"></span>
      <span>☰</span>
    </div>

    <div class="side-list">
      <button
        v-for="item in items"
        :key="item.id"
        class="side-item"
        :class="{ on: item.id === selectedId }"
        @click="emit('pick', item.id)"
      >
        <span class="side-icon">{{ item.icon }}</span>
        <span class="side-title">{{ item.title }}</span>
        <span class="side-tag">{{ item.tag }}</span>
      </button>
      <p v-if="!items.length" class="side-empty">画布暂无节点</p>
    </div>

    <div class="side-foot">共 {{ total }} 节点</div>
  </aside>
</template>
