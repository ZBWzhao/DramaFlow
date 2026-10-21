<script setup>
/* 画布左侧列表（照 LibTV 左侧「画布 / 资产」面板的形态）
 *
 * 两件事：
 * 1. 给画布一个"目录"——节点多了以后（我们现在动辄 50+）列表比在画布上找卡片快；
 * 2. 选中即打开工作台，和点画布上的卡片是同一个状态（state.selectedId）。
 *
 * 注意：这一栏只是索引入口，真正的编辑仍然在卡片本身与工作台里。
 */
import { computed, ref } from "vue";
import { state } from "../store.js";

const tab = ref("canvas");
const keyword = ref("");

const TYPE_LABEL = {
  prompt: "提示词", text: "文本", storyboard: "分镜", asset: "资产",
  image: "图片", image_gen: "图片生成", video: "视频", video_gen: "视频生成",
  audio: "音频", tts_gen: "旁白", timeline: "成片",
};
const TYPE_GLYPH = {
  prompt: "✎", text: "¶", storyboard: "▤", asset: "◈",
  image: "▣", image_gen: "▣", video: "▷", video_gen: "▷",
  audio: "♪", tts_gen: "♪", timeline: "⧗",
};

const allNodes = computed(() => state.canvas.nodes || []);
const nodes = computed(() => {
  const source = tab.value === "asset"
    ? allNodes.value.filter((node) => node.type === "asset")
    : allNodes.value;
  const needle = keyword.value.trim().toLowerCase();
  const list = needle
    ? source.filter((node) => String(node.label || "").toLowerCase().includes(needle))
    : source;
  return list.slice(0, 200);
});

function select(node) {
  state.selectedId = node.id;
}
</script>

<template>
  <aside class="canvas-side" aria-label="画布目录">
    <div class="canvas-side-tabs">
      <button type="button" :class="{ active: tab === 'canvas' }" @click="tab = 'canvas'">画布</button>
      <button type="button" :class="{ active: tab === 'asset' }" @click="tab = 'asset'">资产</button>
    </div>

    <div class="canvas-side-search">
      <input v-model="keyword" type="search" :placeholder="tab === 'asset' ? '搜索资产' : '搜索节点'" />
    </div>

    <p v-if="!nodes.length" class="canvas-side-empty">
      {{ tab === "asset" ? "还没有资产卡" : "画布暂无节点" }}
    </p>
    <ul v-else class="canvas-side-list">
      <li v-for="node in nodes" :key="node.id">
        <button
          type="button"
          :class="{ active: state.selectedId === node.id }"
          @click="select(node)"
        >
          <span class="side-glyph" aria-hidden="true">{{ TYPE_GLYPH[node.type] || "◇" }}</span>
          <span class="side-label" :title="node.label">{{ node.label || node.id }}</span>
          <span class="side-type">{{ TYPE_LABEL[node.type] || node.type }}</span>
        </button>
      </li>
    </ul>

    <footer class="canvas-side-foot">
      <span>共 {{ allNodes.length }} 节点</span>
    </footer>
  </aside>
</template>
