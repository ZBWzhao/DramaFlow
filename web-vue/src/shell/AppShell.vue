<script setup>
/* 系统外壳：顶栏（品牌 / 主导航 / 工具区）+ 路由出口。
 *
 * 主导航按产品流程划分：剧本 → 制作 → 画布 → 预览；
 * 工具区放跨流程的入口：项目资产 / 数据看板 / 项目信息 / 任务中心。
 * AI 助手是全局件：剧本页与画布页停靠（页面自挂 AiPanel mode="dock"），
 * 其余页面用右下浮球唤起全局抽屉（AiPanel mode="drawer"）。
 */
import { computed } from "vue";
import { useRoute } from "vue-router";
import TaskCenter from "./TaskCenter.vue";

const route = useRoute();

/* 项目态：/project/:name/* 下才显示项目导航与工具区 */
const inproj = computed(() => route.path.startsWith("/project/"));
const pname = computed(() => String(route.params.name || ""));

/* 主导航：制作 tab 覆盖 production 与 wall（首帧墙是制作的子页） */
const tabs = [
  { label: "剧本", to: (n) => `/project/${n}/script`, match: (p) => p.endsWith("/script") },
  { label: "制作", to: (n) => `/project/${n}/production`, match: (p) => p.endsWith("/production") || p.endsWith("/wall") },
  { label: "画布", to: (n) => `/project/${n}/canvas`, match: (p) => p.endsWith("/canvas") },
  { label: "预览", to: (n) => `/project/${n}/preview`, match: (p) => p.endsWith("/preview") },
];
</script>

<template>
  <div class="df-shell">
    <header class="topbar">
      <div class="brand">
        <span class="wd">DRAMA<i>FLOW</i></span>
        <router-link v-if="inproj" class="home-link" to="/">‹ 项目</router-link>
      </div>
      <span v-if="inproj" class="pname">{{ pname }}</span>
      <nav v-if="inproj" class="top-tabs">
        <router-link v-for="t in tabs" :key="t.label" :to="t.to(pname)" class="tab" :class="{ cur: t.match(route.path) }">{{ t.label }}</router-link>
      </nav>
      <div v-if="inproj" class="top-utils">
        <router-link class="util" :to="`/project/${pname}/assets`" :class="{ cur: route.path.endsWith('/assets') }">项目资产</router-link>
        <router-link class="util" :to="`/project/${pname}/dashboard`" :class="{ cur: route.path.endsWith('/dashboard') }">数据看板</router-link>
        <!-- 项目信息弹窗：Task 6 接 NewProjectDialog 同款壳 -->
        <button class="util" type="button" title="项目信息（施工中）">项目信息</button>
        <span class="sep"></span>
        <!-- 规格：Task 8 从 studio.project 读真实值 -->
        <span class="spec num">9:16 · 480P</span>
        <TaskCenter />
      </div>
      <!-- NEW-PROJECT：Task 6 挂 NewProjectDialog（首页态右上的「新建项目」按钮） -->
      <div v-else class="home-right"></div>
    </header>

    <main class="df-main">
      <router-view />
    </main>

    <!-- AI-FAB：Task 5 挂全局 AI 浮球 + 抽屉 -->
  </div>
</template>

<style scoped>
/* 作用域重置：新壳内的元素回到 demo 的零间距基线（旧视图不受影响） */
.df-shell,
.df-shell *,
.df-shell *::before,
.df-shell *::after {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

.df-shell { display: flex; flex-direction: column; height: 100vh; }
.df-main { flex: 1; overflow: hidden; position: relative; display: flex; flex-direction: column; }
.df-main > :deep(*) { flex: 1; min-height: 0; }

/* ---------- 顶栏（demo v4 · .topbar） ---------- */
.topbar {
  display: flex; align-items: stretch; height: var(--h-topbar);
  padding: 0 var(--sp-5); background: var(--bg-2);
  border-bottom: 1px solid var(--line); flex: none; position: relative; z-index: 20;
}
.brand { display: flex; align-items: center; gap: 10px; padding-right: var(--sp-5); border-right: 1px solid var(--line); }
.brand .wd { font-weight: 600; font-size: var(--fs-13); letter-spacing: .18em; }
.brand .wd i { font-style: normal; color: var(--accent); }
.home-link { color: var(--text-3); font-size: var(--fs-12); text-decoration: none; }
.home-link:hover { color: var(--text); }
.pname {
  display: flex; align-items: center; padding: 0 var(--sp-4);
  color: var(--text-2); max-width: 300px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.top-tabs { display: flex; align-items: stretch; margin: 0 auto; }
.tab {
  position: relative; display: flex; align-items: center; padding: 0 26px;
  color: var(--text-3); font-size: var(--fs-14);
  font-family: var(--serif); letter-spacing: .06em; text-decoration: none;
}
.tab:hover { color: var(--text-2); }
.tab.cur { color: var(--text); }
.tab.cur::after { content: ""; position: absolute; left: 26px; right: 26px; bottom: 0; height: 2px; background: var(--accent); }
.top-utils { display: flex; align-items: center; gap: var(--sp-4); }
.util { color: var(--text-3); font-size: var(--fs-13); padding: 5px 3px; text-decoration: none; }
.util:hover { color: var(--text); }
.util.cur { color: var(--text); }
.sep { width: 1px; height: 16px; background: var(--line-2); }
.spec { font-family: var(--mono); font-size: var(--fs-11); color: var(--text-3); }
.home-right { margin-left: auto; display: flex; align-items: center; }
.top-utils + .home-right { display: none; }
</style>
