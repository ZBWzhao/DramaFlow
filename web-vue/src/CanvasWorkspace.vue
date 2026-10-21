<script setup>
/* 画布工作台：Agent 侧的编排界面。
 *
 * 地位变了：以前它是系统入口，现在它是「结构视图」——从制片台主动进来，
 * 看 Agent 把这一集拆成了什么图、哪些节点在等什么。人不需要常驻这里。
 */
import { computed, onMounted, ref } from "vue";
import { actions, bootstrap, state } from "./store.js";
import { goHome, openStudioForCurrent } from "./shell/shellStore.js";
import { studio } from "./studio/studioStore.js";
import CanvasPanel from "./components/CanvasPanel.vue";
import CanvasBoard from "./canvas2/CanvasBoard.vue";
import FloatingAgent from "./components/FloatingAgent.vue";
import InfoDrawer from "./components/InfoDrawer.vue";
import ExportDialog from "./components/ExportDialog.vue";
import ProjectPanel from "./components/ProjectPanel.vue";

onMounted(async () => {
  // 从制片台进来时看的是「这个项目」的结构图，不是默认画布。
  if (studio.canvas) state.canvasName = studio.canvas;
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

/* Agent 默认收成右下角一颗悬浮球（LibTV 形态）：画布先给内容让路，
 * 需要对话时再点开，不常驻占掉右侧 1/3。 */
const agentVisible = ref(false);
// 新画布（demo 引擎）默认启用；?legacy=1 回退到 @vue-flow 版
const useLegacyCanvas = ref(location.search.includes("legacy=1"));
const projectPanelOpen = ref(false);
const currentProjectTitle = computed(
  () => state.canvasMeta[state.canvasName]?.story_title || state.canvasName || "未选择项目",
);
</script>

<template>
  <div class="app canvas-app">
    <CanvasPanel v-if="useLegacyCanvas" />
    <CanvasBoard v-else @open-project="projectPanelOpen = true" />

    <!-- 旧画布（?legacy=1）仍用悬浮 dock；新画布的项目上下文/视图切换/主操作已进顶栏 -->
    <template v-if="useLegacyCanvas">
      <!-- 左上：返回项目列表 + 项目入口 + 制片台。画布已降级，制片台才是主界面。 -->
      <div class="dock dock-left">
        <button class="btn ghost" type="button" title="返回项目列表" @click="goHome">← 项目</button>
        <span class="dock-sep" />
        <button
          class="project-trigger"
          type="button"
          title="打开项目管理"
          aria-label="打开项目管理"
          :disabled="state.running || state.canvasBusy"
          @click="projectPanelOpen = true"
        >
          <span class="project-trigger-label">项目</span>
          <span class="project-trigger-title">{{ currentProjectTitle }}</span>
          <el-icon><ArrowDown /></el-icon>
        </button>
        <span class="dock-sep" />
        <button
          class="btn primary"
          type="button"
          title="回到制片台：剧本 / 资产 / 制作 / 预览"
          :disabled="!state.canvasName"
          @click="openStudioForCurrent"
        >
          制片台
        </button>
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
    </template>

    <InfoDrawer v-if="state.drawerOpen" />
    <ProjectPanel :open="projectPanelOpen" @close="projectPanelOpen = false" />
    <ExportDialog />
    <FloatingAgent v-model="agentVisible" :variant="useLegacyCanvas ? 'legacy' : 'cv2'" />
  </div>
</template>
