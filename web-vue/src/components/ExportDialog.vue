<script setup>
/* 导出弹层：全片提示词（{{ref N}} 已由服务端展开成参考声明）。 */
import { computed } from "vue";
import { ElMessage } from "element-plus";
import { state } from "../store.js";

const data = computed(() => state.exportData || {});

async function copy() {
  try {
    await navigator.clipboard.writeText(data.value.markdown || "");
    ElMessage.success("已复制到剪贴板");
  } catch {
    ElMessage.warning("复制失败，可手动选中");
  }
}

function download() {
  const blob = new Blob([data.value.markdown || ""], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${state.canvasName}-prompts.md`;
  link.click();
  URL.revokeObjectURL(url);
}
</script>

<template>
  <el-dialog
    v-model="state.exportOpen"
    width="min(960px, 92vw)"
    top="6vh"
    :title="`全片提示词 · ${state.canvasName}`"
  >
    <template #header>
      <div style="display: flex; align-items: center; gap: 12px">
        <b>全片提示词</b>
        <span class="dim">{{ data.path }}（{{ data.segments }} 个片段，{{ data.chars }} 字）</span>
      </div>
    </template>
    <pre class="export-body">{{ data.markdown }}</pre>
    <template #footer>
      <el-button @click="download">下载 .md</el-button>
      <el-button type="primary" @click="copy">复制</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.export-body {
  margin: 0;
  max-height: 62dvh;
  overflow: auto;
  padding: 16px;
  border-radius: var(--ui-radius);
  background: #f7f8fa;
  border: 1px solid var(--line);
  font: 12px/1.65 ui-monospace, Consolas, monospace;
  white-space: pre-wrap;
  color: var(--text-2);
}
</style>
