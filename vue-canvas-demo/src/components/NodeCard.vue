<script setup>
import { CARD_W } from "../geometry.js";
import NodeToolbar from "./NodeToolbar.vue";
import PromptBox from "./PromptBox.vue";

const props = defineProps({
  node: { type: Object, required: true },
  selected: { type: Boolean, default: false },
});
const emit = defineEmits(["select", "send"]);

function onCardClick(event) {
  event.stopPropagation();
  emit("select", props.node.id);
}
</script>

<template>
  <div
    class="node"
    :class="{ selected, plain: node.plain }"
    :style="{ left: node.x + 'px', top: node.y + 'px', width: CARD_W + 'px' }"
    @click="onCardClick"
  >
    <NodeToolbar v-if="selected" />

    <div class="node-caption">
      <span class="cap-glyph">▣</span>
      <span class="cap-title">{{ node.title }}</span>
      <span v-if="node.dims" class="cap-dims">{{ node.dims }}</span>
    </div>

    <div class="node-body" :class="{ 'is-text': node.plain, 'is-video': node.video }">
      <template v-if="node.plain">
        <p class="node-text">{{ node.prompt }}</p>
      </template>
      <template v-else>
        <img v-if="node.img" :src="node.img" :alt="node.title" draggable="false" />
        <span v-else class="node-empty-icon">▣</span>
        <span v-if="node.img" class="ai-tag">AI生成</span>

        <template v-if="node.video">
          <button class="play-btn">▶</button>
          <span class="video-time">{{ node.duration }}</span>
        </template>

        <div v-if="!node.video && node.attempts" class="attempts">
          <span class="attempts-title">尝试：</span>
          <button
            v-for="label in node.attempts"
            :key="label"
            class="attempt"
            @click.stop
          >
            ⬆ {{ label }}
          </button>
        </div>

        <span class="handle left" title="连接">＋</span>
        <span class="handle right" title="连接">＋</span>
        <span class="node-menu" title="更多">⋯</span>
      </template>
    </div>

    <PromptBox v-if="selected" :node="node" @send="emit('send', $event)" />
  </div>
</template>
