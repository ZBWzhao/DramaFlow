<script setup>
import { onMounted, onUnmounted } from "vue";
import { RouterLink, RouterView, useRouter, useRoute } from "vue-router";
import { activeCount, dock, markSwitch, toggleDock } from "./appStore";
import TaskDock from "./TaskDock.vue";

const router = useRouter();
const route = useRoute();

const nav = [
  { name: "script", label: "剧本" },
  { name: "canvas", label: "画布" },
  { name: "assets", label: "资产" },
];

// 统计跨页切换次数：与任务坞的 elapsed 一起，作为“状态没丢”的证据
let lastPath = "";
router.afterEach((to) => {
  if (lastPath && to.path !== lastPath) markSwitch();
  lastPath = to.path;
});

function onKeydown(e) {
  if (e.key === "Escape" && !dock.collapsed) toggleDock(false);
}
onMounted(() => window.addEventListener("keydown", onKeydown));
onUnmounted(() => window.removeEventListener("keydown", onKeydown));
</script>

<template>
  <div class="sk-app">
    <header class="sk-bar">
      <div class="sk-bar-left">
        <button class="sk-icon" title="返回项目列表" aria-label="返回项目列表">←</button>
        <div class="sk-brand">
          <span class="sk-mark" aria-hidden="true" />
          <span class="sk-brand-name">短剧工作台</span>
        </div>
        <span class="sk-sep" aria-hidden="true" />
        <span class="sk-project">媒体卡演示</span>
        <span class="sk-save"><i class="sk-save-dot" aria-hidden="true" />已保存 20:14</span>
      </div>

      <nav class="sk-nav" aria-label="页面">
        <RouterLink v-for="n in nav" :key="n.name" :to="{ name: n.name }" class="sk-nav-item">
          {{ n.label }}
        </RouterLink>
      </nav>

      <div class="sk-bar-right">
        <button class="sk-dock-btn" :aria-expanded="!dock.collapsed" @click="toggleDock()">
          <span class="sk-live" aria-hidden="true" />
          任务 {{ activeCount }}
        </button>
        <button class="sk-ghost" disabled title="命令面板（骨架未接入）">⌘K</button>
        <button class="sk-cta" @click="router.push({ name: 'canvas' })">
          进入画布<span aria-hidden="true">→</span>
        </button>
      </div>
    </header>

    <main class="sk-main">
      <RouterView v-slot="{ Component }">
        <component :is="Component" />
      </RouterView>
    </main>

    <!-- 任务坞挂在 RouterView 之外：路由切换只换 <main> 的内容，坞本身不卸载 -->
    <TaskDock />

    <div v-if="route.name !== 'canvas'" class="sk-hint">路由：{{ route.fullPath }} · 任务坞与计时器在路由之外</div>
  </div>
</template>
