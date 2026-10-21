<script setup>
/* 系统外壳：三层路由。
 *
 *   home    项目列表（打开系统看到的第一屏）
 *   studio  制片台（制作者的主界面）
 *   canvas  画布（Agent 编排的结构视图，从制片台主动进入）
 *
 * 画布不再默认挂载成「工作台」：它不是制作者必须操作的东西，
 * 让它常驻会把整条产品叙事拉回「Agent 演示器」。
 */
import { shell, goHome, openCanvas } from "./shell/shellStore.js";
import HomeView from "./shell/HomeView.vue";
import StudioPanel from "./studio/StudioPanel.vue";
import CanvasWorkspace from "./CanvasWorkspace.vue";
import "./studio/studio.css";
</script>

<template>
  <HomeView v-if="shell.view === 'home'" />
  <StudioPanel v-else-if="shell.view === 'studio'" @close="goHome" @canvas="openCanvas" />
  <CanvasWorkspace v-else />
</template>
