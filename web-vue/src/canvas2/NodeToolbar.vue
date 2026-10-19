<script setup>
/* B 版画布的工具条：只放**真实可用**的动作。
 * demo 里那 9 个图像工具（人像质感/全景/多角度/打光/九宫格/高清/元素编辑/图层分离/宫格切分）
 * 我们没有对应后端，摆上去只会点不动，所以这里不放；等有接口再逐个长出来。
 */
const props = defineProps({
  node: { type: Object, required: true },
});
const emit = defineEmits(["generate", "upload", "history", "detail"]);

const TOOLS = [
  { key: "generate", label: "生成这一张", show: (n) => n.canGenerate },
  { key: "upload", label: "上传 / 换一张", show: (n) => n.canUpload },
  { key: "history", label: "历史", show: (n) => n.historyCount > 0 },
  { key: "detail", label: "详情", show: () => true },
];
</script>

<template>
  <div class="node-toolbar" @click.stop>
    <button
      v-for="tool in TOOLS.filter((item) => item.show(props.node))"
      :key="tool.key"
      class="tool-btn"
      :class="{ primary: tool.key === 'generate' }"
      :disabled="props.node.busy && tool.key === 'generate'"
      :title="props.node.busy && tool.key === 'generate' ? '正在生成，等它完成' : ''"
      @click.stop="emit(tool.key, props.node)"
    >
      {{ tool.label }}
    </button>
  </div>
</template>
