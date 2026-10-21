/* 前端路由（hash）：首页 + 项目内七屏 + 画布。
 *
 * 项目名 = 画布名（后端所有 /api/studio/* 都以 canvas 为键）。
 * 画布复用现有 CanvasWorkspace（内部直连 store.js），不经过 studioStore。
 */
import { createRouter, createWebHashHistory } from "vue-router";
import AppShell from "./shell/AppShell.vue";

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    {
      path: "/",
      component: AppShell,
      children: [
        { path: "", name: "home", component: () => import("./views/HomeView.vue") },
        { path: "project/:name/script", name: "script", component: () => import("./views/ScriptView.vue") },
        { path: "project/:name/production", name: "production", component: () => import("./views/ProductionView.vue") },
        { path: "project/:name/wall", name: "wall", component: () => import("./views/KeyframeWall.vue") },
        { path: "project/:name/assets", name: "assets", component: () => import("./views/AssetsView.vue") },
        { path: "project/:name/dashboard", name: "dashboard", component: () => import("./views/DashboardView.vue") },
        { path: "project/:name/preview", name: "preview", component: () => import("./views/PreviewView.vue") },
        { path: "project/:name/canvas", name: "canvas", component: () => import("./views/CanvasView.vue") },
      ],
    },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
});
