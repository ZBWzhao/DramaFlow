<script setup>
import { computed, onMounted, ref } from "vue";
import { EDGES, NODES } from "./data.js";
import { CARD_W, cardHeight, clamp, edgePath, nodeAnchor } from "./geometry.js";
import TopBar from "./components/TopBar.vue";
import SidebarPanel from "./components/SidebarPanel.vue";
import NodeCard from "./components/NodeCard.vue";
import DockBar from "./components/DockBar.vue";
import HeroPage from "./components/HeroPage.vue";

const nodes = ref(NODES.map((item) => ({ ...item })));
const selectedId = ref(null);
const tab = ref("canvas");
const keyword = ref("");
const empty = ref(false);
const view = ref({ x: 0, y: 0, z: 0.62 });
const toast = ref("");
const credits = ref(215);

const stageRef = ref(null);
const pan = ref(null);
let toastTimer = 0;

const edges = computed(() => {
  const byId = Object.fromEntries(nodes.value.map((n) => [n.id, n]));
  return EDGES.filter(([a, b]) => byId[a] && byId[b]).map(([a, b]) =>
    edgePath(nodeAnchor(byId[a], "right"), nodeAnchor(byId[b], "left")),
  );
});

function showToast(text) {
  toast.value = text;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    toast.value = "";
  }, 2200);
}

function stageRect() {
  return stageRef.value.getBoundingClientRect();
}

function contentBox() {
  return {
    maxX: Math.max(...nodes.value.map((n) => n.x + CARD_W)),
    maxY: Math.max(...nodes.value.map((n) => n.y + cardHeight(n))),
  };
}

function fit() {
  const rect = stageRect();
  const { maxX, maxY } = contentBox();
  const z = clamp(Math.min(rect.width / (maxX + 160), rect.height / (maxY + 170)), 0.35, 1.05);
  view.value = { x: (rect.width - maxX * z) / 2, y: (rect.height - maxY * z) / 2, z };
}

onMounted(() => {
  fit();
  requestAnimationFrame(fit);
  window.setTimeout(fit, 150);
});

function toFlow(clientX, clientY) {
  const rect = stageRect();
  return {
    x: (clientX - rect.left - view.value.x) / view.value.z,
    y: (clientY - rect.top - view.value.y) / view.value.z,
  };
}

function onPointerDown(event) {
  if (event.button !== 0) return;
  if (event.target.closest?.(".node")) return;
  pan.value = {
    sx: event.clientX,
    sy: event.clientY,
    vx: view.value.x,
    vy: view.value.y,
  };
  event.currentTarget.setPointerCapture?.(event.pointerId);
}

function onPointerMove(event) {
  if (!pan.value) return;
  const { sx, sy, vx, vy } = pan.value;
  view.value = { ...view.value, x: vx + (event.clientX - sx), y: vy + (event.clientY - sy) };
}

function onPointerUp() {
  pan.value = null;
}

function onWheel(event) {
  const rect = stageRect();
  const px = event.clientX - rect.left;
  const py = event.clientY - rect.top;
  const z = clamp(view.value.z * Math.exp(-event.deltaY * 0.0016), 0.15, 2.2);
  const k = z / view.value.z;
  view.value = {
    z,
    x: px - (px - view.value.x) * k,
    y: py - (py - view.value.y) * k,
  };
}

function zoomBy(factor) {
  const rect = stageRect();
  const px = rect.width / 2;
  const py = rect.height / 2;
  const z = clamp(view.value.z * factor, 0.15, 2.2);
  const k = z / view.value.z;
  view.value = {
    z,
    x: px - (px - view.value.x) * k,
    y: py - (py - view.value.y) * k,
  };
}

function onStageClick(event) {
  if (event.target.closest?.(".node")) return;
  selectedId.value = null;
}

function onStageDoubleClick(event) {
  if (event.target.closest?.(".node")) return;
  addNode(event.clientX, event.clientY);
}

function addNode(clientX, clientY) {
  const rect = stageRect();
  const p = toFlow(
    clientX ?? rect.left + rect.width / 2,
    clientY ?? rect.top + rect.height / 2,
  );
  const id = "n" + Math.random().toString(36).slice(2, 7);
  const genCount = nodes.value.filter((n) => n.kind === "gen").length;
  nodes.value = [
    ...nodes.value,
    {
      id,
      kind: "gen",
      typeLabel: "图片生成",
      title: "图片节点 " + (genCount + 1),
      dims: "2048 × 1152",
      x: Math.round(p.x - CARD_W / 2),
      y: Math.round(p.y - 100),
      attempts: ["图生图", "图片高清"],
      prompt: "",
      model: "Lib Image 2.5 Pro",
      size: "16:9 · 标准画质 · 2K · 1张",
    },
  ];
  selectedId.value = id;
  empty.value = false;
}

function pickSide(id) {
  selectedId.value = id;
  const node = nodes.value.find((n) => n.id === id);
  if (!node) return;
  const rect = stageRect();
  view.value = {
    ...view.value,
    x: rect.width / 2 - (node.x + CARD_W / 2) * view.value.z,
    y: rect.height / 2 - (node.y + cardHeight(node) / 2) * view.value.z,
  };
}

function send(node) {
  credits.value = Math.max(0, credits.value - 1);
  showToast(`已提交「${node.title}」`);
}

function toggleEmpty() {
  empty.value = !empty.value;
}

function heroPick() {
  empty.value = false;
  addNode();
}
</script>

<template>
  <div class="app">
    <TopBar :credits="credits" @toggle-empty="toggleEmpty" />

    <SidebarPanel
      v-model:tab="tab"
      v-model:keyword="keyword"
      :selected-id="selectedId"
      :total="empty ? 0 : nodes.length"
      :hidden="empty"
      @pick="pickSide"
    />

    <main
      ref="stageRef"
      class="stage"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @wheel.prevent="onWheel"
      @click="onStageClick"
      @dblclick="onStageDoubleClick"
    >
      <div
        class="grid-bg"
        :style="{
          backgroundSize: `${22 * view.z}px ${22 * view.z}px`,
          backgroundPosition: `${view.x}px ${view.y}px`,
        }"
      ></div>

      <div
        v-if="!empty"
        class="world"
        :style="{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.z})` }"
      >
        <svg class="edges" :width="3600" :height="2400">
          <path v-for="(d, index) in edges" :key="index" :d="d" />
        </svg>
        <NodeCard
          v-for="node in nodes"
          :key="node.id"
          :node="node"
          :selected="node.id === selectedId"
          @select="selectedId = $event"
          @send="send"
        />
      </div>

      <HeroPage v-if="empty" @pick="heroPick" />

      <div class="zoom-pill">
        <button @click="zoomBy(1.2)">＋</button>
        <button @click="zoomBy(1 / 1.2)">−</button>
        <span>{{ Math.round(view.z * 100) }}%</span>
        <button class="fit" title="适应窗口" @click="fit">⤢</button>
      </div>

      <DockBar :empty="empty" @add="addNode()" @toggle-empty="toggleEmpty" />
      <div class="count-pill">共 {{ empty ? 0 : nodes.length }} 节点</div>
    </main>

    <div v-if="toast" class="toast">{{ toast }}</div>
  </div>
</template>
