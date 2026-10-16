<script setup>
import { computed } from "vue";
import { ElMessage } from "element-plus";
import { actions, state } from "../store.js";

const ORDER = [
  "planning",
  "scripting",
  "asset_design",
  "storyboarding",
  "prompting",
  "reviewing",
  "preparing_media",
  "waiting_approval",
  "generating",
  "waiting_composition",
  "composing",
  "final_review",
  "completed",
];

const LABELS = {
  planning: "创意规划",
  scripting: "剧本创作",
  asset_design: "角色与场景设定",
  storyboarding: "结构化分镜",
  prompting: "视频提示词",
  reviewing: "生产包审校",
  preparing_media: "生成图准备",
  waiting_approval: "用户审批",
  generating: "媒体生成",
  waiting_composition: "等待成片合成",
  composing: "成片合成",
  final_review: "最终验收",
  completed: "制作完成",
};

const run = computed(() => state.productionRun);
const stages = computed(() => run.value?.stages || {});
const visibleStages = computed(() => {
  const names = [...ORDER];
  for (const name of Object.keys(stages.value)) if (!names.includes(name)) names.push(name);
  return names.map((name) => ({ name, label: LABELS[name] || name, ...(stages.value[name] || {}) }));
});

function meta(item) {
  if (item.status === "succeeded") return { icon: "✓", cls: "ok", text: "已通过" };
  if (["running", "validating", "retrying"].includes(item.status)) {
    const text = item.status === "validating" ? "验收中" : item.status === "retrying" ? "返工中" : "执行中";
    return { icon: "↻", cls: "active", text };
  }
  if (["failed", "needs_revision"].includes(item.status)) return { icon: "!", cls: "bad", text: "未通过" };
  if (["waiting_approval", "waiting_composition"].includes(item.status)) return { icon: "●", cls: "wait", text: "等待拍板" };
  return { icon: "○", cls: "pending", text: "未开始" };
}

async function retry() {
  try {
    await actions.resumeProduction();
  } catch (err) {
    ElMessage.error(`继续制作失败：${err.message || err}`);
  }
}
</script>

<template>
  <section v-if="run" class="production-progress">
    <header>
      <div>
        <b>短剧生产进度</b>
        <span class="dim mono">{{ run.id?.slice(0, 8) }}</span>
      </div>
      <el-tag v-if="run.status === 'waiting_approval'" size="small" type="warning">等待审批</el-tag>
      <el-tag v-else-if="run.status === 'waiting_composition'" size="small" type="warning">等待合成</el-tag>
      <el-tag v-else-if="run.status === 'limited'" size="small" type="danger">已暂停</el-tag>
      <el-tag v-else size="small" :type="run.status === 'running' ? 'primary' : 'success'">{{ run.status }}</el-tag>
    </header>

    <div class="production-stage-list">
      <div v-for="item in visibleStages" :key="item.name" class="production-stage" :class="meta(item).cls">
        <span class="stage-icon">{{ meta(item).icon }}</span>
        <span class="stage-name">{{ item.label }}</span>
        <span v-if="item.attempts" class="dim">第 {{ item.attempts }} 次</span>
        <span class="stage-status">{{ meta(item).text }}</span>
        <ul v-if="item.problems?.length" class="stage-problems">
          <li v-for="problem in item.problems.slice(0, 3)" :key="problem">{{ problem }}</li>
        </ul>
      </div>
    </div>

    <footer v-if="run.status === 'limited' && !state.running">
      <span class="dim">已通过的阶段不会重复执行。</span>
      <el-button size="small" type="warning" plain @click="retry">只重试当前阶段</el-button>
    </footer>
  </section>
</template>
