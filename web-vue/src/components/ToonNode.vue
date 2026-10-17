<script setup>
/* 自定义节点卡：标题在卡外上方、端口默认隐藏（hover 才显）。
 * 正文区可独立滚动，避免长剧本被截断；端口名与类型来自后端返回的 node_spec，
 * 前端不重复维护一份端口定义。
 */
import { computed } from "vue";
import { Handle, Position } from "@vue-flow/core";
import { assetUrl, mediaOf } from "../utils.js";

const props = defineProps({
  data: { type: Object, required: true },
});

const node = computed(() => props.data.node);
const spec = computed(() => props.data.spec || {});
const inPorts = computed(() => Object.entries(spec.value.in || {}));
const outPorts = computed(() => Object.entries(spec.value.out || {}));
const shortType = computed(() => String(spec.value.label || node.value.type).split("（")[0]);
const bodyStyle = computed(() => ({ minHeight: `${Math.max(82, 40 + (Math.max(inPorts.value.length, outPorts.value.length) - 1) * 30)}px` }));

// 产物缩略图：outputs 里任意一个媒体端口有 path 就显示
const media = computed(() => mediaOf(node.value));
const mediaSrc = computed(() => assetUrl(media.value?.path));
const textPreview = computed(() => {
  if (typeof node.value.content === "string" && node.value.content.trim()) return node.value.content.trim();
  const outputs = Object.values(node.value.outputs || {});
  const value = outputs.find((item) => item?.data_type === "STRING" && typeof item.value === "string")?.value;
  return value ? value.trim() : "";
});

// 节点状态是后端的协议值，卡片上用用户能快速理解的生产阶段展示。
// 不修改已有状态协议，避免 Agent、媒体任务和旧画布数据产生兼容问题。
const lifecycleSteps = [
  { key: "draft", label: "创建" },
  { key: "generating", label: "生成" },
  { key: "ready", label: "待确认" },
  { key: "done", label: "完成" },
];
const lifecycleMap = {
  draft: { label: "创建中", index: 0, hint: "节点已创建，等待 Agent 写入内容" },
  generating: { label: "生成中", index: 1, hint: "Agent 或媒体 Provider 正在生成" },
  ready: { label: "待确认", index: 2, hint: "内容已生成，等待检查或继续制作" },
  done: { label: "已完成", index: 3, hint: "该节点的产物已经完成" },
  error: { label: "生成失败", index: -1, hint: "生成失败，可打开详情检查并重试" },
};
const lifecycle = computed(() => lifecycleMap[node.value.status] || lifecycleMap.draft);
const lifecycleClass = computed(() => node.value.status || "draft");
const lifecycleMessage = computed(() => {
  if (node.value.status === "error") return node.value.data?.error || "生成失败，可打开详情检查并重试";
  if (node.value.status === "generating") return textPreview.value ? "正在继续生成，当前内容已保留" : "Agent 正在写入内容";
  if (node.value.status === "draft") return "等待 Agent 写入内容";
  return "";
});

const portTop = (index) => `${22 + index * 30}px`;
const labelTop = (index) => `${19 + index * 30}px`;
</script>

<template>
  <div class="tnode" :class="[`type-${node.type}`, `status-${node.status}`, { 'agent-changed': data.changed }]">
    <div class="tnode-head">
      <span class="tnode-type" :title="spec.label || node.type">{{ shortType }}</span>
      <span class="tnode-status" :class="`lifecycle-${lifecycleClass}`" :title="lifecycle.hint">{{ lifecycle.label }}</span>
    </div>

    <div
      class="tnode-lifecycle"
      :class="`lifecycle-${lifecycleClass}`"
      role="status"
      aria-live="polite"
      :title="lifecycle.hint"
    >
      <div v-if="lifecycleClass !== 'error'" class="tnode-lifecycle-track">
        <span
          v-for="(step, index) in lifecycleSteps"
          :key="step.key"
          class="tnode-lifecycle-segment"
          :class="{ active: index <= lifecycle.index, current: index === lifecycle.index }"
          :title="step.label"
        />
      </div>
      <span v-else class="tnode-lifecycle-error-mark" aria-hidden="true">!</span>
      <span class="tnode-lifecycle-label">{{ lifecycle.label }}</span>
    </div>

    <div class="tnode-body" :style="bodyStyle">
      <div class="tnode-label">{{ node.label }}</div>
      <div v-if="lifecycleMessage" class="tnode-lifecycle-message" :class="`lifecycle-${lifecycleClass}`">
        <span v-if="lifecycleClass === 'generating'" class="tnode-progress-dot" aria-hidden="true" />
        {{ lifecycleMessage }}
      </div>
      <div
        v-if="textPreview"
        class="tnode-preview nowheel nodrag"
        title="滚动查看完整正文；单击卡片可在右侧查看详情"
      >{{ textPreview }}</div>

      <div v-if="media" class="tnode-media">
        <video
          v-if="media.port === 'video'"
          class="nowheel nodrag"
          :src="mediaSrc"
          controls
          preload="metadata"
          @click.stop
        />
        <audio
          v-else-if="media.port === 'audio'"
          class="nowheel nodrag"
          :src="mediaSrc"
          controls
          preload="metadata"
          @click.stop
        />
        <img v-else :src="mediaSrc" :alt="node.label" />
        <span v-if="media.mock" class="mock-badge">mock 占位</span>
      </div>
      <div v-if="node.type === 'image'" class="tnode-connect-hint">从右侧橙色圆点拖到 ref / image 输入</div>
      <div v-else-if="node.type === 'video'" class="tnode-connect-hint">从右侧紫色圆点拖到 video_ref 输入</div>
      <div v-else-if="node.type === 'audio'" class="tnode-connect-hint">从右侧音频端口拖到时间线 audio 输入</div>
      <div v-else-if="node.type === 'timeline'" class="tnode-connect-hint">连接视频后，点击底部“成片”并人工确认</div>

      <!-- 输入端口（左） -->
      <Handle
        v-for="([name, type], index) in inPorts"
        :key="`in-${name}`"
        :id="name"
        type="target"
        :position="Position.Left"
        :class="`port-${type}`"
        :style="{ top: portTop(index) }"
        :title="`输入：${name}（${type}）`"
        :aria-label="`输入 ${name} ${type}`"
      />
      <span
        v-for="([name], index) in inPorts"
        :key="`in-label-${name}`"
        class="tnode-portlabel in"
        :style="{ top: labelTop(index) }"
        >{{ name }}</span
      >

      <!-- 输出端口（右） -->
      <Handle
        v-for="([name, type], index) in outPorts"
        :key="`out-${name}`"
        :id="name"
        type="source"
        :position="Position.Right"
        :class="`port-${type}`"
        :style="{ top: portTop(index) }"
        :title="`输出：${name}（${type}）`"
        :aria-label="`输出 ${name} ${type}`"
      />
      <span
        v-for="([name], index) in outPorts"
        :key="`out-label-${name}`"
        class="tnode-portlabel out"
        :style="{ top: labelTop(index) }"
        >{{ name }}</span
      >
    </div>
  </div>
</template>
