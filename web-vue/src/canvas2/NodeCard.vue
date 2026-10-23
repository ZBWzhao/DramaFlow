<script setup>
/* V2 卡片：媒体层占满整卡 + 卡外标签行（14 图标 / 12 名称 / 11 分辨率）。
 *
 * 与 V1 的差别：
 *  - 卡体不再「空壳」：图片/视频/文本/空态/生成中/失败都是同一块媒体层的不同状态；
 *  - 宽高比固定（媒体 16:9、文本 3:2），宽度取 300/360/420 三档（geometry.js）；
 *  - 提示词输入框从这个组件里移走了（HUD 检查器，见 CardInspector.vue）——
 *    它以前渲染在 transform:scale 的容器里，缩放时会变形；
 *  - 工具条也移走（检查器底部参数条承担生成/上传/历史/详情）。
 */
import { computed, onBeforeUnmount, ref } from "vue";
import { CAPTION_GAP, CAPTION_H, bodyHeight, cardWidth, portOffset } from "./geometry.js";
import VideoPlayer from "./VideoPlayer.vue";
import { progressDash, progressText } from "./progress.js";

const props = defineProps({
  node: { type: Object, required: true },
  selected: { type: Boolean, default: false },
  inSelection: { type: Boolean, default: false },
  zoom: { type: Number, default: 1 },
  // 多选拖动时，非主卡由画板下发"跟着主卡位移"的预览偏移（松手前不落库）
  preview: { type: Object, default: null },
  // 服务端 node_spec 里的类型化端口：[[name, STRING|IMAGE|VIDEO|AUDIO], ...]
  inPorts: { type: Array, default: () => [] },
  outPorts: { type: Array, default: () => [] },
  // 连线交互：这张卡能否作为当前连线的落点 / 是否正在「点击式连线」模式
  connectable: { type: Boolean, default: false },
  connectMode: { type: Boolean, default: false },
  // Agent 共创可视化：born=AI 刚建（落卡动画） / touched=AI 刚改（金圈脉冲）
  aiMark: { type: String, default: "" },
  // 影响传播：这张卡在选中卡的下游依赖里
  affected: { type: Boolean, default: false },
  // 空间过滤：不符合当前过滤器，只变暗
  dimmed: { type: Boolean, default: false },
});
const emit = defineEmits([
  "select", "move", "drag", "edge-start", "open-video", "retry", "context-menu", "detail",
]);

function portTop(index, count) {
  return portOffset(props.node, index, count);
}
const portOffsetFor = portOffset;

function onEdgeStartFrom(event, handle, portType) {
  emit("edge-start", { id: props.node.id, handle, ptype: portType, clientX: event.clientX, clientY: event.clientY });
}

const drag = ref(null);
const offset = ref({ x: 0, y: 0 });

/* 状态机（只读投影）：形状先于颜色，灰度下也要能认出「排队 / 生成中 / 成功 / 失败」 */
const statusKind = computed(() => {
  const node = props.node;
  if (node.failed || node.status === "error") return "error";
  if (node.status === "discarded") return "discarded";
  if (node.status === "unknown") return "unknown";
  if (node.busy) return node.status === "generating" ? "generating" : "queued";
  if (node.status === "done") return "done";
  if (node.status === "ready") return "ready";
  return "draft";
});

const mediaState = computed(() => {
  if (statusKind.value === "error" || props.node.busy) return "status";
  if (props.node.plain) return "text";
  if (props.node.video && props.node.src) return "video";
  if (props.node.img) return "image";
  return "empty";
});

const ring = computed(() => progressDash(props.node.progress, 16));
const stepText = computed(() => progressText(props.node.progress));
const bodyH = computed(() => props.node.cardBodyH || bodyHeight(props.node));
const cardW = computed(() => props.node.cardW || cardWidth(props.node));

const shownOffset = computed(() => (drag.value ? offset.value : props.preview || { x: 0, y: 0 }));
const left = computed(() => props.node.x + (shownOffset.value.x || 0));
const top = computed(() => props.node.y + (shownOffset.value.y || 0));

const failReason = computed(() => {
  const data = props.node.raw?.data || {};
  return String(data.generation_error || data.error || "").trim();
});

function isInteractive(target) {
  if (target?.closest?.(".handle.move")) return false; // 上下抓手就是用来拖卡的
  return Boolean(target?.closest?.("button, .node-menu, .handle, input, textarea"));
}

