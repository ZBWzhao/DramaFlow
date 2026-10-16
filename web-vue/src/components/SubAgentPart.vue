<script setup>
/* 子 Agent 块：父消息里的一个 part，内部再有自己的 steps（工具调用）。
 * 层次关系保留在数据里 —— 这正是 parts 模型比"平铺事件"强的地方。
 */
import { computed } from "vue";
import MarkdownText from "./MarkdownText.vue";
import ThinkingPart from "./ThinkingPart.vue";
import ToolPart from "./ToolPart.vue";

const props = defineProps({ part: { type: Object, required: true } });

const STATUS = {
  running: { text: "执行中", tag: "warning" },
  inputRequired: { text: "等待回答", tag: "warning" },
  done: { text: "完成", tag: "success" },
  aborted: { text: "已中断", tag: "danger" },
  error: { text: "失败", tag: "danger" },
  limited: { text: "结果超长", tag: "info" },
};
const statusMeta = computed(() => STATUS[props.part.status] || STATUS.running);
const toolCount = computed(() => props.part.steps.filter((step) => step.kind === "tool").length);
</script>

<template>
  <div class="part part-sub">
    <button class="part-head" @click="part.expanded = !part.expanded">
      <el-icon class="chev" :class="{ open: part.expanded }"><ArrowRight /></el-icon>
      <el-icon class="icon" :class="{ spin: part.status === 'running' }">
        <Loading v-if="part.status === 'running'" />
        <Connection v-else />
      </el-icon>
      <b class="title">子 Agent · {{ part.name }}</b>
      <span class="dim">{{ toolCount }} 次工具调用</span>
      <span v-if="part.tokens !== null" class="dim mono">增量 {{ part.tokens }} tok</span>
      <el-tag size="small" :type="statusMeta.tag">{{ statusMeta.text }}</el-tag>
    </button>

    <div v-if="part.reports.length" class="reports">
      <div v-for="(report, index) in part.reports" :key="index" class="report">{{ report }}</div>
    </div>

    <div v-if="part.expanded && part.steps.length" class="part-body sub-steps">
      <template v-for="(step, index) in part.steps" :key="index">
        <ThinkingPart v-if="step.kind === 'thinking'" :part="step" />
        <ToolPart v-else-if="step.kind === 'tool'" :part="step" compact />
        <MarkdownText v-else-if="step.kind === 'text'" :content="step.text" :streaming="step.streaming" />
      </template>
    </div>
  </div>
</template>
