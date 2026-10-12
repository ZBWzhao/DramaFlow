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

const portTop = (index) => `${22 + index * 30}px`;
const labelTop = (index) => `${19 + index * 30}px`;
</script>

<template>
  <div class="tnode" :class="[`type-${node.type}`, `status-${node.status}`, { 'agent-changed': data.changed }]">
    <div class="tnode-head">
      <span class="tnode-type" :title="spec.label || node.type">{{ shortType }}</span>
      <span class="tnode-status">{{ node.status }}</span>
    </div>

    <div class="tnode-body" :style="bodyStyle">
      <div class="tnode-label">{{ node.label }}</div>
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
        <img v-else :src="mediaSrc" :alt="node.label" />
        <span v-if="media.mock" class="mock-badge">mock 占位</span>
      </div>
      <div v-if="node.type === 'image'" class="tnode-connect-hint">从右侧橙色圆点拖到 ref / image 输入</div>
      <div v-else-if="node.type === 'video'" class="tnode-connect-hint">从右侧紫色圆点拖到 video_ref 输入</div>

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
