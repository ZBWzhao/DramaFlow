<script setup>
/* Agent 会话浮层
 *
 * 新画布（spec v2）里它是「浮在画布右侧的一层」，不占布局：
 *   宽 398 / 右 20 / 上 88 / 下 96 / r16 / --bg-s1 + 大投影。
 * 旧画布（?legacy=1）保留原来的可拖拽、可停靠悬浮窗，避免动到冻结的旧界面。
 */
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import ChatPanel from "./ChatPanel.vue";
import { actions, state } from "../store.js";

const props = defineProps({
  variant: { type: String, default: "cv2" },
});

const visible = defineModel({ type: Boolean, default: true });
const minimized = ref(false);
const maximized = ref(false);
const chatRef = ref(null);

const isCv2 = computed(() => props.variant !== "legacy");

/* 旧画布：可拖拽 / 可停靠的悬浮窗（保持原行为） */
const docked = ref(false);
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
  if (isCv2.value) return null;
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

watch(maximized, (on) => {
  if (on) minimized.value = false;
});

function clampPanel() {
  const width = Math.min(panel.width, viewport.width - 24);
  const height = minimized.value ? 52 : Math.min(panel.height, viewport.height - 24);
  panel.left = Math.max(12, Math.min(panel.left, viewport.width - width - 12));
  panel.top = Math.max(12, Math.min(panel.top, viewport.height - height - 12));
}

function beginDrag(event) {
  if (isCv2.value || docked.value || event.button !== 0 || event.target.closest("button")) return;
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

function newSession() {
  chatRef.value?.newSession?.();
}

function chooseSession(sessionId) {
  if (!sessionId || sessionId === state.sessionId) return;
  chatRef.value?.chooseSession?.(sessionId);
}

function clearFeed() {
  actions.clearFeed();
}

function toggleMaximize() {
  maximized.value = !maximized.value;
  if (maximized.value) minimized.value = false;
}

function toggleMinimize() {
  minimized.value = !minimized.value;
  if (minimized.value) maximized.value = false;
}
</script>

<template>
  <button
    v-if="!visible"
    class="agent-launcher"
    :class="{ 'cv2-agent-launcher': isCv2 }"
    aria-label="打开 Agent"
    title="打开 Agent"
    @click="visible = true"
  >
    <span class="agent-orb">AI</span>
  </button>

  <aside
    v-show="visible"
    class="floating-agent"
    :class="{ 'cv2-agent': isCv2, docked, minimized, maximized, dragging: !!drag }"
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
        <template v-if="isCv2">
          <button class="agent-icon-btn" type="button" title="新会话" aria-label="新会话" @click="newSession">
            <el-icon><Plus /></el-icon>
          </button>
          <el-popover placement="bottom-end" :width="252" trigger="click" popper-class="cv2-agent-pop">
            <template #reference>
              <button class="agent-icon-btn" type="button" title="会话记录" aria-label="会话记录">
                <el-icon><Clock /></el-icon>
              </button>
            </template>
            <div class="cv2-agent-sessions">
              <button
                v-for="session in state.sessions"
                :key="session.id"
                type="button"
                class="cv2-agent-session"
                :class="{ on: session.id === state.sessionId }"
                @click="chooseSession(session.id)"
              >
                <span class="s-title">{{ session.title }}</span>
                <span v-if="session.messages" class="s-meta">{{ session.messages }} 条</span>
              </button>
              <p v-if="!state.sessions.length" class="cv2-agent-session-empty">还没有历史会话</p>
            </div>
          </el-popover>
          <button class="agent-icon-btn" type="button" title="清屏" aria-label="清屏" @click="clearFeed">
            <el-icon><Delete /></el-icon>
          </button>
          <button
            class="agent-icon-btn"
            type="button"
            :title="maximized ? '还原大小' : '放大'"
            :aria-label="maximized ? '还原大小' : '放大'"
            @click="toggleMaximize"
          >
            <el-icon><FullScreen /></el-icon>
          </button>
          <button class="agent-icon-btn" type="button" title="最小化" aria-label="最小化" @click="toggleMinimize">
            <el-icon><Minus /></el-icon>
          </button>
          <button class="agent-icon-btn" type="button" title="关闭" aria-label="关闭" @click="visible = false">
            <el-icon><Close /></el-icon>
          </button>
        </template>
        <template v-else>
          <el-button text circle :title="docked ? '切换为悬浮窗' : '停靠到右侧'" @click="docked = !docked">
            <el-icon><Switch /></el-icon>
          </el-button>
          <el-button text circle :title="minimized ? '展开' : '最小化'" @click="toggleMinimize">
            <el-icon><Minus /></el-icon>
          </el-button>
          <el-button text circle title="关闭" @click="visible = false">
            <el-icon><Close /></el-icon>
          </el-button>
        </template>
      </div>
    </header>

    <div v-show="!minimized" class="floating-agent-body">
      <ChatPanel ref="chatRef" :popper-class="isCv2 ? 'cv2-agent-pop' : ''" />
    </div>
  </aside>
</template>
