import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 开发/预览期：Vite 起在 5173（dev）/ 4173（preview），把 /api 与 /assets 代理到 FastAPI（8000）。
// 生产期：npm run build 产出 dist/，由 FastAPI 直接挂到根路径（同源，无需代理）。
const proxy = {
  "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
  "/assets": { target: "http://127.0.0.1:8000", changeOrigin: true },
};

export default defineConfig({
  plugins: [vue()],
  server: { port: 5173, proxy },
  preview: { port: 4173, proxy },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // 默认产物目录叫 assets，会和后端的 /assets（工作区产物：生成图/视频）**撞路径**，
    // 那样前端 JS 会 404。改成 static，两条路径各归各的。
    assetsDir: "static",
    chunkSizeWarningLimit: 2000,
    // 单入口：index.html（IA 骨架 Demo 已随 src/skeleton 一并移除）
    rollupOptions: {
      input: {
        main: fileURLToPath(new URL("./index.html", import.meta.url)),
      },
    },
  },
});
