<script setup>
/* 画布路由壳：把 :name 绑到 store.js 的画布，再挂现有 CanvasWorkspace。
 * CanvasWorkspace 自带 bootstrap；这里只负责「看哪个画布」。
 */
import { onMounted } from "vue";
import { useRoute } from "vue-router";
import CanvasWorkspace from "../CanvasWorkspace.vue";
import { actions, state } from "../store.js";

const route = useRoute();

onMounted(async () => {
  const name = String(route.params.name || "");
  if (name && state.canvasName !== name) {
    try {
      await actions.loadCanvas(name);
    } catch {
      /* 画布名不存在时保留默认画布，画布内部会显示加载错误 */
    }
  }
});
</script>

<template>
  <CanvasWorkspace />
</template>