function onPointerDown(event) {
  if (event.button !== 0 || isInteractive(event.target)) return;
  emit("select", props.node.id);
  if (props.connectMode) return;
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

onBeforeUnmount(() => {
  window.removeEventListener("pointermove", onPointerMove);
  window.removeEventListener("pointerup", onPointerUp);
  drag.value = null;
});

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
    :data-type="node.type"
    :class="{
      selected,
      'in-selection': inSelection && !selected,
      dragging: !!drag,
      'gen-busy': node.busy,
      'gen-failed': node.failed,
      'gen-done': node.justDone,
      'is-fail': statusKind === 'error',
      connectable,
      'ai-born': aiMark === 'born',
      'ai-touched': aiMark === 'touched',
      'is-affected': affected && !selected,
      dimmed,
      ['status-' + statusKind]: !!statusKind,
    }"
    :style="{ left: left + 'px', top: top + 'px', width: cardW + 'px' }"
    @pointerdown="onPointerDown"
    @click.stop="emit('select', node.id)"
    @dblclick.stop="mediaState === 'video' ? onOpenVideo() : emit('select', node.id)"
    @contextmenu.prevent.stop="emit('context-menu', { node, clientX: $event.clientX, clientY: $event.clientY })"
  >
    <span v-if="node.flash" class="focus-flash" aria-hidden="true"></span>

    <!-- 卡外标签行：类型图标 14 + 名称 12 + 右对齐分辨率 11（tabular） -->
    <div class="node-head">
      <span class="nh-ic" aria-hidden="true">{{ node.glyph || "▣" }}</span>
      <span class="nh-nm">{{ node.title }}</span>
      <span class="nh-res">{{ node.res || node.dims || "" }}</span>
    </div>

    <!-- 卡体：媒体层占满整卡 -->
    <div class="node-card" :style="{ height: bodyH + 'px' }">
      <div class="node-media" :class="'media-' + mediaState">
        <template v-if="mediaState === 'text'">
          <div class="media-text">{{ node.prompt }}</div>
        </template>

        <template v-else-if="mediaState === 'video'">
          <VideoPlayer
            :src="node.src"
            :title="node.title"
            :duration="node.durationSeconds || 0"
            :enabled="zoom >= 0.45"
            @open="onOpenVideo"
          />
          <span v-if="node.durationSeconds" class="badge">{{ node.durationText }}</span>
        </template>

        <template v-else-if="mediaState === 'image'">
          <img :src="node.img" :alt="node.title" draggable="false" />
        </template>

        <template v-else-if="mediaState === 'empty'">
          <div class="media-empty">
            <div class="slot" aria-hidden="true">⤒</div>
            <div class="act">{{ node.canUpload ? "上传 / 拖入图片" : "等待上游素材" }}</div>
          </div>
        </template>

        <template v-else-if="node.busy">
          <div class="media-progress" aria-live="polite">
            <svg class="ring" viewBox="0 0 40 40" aria-hidden="true">
              <circle class="ring-track" cx="20" cy="20" r="16" />
              <circle
                class="ring-bar"
                cx="20"
                cy="20"
                r="16"
                :stroke-dasharray="ring.circumference"
                :stroke-dashoffset="ring.offset"
              />
            </svg>
            <div class="t1">{{ node.progress?.label || node.busyLabel || "生成中" }}</div>
            <div class="t2">{{ stepText || "排队中" }}</div>
          </div>
        </template>

        <template v-else-if="statusKind === 'discarded'">
          <div class="media-fail is-muted">
            <div class="t1">结果已过期，未写入画布</div>
          </div>
        </template>

        <template v-else-if="statusKind === 'unknown'">
          <div class="media-fail is-muted">
            <div class="t1">结果不确定，待人工核对</div>
          </div>
        </template>

        <template v-else>
          <div class="media-fail">
            <div class="t1">生成失败{{ failReason ? ` · ${failReason}` : "" }}</div>
            <button type="button" class="retry" @click.stop="emit('retry', node)">重试</button>
          </div>
        </template>

        <!-- 角标：已生成（右上，文字 + 形状可辨）/ 来源（左下） -->
        <span v-if="statusKind === 'done' && !node.busy" class="tag-ok">已生成</span>
        <span v-if="node.badge && mediaState === 'image'" class="tag-src">{{ node.badge }}</span>
        <span class="node-menu" title="详情" @click.stop="emit('detail', node)">⋯</span>
      </div>
    </div>

    <!-- 端口锚点（卡体外，避免被 overflow 裁掉）：左右是真实类型化端口，上下是拖动抓手 -->
    <span
      v-for="([portName, portType], index) in inPorts"
      :key="'in-' + portName"
      class="handle left"
      :style="{ top: portOffsetFor(index, inPorts.length) + 'px' }"
      :title="`输入 · ${portName} (${portType})`"
    />
    <span
      v-for="([portName, portType], index) in outPorts"
      :key="'out-' + portName"
      class="handle right"
      :style="{ top: portOffsetFor(index, outPorts.length) + 'px' }"
      :title="`点击选择起点，或按住拖到目标卡 · ${portName} (${portType})`"
      @pointerdown.stop="onEdgeStartFrom($event, portName, portType)"
    />
    <span v-if="selected" class="handle move top" title="拖动卡片" aria-hidden="true" />
    <span v-if="selected" class="handle move bottom" title="拖动卡片" aria-hidden="true" />
  </div>
</template>
