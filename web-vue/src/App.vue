<script setup>
/* 外壳：画布始终挂载为工作台，Agent 以浮窗覆盖在画布上。
 * 这样模型调用画布工具时，用户不需要离开对话就能看到节点和连线发生变化。
 */
import { onMounted, ref } from "vue";
import { actions, bootstrap, state } from "./store.js";
import CanvasPanel from "./components/CanvasPanel.vue";
import FloatingAgent from "./components/FloatingAgent.vue";
import InfoDrawer from "./components/InfoDrawer.vue";
import ExportDialog from "./components/ExportDialog.vue";

onMounted(async () => {
  await bootstrap().catch((err) => console.error("初始化失败", err));
  // 开发用假数据（见 src/demoFeed.js）：
  //   ?demo=1     一次铺出全部形态（思考/工具成败/画布变更/子 Agent/Markdown）
  //   ?demo=live  停在"正在运行"状态，检查计时器与"无新事件"告警
  if (location.search.includes("demo=")) {
    const { loadDemoFeed, loadLiveFeed } = await import("./demoFeed.js");
    if (location.search.includes("demo=live")) loadLiveFeed();
    else loadDemoFeed();
  }
});

async function onPickCanvas(name) {
  await actions.switchCanvas(name);
}

const agentVisible = ref(true);
</script>

<template>
  <div class="app">
    <CanvasPanel />

    <!-- 左上：品牌 + 画布切换 + 信息 -->
    <div class="dock dock-left">
      <div class="brand">
        <span class="brand-mark">TF</span>
        <span class="brand-name">短剧工作台</span>
      </div>
      <span class="dock-sep" />
      <el-select
        :model-value="state.canvasName"
        size="small"
        style="width: 150px"
        title="切换画布"
        :disabled="state.running || state.canvasBusy"
        @update:model-value="onPickCanvas"
      >
        <el-option
          v-for="name in state.canvases"
          :key="name"
          :label="state.canvasMeta[name]?.story_title || name"
          :value="name"
        />
      </el-select>
      <span class="dock-sep" />
      <el-button size="small" title="会话信息 / 技能 / 工具" @click="state.drawerOpen = !state.drawerOpen">
        <el-icon><InfoFilled /></el-icon>
      </el-button>
    </div>

    <!-- 顶部居中：当前工作区状态 -->
    <div class="dock dock-center">
      <span class="workspace-state" :class="{ active: state.running }">
        <i />{{ state.running ? state.activity || "Agent 运行中" : state.canvas.meta?.story_title || "画布工作区" }}
      </span>
    </div>

    <!-- 右上：工作区 + 会话 -->
    <div class="dock dock-right">
      <span class="chip" :title="state.workspace">{{ state.workspace || "—" }}</span>
      <span class="chip" :title="state.sessionId || ''">{{ state.sessionId || "未开始" }}</span>
    </div>

    <InfoDrawer v-if="state.drawerOpen" />
    <ExportDialog />
    <FloatingAgent v-model="agentVisible" />
  </div>
</template>
