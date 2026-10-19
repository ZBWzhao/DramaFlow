<script setup>
/* B 版卡片：卡外标题 + 16:9 卡体 + 选中时的工具条/提示词框 + 「拖动整卡」手势 + 右侧连接点拖线。
 * 与 demo 的差别：图片来自真实 assets、工具条按钮接真实动作、卡片可拖（写回 position）、
 * 右边 ⊕ 可以按住往外拖出一条新连线（由 CanvasBoard 负责落点判定与写库）。
 */
import { computed, onBeforeUnmount, ref } from "vue";
import { CARD_W } from "./geometry.js";
import NodeToolbar from "./NodeToolbar.vue";
import PromptBox from "./PromptBox.vue";
import VideoPlayer from "./VideoPlayer.vue";

const props = defineProps({
  node: { type: Object, required: true },
  selected: { type: Boolean, default: false },
  inSelection: { type: Boolean, default: false },
  zoom: { type: Number, default: 1 },
  // 多选拖动时，非主卡由画板下发"跟着主卡位移"的预览偏移（松手前不落库）
  preview: { type: Object, default: null },
});
const emit = defineEmits([
  "select", "send", "change", "move", "drag", "generate", "upload", "history", "detail", "edge-start",
  "open-video", "retry",
]);

const drag = ref(null);
const offset = ref({ x: 0, y: 0 });

const shownOffset = computed(() => (drag.value ? offset.value : props.preview || { x: 0, y: 0 }));
const left = computed(() => props.node.x + (shownOffset.value.x || 0));
const top = computed(() => props.node.y + (shownOffset.value.y || 0));

function isInteractive(target) {
  return Boolean(
    target?.closest?.("textarea, input, button, .node-menu, .handle, .prompt-box, .node-toolbar"),
  );
}

function onPointerDown(event) {
  if (event.button !== 0 || isInteractive(event.target)) return;
  emit("select", props.node.id);
  drag.value = { sx: event.clientX, sy: event.clientY, moved: false };
  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", onPointerUp, { once: true });
}

function onPointerMove(event) {
  if (!drag.value) return;
  const z = props.zoom || 1;
  const dx = (event.clientX - drag.value.sx) / z;
  const dy = (event.clientY - drag.value.sy) / z;
  if (Math.abs(dx) > 2 || Math.abs(dy) > 2) drag.value.moved = true;
  offset.value = { x: dx, y: dy };
  // 通知画板：同组卡片跟着移动（松手前不写库，只做视觉效果）
  if (drag.value.moved) emit("drag", { id: props.node.id, dx, dy });
}

function onPointerUp() {
  window.removeEventListener("pointermove", onPointerMove);
  if (drag.value?.moved) {
    emit("move", {
      id: props.node.id,
      x: Math.round(props.node.x + offset.value.x),
      y: Math.round(props.node.y + offset.value.y),
      dx: offset.value.x,
      dy: offset.value.y,
    });
  }
  drag.value = null;
  offset.value = { x: 0, y: 0 };
}

// 拖到一半组件被卸载（画布切换/节点被删）时，别把 window 监听留在外面
onBeforeUnmount(() => {
  window.removeEventListener("pointermove", onPointerMove);
  window.removeEventListener("pointerup", onPointerUp);
  drag.value = null;
});

/* 右侧连接点：按住就能拖出一条线，落点由画板判定 */
function onEdgeStart(event) {
  if (event.button !== 0) return;
  event.stopPropagation();
  emit("edge-start", { id: props.node.id, clientX: event.clientX, clientY: event.clientY });
}

/* 视频大图：卡太小看不清表演，交给画板统一开灯箱 */
function onOpenVideo() {
  emit("open-video", {
    id: props.node.id,
    src: props.node.src,
    title: props.node.title,
    badge: props.node.badge,
    duration: props.node.durationSeconds || 0,
  });
}
</script>

<template>
  <div
    class="node"
    :data-id="node.id"
    :class="{
      selected,
      'in-selection': inSelection && !selected,
      plain: node.plain,
      dragging: !!drag,
      'gen-busy': node.busy,
      'gen-failed': node.failed,
      'gen-done': node.justDone,
    }"
    :style="{ left: left + 'px', top: top + 'px', width: CARD_W + 'px' }"
    @pointerdown="onPointerDown"
    @click.stop="emit('select', node.id)"
    @contextmenu.prevent.stop="emit('context-menu', { node, clientX: $event.clientX, clientY: $event.clientY })"
  >
    <!-- 聚焦闪一下：左侧列表/搜索跳过来时告诉人「是这一张」 -->
    <span v-if="node.flash" class="focus-flash" aria-hidden="true"></span>
    <NodeToolbar
      v-if="selected"
      :node="node"
      @generate="emit('generate', $event)"
      @upload="emit('upload', $event)"
      @history="emit('history', $event)"
      @detail="emit('detail', $event)"
    />

    <div class="node-caption">
      <span class="cap-glyph">{{ node.glyph || "▣" }}</span>
      <span class="cap-title">{{ node.title }}</span>
      <span v-if="node.dims" class="cap-dims">{{ node.dims }}</span>
    </div>

    <div class="node-body" :class="{ 'is-text': node.plain, 'is-video': node.video }">
      <template v-if="node.plain">
        <p class="node-text">{{ node.prompt }}</p>
      </template>
      <template v-else>
        <VideoPlayer
          v-if="node.video && node.src"
          :src="node.src"
          :title="node.title"
          :duration="node.durationSeconds || 0"
          :enabled="zoom >= 0.45"
          @open="onOpenVideo"
        />
        <img v-else-if="node.img" :src="node.img" :alt="node.title" draggable="false" />
        <span v-else class="node-empty-icon">{{ node.glyph || "▣" }}</span>
        <span v-if="node.badge" class="ai-tag">{{ node.badge }}</span>

        <!-- 生成状态遮罩：服务端状态 + 本地"已提交"共同驱动；失败给一键重试 -->
        <div v-if="node.busy" class="gen-overlay" aria-live="polite">
          <span class="gen-spinner" aria-hidden="true"></span>
          <span class="gen-text">{{ node.busyLabel || "生成中…" }}</span>
        </div>
        <div v-else-if="node.failed" class="gen-overlay is-error">
          <span class="gen-fail-icon" aria-hidden="true">!</span>
          <span class="gen-text">生成失败</span>
          <button type="button" class="gen-retry" @click.stop="emit('retry', node)">重试</button>
        </div>

        <div v-if="!node.video && node.attempts" class="attempts">
          <span class="attempts-title">尝试：</span>
          <button
            v-for="label in node.attempts"
            :key="label"
            class="attempt"
            type="button"
            @click.stop="emit('select', node.id)"
          >
            ⬆ {{ label }}
          </button>
        </div>

        <span class="handle left" title="输入" />
        <span class="handle right" title="按住拖出一条线" @pointerdown="onEdgeStart" />
        <span class="node-menu" title="更多" @click.stop="emit('detail', node)">⋯</span>
      </template>
    </div>

    <PromptBox
      v-if="selected"
      :node="node"
      @send="emit('send', $event)"
      @change="emit('change', $event)"
    />
  </div>
</template>
