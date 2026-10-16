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

    <!-- 左上：项目入口。会话只在 Agent 面板中管理，避免同一会话出现两处入口。 -->
    <div class="dock dock-left">
      <div class="brand">
        <span class="brand-mark">TF</span>
        <span class="brand-name">短剧工作台</span>
      </div>
      <span class="dock-sep" />
      <span class="dock-label">项目</span>
      <el-select
        :model-value="state.canvasName"
        size="small"
        class="project-select"
        title="切换项目工作区"
        aria-label="当前项目工作区"
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

    <!-- 右上：只保留工作区状态；当前会话在 Agent 面板顶部统一管理。 -->
    <div class="dock dock-right">
      <span class="chip" :title="state.workspace">{{ state.workspace || "—" }}</span>
    </div>

    <InfoDrawer v-if="state.drawerOpen" />
    <ExportDialog />
    <FloatingAgent v-model="agentVisible" />
  </div>
</template>
