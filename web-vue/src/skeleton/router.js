import { createRouter, createWebHashHistory } from "vue-router";
import ScriptPage from "./pages/ScriptPage.vue";
import CanvasPage from "./pages/CanvasPage.vue";
import AssetsPage from "./pages/AssetsPage.vue";

/**
 * 骨架 Demo 使用 hash 路由：静态托管（FastAPI 直接挂 dist）下刷新任意页面都不会 404。
 * 真正接入时可以换成 createWebHistory + 服务端 fallback，路由表本身不变。
 */
export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", redirect: "/script" },
    { path: "/script", name: "script", component: ScriptPage, meta: { label: "剧本" } },
    { path: "/canvas", name: "canvas", component: CanvasPage, meta: { label: "画布" } },
    { path: "/assets", name: "assets", component: AssetsPage, meta: { label: "资产" } },
    { path: "/:pathMatch(.*)*", redirect: "/script" },
  ],
});
