# 开源「无限画布 / AI 分镜画布」调研（2026-10）

目的：给「节点即工具契约」的画布重做找可复用的开源实现，避免重复造轮子。

---

## 0. 一句话结论

**不用换底座，也不用另找"无限画布"库。**

- 我们的 `web-vue` 与 [`HBAI-Ltd/Toonflow-app`](https://github.com/HBAI-Ltd/Toonflow-app) 的 `apps/web` **同栈、几乎同版本**：`@vue-flow/core ^1.48.2`、`@vue-flow/minimap ^1.5.4`、`@vue-flow/background ^1.3.2`、`element-plus ^2.14.x`、`vue ^3.5.x`、`vite ^8.x`。它 **18,050★、MIT、TypeScript(Vue 3)**，画布交互层在 `apps/web/src/pages/workspace/panels/canvas/**`，可以按文件对齐移植。
- 真正值得抄的「**单卡工作台**（点开卡片=该节点 AI 工具条 + 对话框 + 候选版本）」在 [`869413421/ai-moive-studio`](https://github.com/869413421/ai-moive-studio)（AICON）**Apache-2.0、Vue3 + Element Plus**，`frontend/src/components/canvas/` 下就是现成组件。

---

## 1. 检索方式与我用到的工具

| 工具 | 状态 | 说明 |
| --- | --- | --- |
| `web_search` / `web_fetch`（内置） | 可用 | 无需插件即可上网查资料 |
| GitHub REST API（匿名） | 可用但限流 | 搜索 10 次/分钟、核心 API 60 次/小时（本次撞过一次 403 rate limit） |
| `gh` CLI 2.102.0 | 已安装**未登录** | `C:\Program Files\GitHub CLI\gh.exe`；`gh auth login` 后限流升到 5000/h 且可读私有仓库 |
| 插件市场命令（`dshmarket` 等） | 本机不存在 | PATH 里没有相关命令 |

建议：要更高效的 GitHub 检索，直接 `gh auth login`（比装插件更省事），之后可用
`gh search repos "infinite canvas" --sort stars --limit 30`、`gh api repos/{owner}/{repo}/...`。

---

## 2. 候选清单

### A 类：同栈 / 许可友好，**可以直接搬组件**

#### A1. `HBAI-Ltd/Toonflow-app` — 18,050★ · MIT · TypeScript(Vue 3)

> 短剧/漫剧制作工具 Toonflow：无限画布 + AI Agent + 可视化工作流，本地部署、自选模型、MCP 与插件扩展。topics 里直接写了 `libtv`、`node-based-editor`、`infinite-canvas`。

**优点**

- 与我们同栈同版本，`@vue-flow` 的画布参数（`only-render-visible-elements`、`snap-grid [16,16]`、`min-zoom 0.2 / max-zoom 8`、`ConnectionMode.Strict`、`selectionMode Partial`…）可直接对照。
- 画布交互层文件级齐全：`canvasHost.vue`、`nodeMenu.vue`（双击画布 + 选区 → 四级菜单：添加节点/选区操作/整理/操作）、`selectionHandle.vue`、`selectionToolbar.vue`、`useCanvasTools.ts`、`nodeSearch.vue`、`assetLibrary.vue`（左侧素材库，支持拖入画布、文件夹树、缩略图懒加载）、`canvasControls.vue`、`canvasEmpty.vue`、`arrangeCanvas.ts`（`@dagrejs/dagre` 自动整理）、`nodeClipboard.ts`、`useCanvasHistory.ts`、`loadNodeComponent.ts`、`remoteNode.vue`、`groupNode.vue`。
- **节点类型是扩展包**：`packages/ext/canvasNode|image|media|md|txt`，各自 `package.json` + `build/ext/*.umd.js`，可在插件市场安装/更新 —— 这正是「每个卡片对应一个能力」的工程化形态。
- 面很全：`dockview-vue` 面板布局、`three` 导演台、i18n 21 语言、MCP、`@tiptap` 富文本、`@tanstack/vue-virtual`。
- MIT，可商用。

**缺点**

- Electron 多端 monorepo（仓库约 297 MB），`package.json` 大量 `workspace:*`，**不能整包搬**，只能按文件移植并切依赖。
- 画布组件与它自己的 pinia store、`@toonflow/server` 路由深度耦合，移植要重写数据适配层。
- 是**商业竞品**（toonflow.net）：代码能用，但品牌/素材/视觉一致性有风险。

#### A2. `869413421/ai-moive-studio`（AICON）— 1,618★ · Apache-2.0 · Vue3 + Element Plus

> 自然语言驱动的无限画布工作流 Agent：剧本→分镜→素材→视频→分发；「Canvas 助手负责创建节点与连线，生成任务由你手动触发」。

**优点**

- 组件与我们要做的东西**一一对应**：
  - `CanvasNodeCard.vue`：类型 + 标题 + 状态 + 媒体区（图/视频/文本三态）
  - `CanvasImageStudio.vue` / `CanvasVideoStudio.vue` / `CanvasTextStudio.vue`：**单节点工作室** = 浮动头（类型图标 + 可编辑标题 + 上传）+ 预览卡（生成中遮罩 + 「预计 30 秒至 2 分钟」）+ 停靠面板（`CanvasPromptMentionEditor` 支持 `@` 引用其它节点 + 工具条）
  - `CanvasGenerationHistoryDrawer.vue`：**候选版本/生成历史抽屉**
  - `CanvasConnectionActions.vue` / `CanvasLinkCreateMenu.vue` / `CanvasLinkDragOverlay.vue`：从节点把手拖出 → 直接选「连到哪个节点/新建什么节点」
  - `CanvasWorkbenchLayout.vue`：顶栏 + 左侧工具栏（新建文本/图片/视频节点）
- Apache-2.0 商用最宽松，组件粒度小、Element Plus 与我们一致。

**缺点**

- 画布是**自研**（绝对定位 DOM 卡片 + Konva 手势），没有 vue-flow 的缩放/LOD/连线生态 —— 只抄"卡片 + 工作室"，画布底座仍用我们的。
- 后端是 FastAPI + Postgres + Redis + MinIO 全栈，接口语义要对齐我们的（`/api/canvas/nodes`、`/segments/{id}/generate` 等）。

---

### B 类：可参考范式，不建议搬代码

| 项目 | ★ | 许可 | 用途 |
| --- | --- | --- | --- |
| [`waooAI/waoowaoo`](https://github.com/waooAI/waoowaoo) | 14,575 | NOASSERTION | 工业级全流程 AI 影视生产平台，看"分镜-资产-镜头"信息架构 |
| [`dramaclaw/dramaclaw`](https://github.com/dramaclaw/dramaclaw) | 6,328 | NOASSERTION | FastAPI 的 "script → film" 流水线，后端能力对照 |
| [`ArcReel/ArcReel`](https://github.com/ArcReel/ArcReel) | 5,553 | **AGPL-3.0** | 跨镜头一致性、多供应商、**费用追踪**、剪映草稿导出 |
| [`zenstory-ai/drama-skills`](https://github.com/zenstory-ai/drama-skills) | 3,004 | MIT | 剧本/角色资产/分镜/提示词/审查 skill 包，可作"尝试预设"的提示词资产 |
| [`shuyu-labs/BigBanana-AI-Director`](https://github.com/shuyu-labs/BigBanana-AI-Director) | 2,342 | NOASSERTION | "Script-to-Asset-to-Keyframe" 工业化工作流范式 |
| [`gzxx-2025/aid-studio`](https://github.com/gzxx-2025/aid-studio) | 1,009 | MIT | Java 栈，路线图里就写"无限画布、导演台" |

---

### C 类：画布底座（只有换库时才考虑）

| 项目 | ★ | 许可 | 结论 |
| --- | --- | --- | --- |
| [`bcakmakoglu/vue-flow`](https://github.com/bcakmakoglu/vue-flow) | 6,906 | MIT | **我们已在用，不换** |
| [`retejs/rete`](https://github.com/retejs/rete) | 12,293 | MIT | 通用节点编辑器框架（Vue 渲染插件），可编程性更强但生态远不如 vue-flow |
| [`newcat/baklavajs`](https://github.com/newcat/baklavajs) | 2,096 | MIT | Vue 节点编辑器，偏工程化、缺媒体卡能力 |
| [`jagenjo/litegraph.js`](https://github.com/jagenjo/litegraph.js) | 8,170 | MIT | canvas2d 渲染，万级节点性能强；UI 老派、非 Vue |
| [`Comfy-Org/ComfyUI_frontend`](https://github.com/Comfy-Org/ComfyUI_frontend) | 2,072 | GPL-3.0 | "每节点=AI 工具+参数面板+队列"最成熟实现；GPL 传染、Vue3+PrimeVue 自成一派 |
| [`tldraw/tldraw`](https://github.com/tldraw/tldraw) | 50,965 | NOASSERTION | 无限画布 SDK 最强，但 **React-only + 自定义商用许可**，不适用 |
| [`excalidraw/excalidraw`](https://github.com/excalidraw/excalidraw) | 134,073 | MIT | React-only 白板，非节点图 |
| [`leaferjs/leafer-ui`](https://github.com/leaferjs/leafer-ui) | 4,457 | MIT | Canvas 渲染引擎（"AI 时代无限画布引擎"），若将来 LOD 走 canvas 渲染可参考 |
| [`konvajs/konva`](https://github.com/konvajs/konva) | 14,870 | NOASSERTION | 2D 渲染引擎，AICON 在用 |

---

### D 类：交互范式（学，不搬）

- [`n8n-io/n8n`](https://github.com/n8n-io/n8n) — 207,838★、Sustainable Use License（**非 OSI 开源**）。它的「单节点 **Execute node** / **Pin data** / 输入输出面板 / 运行历史」就是"单卡重跑 + 锁定 + 候选版本"的成熟范本，交互值得逐个对照。

---

## 3. 落到我们身上的三条动作（建议）

1. **抄 Toonflow-app 的画布交互层（MIT）**：双击画布→节点菜单、框选→选区工具条（创建副本/整理/删除）、左侧素材库拖入画布、节点搜索、快捷键工具模式、dockview 面板、dagre 自动整理。
   做法：按文件移植 8–10 个组件 + 适配我们的 `store.js`，**不引入它的 monorepo**。
2. **抄 AICON 的「单节点工作室」（Apache-2.0）**：点卡片 → 浮动头（类型 + 可编辑标题 + 上传）→ 预览卡（生成中遮罩/预计时长）→ 停靠面板（`@` 引用 prompt 编辑器 + 模型/参数工具条）→ 生成历史抽屉（候选版本切换）。这是 m00712 三件事里的第 3 件。
3. **不换底座**：继续 `@vue-flow`，LOD 与视口剔除已落地（`only-render-visible-elements`）。

---

## 4. 风险

- **许可**：MIT / Apache-2.0 可商用；AGPL（ArcReel）传染；GPL（ComfyUI）传染；NOASSERTION（tldraw、waoowaoo、n8n）必须逐个读 `LICENSE` 再决定。
- **竞品**：Toonflow-app 是商业化竞品，复用代码需保留 MIT 版权声明，并避免视觉/品牌"同款"。
- **栈漂移**：对方是 TS + pinia + workspace 包，我们是 JS + 自研 store，因此是"对齐交互、重写实现"，不是 copy 目录。
