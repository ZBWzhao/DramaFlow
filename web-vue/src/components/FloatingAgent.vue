<script setup>
import { computed, onBeforeUnmount, reactive, ref } from "vue";
import ChatPanel from "./ChatPanel.vue";
import { state } from "../store.js";

const visible = defineModel({ type: Boolean, default: true });
const docked = ref(false);
const minimized = ref(false);
const panel = reactive({
  left: Math.max(16, window.innerWidth - 456),
  top: 72,
  width: 420,
  height: Math.min(760, window.innerHeight - 96),
});
const viewport = reactive({ width: window.innerWidth, height: window.innerHeight });
let drag = null;

const statusText = computed(() => {
  if (state.running) return state.activity || "运行中";
  if (state.endReason === "limited") return "已暂停，可继续";
  if (state.status === "error") return "需要检查";
  return "空闲";
});

const panelStyle = computed(() => {
  if (docked.value) {
    return {
      left: `${Math.max(0, viewport.width - Math.min(panel.width, viewport.width))}px`,
      top: "0px",
      width: `${Math.min(panel.width, viewport.width)}px`,
      height: `${viewport.height}px`,
    };
  }
  return {
    left: `${panel.left}px`,
    top: `${panel.top}px`,
    width: `${Math.min(panel.width, viewport.width - 24)}px`,
    height: minimized.value ? "52px" : `${Math.min(panel.height, viewport.height - 24)}px`,
  };
});

function clampPanel() {
  const width = Math.min(panel.width, viewport.width - 24);
  const height = minimized.value ? 52 : Math.min(panel.height, viewport.height - 24);
  panel.left = Math.max(12, Math.min(panel.left, viewport.width - width - 12));
  panel.top = Math.max(12, Math.min(panel.top, viewport.height - height - 12));
}

function beginDrag(event) {
  if (docked.value || event.button !== 0 || event.target.closest("button")) return;
  drag = { x: event.clientX, y: event.clientY, left: panel.left, top: panel.top };
  event.currentTarget.setPointerCapture(event.pointerId);
}

function moveDrag(event) {
  if (!drag) return;
  panel.left = drag.left + event.clientX - drag.x;
  panel.top = drag.top + event.clientY - drag.y;
  clampPanel();
}

function endDrag() {
  drag = null;
}

function resizeViewport() {
  viewport.width = window.innerWidth;
  viewport.height = window.innerHeight;
  clampPanel();
}

window.addEventListener("resize", resizeViewport);
onBeforeUnmount(() => window.removeEventListener("resize", resizeViewport));
</script>

<template>
  <button v-if="!visible" class="agent-launcher" aria-label="打开 Agent" title="打开 Agent" @click="visible = true">
    <span class="agent-orb">AI</span>
  </button>

  <aside
    v-show="visible"
    class="floating-agent"
    :class="{ docked, minimized, dragging: !!drag }"
    :style="panelStyle"
    aria-label="Agent 对话"
  >
    <header
      class="floating-agent-head"
      @pointerdown="beginDrag"
      @pointermove="moveDrag"
      @pointerup="endDrag"
      @pointercancel="endDrag"
    >
      <span class="agent-orb">AI</span>
      <div class="agent-title">
        <b>ToonFlow Agent</b>
        <span :class="{ active: state.running }"><i />{{ statusText }}</span>
      </div>
      <span v-if="state.canvasActivity?.summary" class="agent-canvas-state" :title="state.canvasActivity.summary">
        {{ state.canvasActivity.summary }}
      </span>
      <div class="agent-window-actions">
        <el-button text circle :title="docked ? '切换为悬浮窗' : '停靠到右侧'" @click="docked = !docked">
          <el-icon><Switch /></el-icon>
        </el-button>
        <el-button text circle :title="minimized ? '展开' : '最小化'" @click="minimized = !minimized">
          <el-icon><Minus /></el-icon>
        </el-button>
        <el-button text circle title="关闭" @click="visible = false">
          <el-icon><Close /></el-icon>
        </el-button>
      </div>
    </header>

    <div v-show="!minimized" class="floating-agent-body">
      <ChatPanel />
    </div>
  </aside>
</template>
