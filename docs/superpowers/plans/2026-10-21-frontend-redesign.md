# DramaFlow 前端重设计 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按已批准的 demo v4（`docs/design-demo/2026-10-20-redesign-demo-v4.html`）重写 web-vue 外壳与项目内 8 屏，统一暖墨/朱砂/宋体视觉语言，AI 助手成为全局可对话组件。

**Architecture:** 视图层全部重写为「旧数据接线 + demo 皮」；数据层复用 `studio/studioStore.js`（后端算规则、jobs 轮询内置）与 `api.js`；AI 面板复用 `store.js` 的 `/api/agent` 会话协议；画布引擎（CanvasWorkspace→canvas2→store.js）不重写只换外壳；新增 vue-router（依赖已在 package.json）。

**Tech Stack:** Vue 3 + vue-router 4（hash）+ 手写 token CSS（新视图禁用 Element Plus 组件）+ Vite。

**Spec:** `docs/superpowers/specs/2026-10-21-frontend-redesign-design.md`

## Global Constraints

- 工作目录：`C:\Users\Lenovo\Desktop\toonflow-py-lab`（Desktop 检出），一切 git 操作在该目录。
- 分支：从 `feat/visual-canvas-v2` 切出 `feat/frontend-redesign`。
- 新视图不 import Element Plus 组件；CSS 只用 tokens 变量，不写裸色值。
- 按钮尺寸下限：`.btn` padding 9px 20px / font 14px；`.btn.sm` 6px 14px / 13px（用户要求按钮加大）。
- 语义不变量（spec §6）：人改优先、采用不抢占、过期可见、lint 显式、AI 先 diff 后应用、鎏金=声音。
- 所有后端调用走 `api.js`（getJSON/postJSON/deleteJSON/streamNDJSON），组件不直接 fetch。
- studio 端点前缀是 `/api/studio/`（见 studioStore.js 现有调用）；agent 会话端点无前缀（`/api/agent`、`/api/sessions`）。
- 每个 Task 收尾必须 `npm run build` 通过（在 `web-vue/` 下）再 commit。
- demo 文件是 UI 唯一视觉真源：每个视图任务标注了要移植的 demo 区块（`═══ 屏N ═══` 注释锚点），类名原样保留。

## File Structure

```
web-vue/src/
  main.js               改：去 theme.css，挂 router
  router.js             新
  App.vue               改：AppShell + router-view
  styles/tokens.css     重写（唯一真源）
  styles/theme.css      删
  shell/AppShell.vue    新：顶栏+AI抽屉+浮球
  shell/TaskCenter.vue  新
  shell/HomeView.vue    删（逻辑迁入 views/HomeView.vue）
  shell/NewProjectDialog.vue 改：加风格 Skill 卡
  ai/AiPanel.vue        新
  views/HomeView.vue / ScriptView.vue / ProductionView.vue / KeyframeWall.vue
        / AssetsView.vue / DashboardView.vue / PreviewView.vue   新
  studio/ProductionView.vue / AssetsView.vue / PreviewView.vue / ScriptView.vue
                        旧：逻辑迁移来源，迁移完删
  CanvasWorkspace.vue   改：去掉手写切换依赖，接受路由挂载
  src/skeleton/         删
```

---

### Task 1: 分支 + token 基建

**Files:** 改 `styles/tokens.css`、`main.js`；删 `styles/theme.css`（本 task 只断 import，文件删除放 Task 3）

- [ ] **Step 1** `git checkout feat/visual-canvas-v2 && git pull && git checkout -b feat/frontend-redesign`
- [ ] **Step 2** 重写 `web-vue/src/styles/tokens.css`：以 demo v4 `<style>` 内 `:root` 块为唯一内容（`--bg/--bg-2/--panel/--card/--card-2/--card-3/--line/--line-2/--line-3/--text/--text-2/--text-3/--accent/--accent-hi/--accent-soft/--gold/--gold-soft/--ok/--ok-soft/--warn/--warn-soft/--bad/--sp-1..6/--r-1..3/--fs-11..20/--dur/--ease/--sans/--serif/--mono`），外加 demo 的 `* reset`、`body`、滚动条、`.num` 四条基础规则。
- [ ] **Step 3** `grep -rn "var(--" web-vue/src --include="*.vue" --include="*.css" | grep -v canvas | grep -v skeleton` 提取现存组件引用的旧变量名；对其中新 token 未覆盖的名字，在 tokens.css 尾部加「兼容别名」段（旧名 → 新值，注释标明随旧视图删除）。特别核对：`--fg/--border/--bg-card` 类旧名是否存在。
- [ ] **Step 4** `main.js` 删除 `import "./styles/theme.css";` 一行（其余不动）。
- [ ] **Step 5** `cd web-vue && npm run build` 通过；`npm run dev` 肉眼确认旧首页/制片台/画布无样式崩坏（允许配色变暖，不允许布局烂）。
- [ ] **Step 6** Commit: `feat(ui): unify design tokens on warm-ink language, drop theme.css import`

### Task 2: router + AppShell 骨架

**Files:** 新 `router.js`、`shell/AppShell.vue`；改 `App.vue`、`main.js`

- [ ] **Step 1** 新建 `web-vue/src/router.js`：
```js
import { createRouter, createWebHashHistory } from "vue-router";
import AppShell from "./shell/AppShell.vue";

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", component: AppShell, children: [
      { path: "", component: () => import("./views/HomeView.vue") },
      { path: "project/:name/script",     component: () => import("./views/ScriptView.vue") },
      { path: "project/:name/production", component: () => import("./views/ProductionView.vue") },
      { path: "project/:name/wall",       component: () => import("./views/KeyframeWall.vue") },
      { path: "project/:name/assets",     component: () => import("./views/AssetsView.vue") },
      { path: "project/:name/dashboard",  component: () => import("./views/DashboardView.vue") },
      { path: "project/:name/preview",    component: () => import("./views/PreviewView.vue") },
      { path: "project/:name/canvas",     component: () => import("./CanvasWorkspace.vue") },
    ]},
  ],
});
```
- [ ] **Step 2** `main.js`：`import { router } from "./router.js"` + `app.use(router)`（Element Plus 全局注册保留）。
- [ ] **Step 3** 新建 `shell/AppShell.vue`：移植 demo 的 `.topbar` 块（品牌 `DRAMA<i>FLOW</i>`、`.top-tabs`（剧本/制作/画布/预览，衬线）、`.top-utils`（项目资产/数据看板/项目信息/规格/任务中心）、`.pname`），导航用 `<router-link :to>` 绑 `/project/${route.params.name}/...`；`route.path` 以 `/project/` 开头时显示项目态（对应 demo `body.inproj`）。主区放 `<router-view/>`。暂时不接 TaskCenter/AI（后续 Task 填充），顶栏右侧先留注释锚点 `<!-- TASKCENTER -->`、`<!-- AI-FAB -->`。
- [ ] **Step 4** `App.vue` 整体替换为 `<template><router-view/></template>`（旧手写切换逻辑删除；`CanvasWorkspace` 的挂载参数照旧 App.vue 传法搬进路由组件，见 Task 12）。
- [ ] **Step 5** 建占位页：`views/` 下 7 个 vue 先各写一行项目名 + 路由名（保证路由全通）。`npm run build` + 手动点一遍 8 条路由。
- [ ] **Step 6** Commit: `feat(ui): hash router + AppShell topbar skeleton`

### Task 3: 死代码清除

**Files:** 删 `src/skeleton/`、`styles/theme.css`、`components/` 死文件、旧 studio 视图延后到迁移完再删（本 task 不删旧 studio 视图）

- [ ] **Step 1** `rm -rf web-vue/src/skeleton`（唯一引用是其自身 main.js，已核实）。
- [ ] **Step 2** `rm web-vue/src/styles/theme.css`（Task 1 已断 import）。
- [ ] **Step 3** 对 `components/` 每个 `.vue` 逐个 `grep -rn "<FileName" web-vue/src --include="*.vue"`：引用者为空或仅为 skeleton/已删文件 → 删；`NodeStudio`、`GenerationHistoryDrawer`（CanvasBoard 用）与 FloatingAgent/ChatPanel/AssistantMessage/MarkdownText/ThinkingPart/ToolPart/QuestionCard/SubAgentPart（CanvasWorkspace→FloatingAgent 链路用，先 `grep CanvasWorkspace.vue` 确认其 import 清单再定）必须保留。
- [ ] **Step 4** `npm run build` 必须过（任何 missing import 立刻暴露）。
- [ ] **Step 5** Commit: `chore(ui): remove skeleton dir, theme.css and orphaned components`

### Task 4: TaskCenter 任务中心

**Files:** 新 `shell/TaskCenter.vue`；改 `AppShell.vue`

- [ ] **Step 1** 数据源 = `studio/studioStore.js` 现有 `studio.jobs` / `studio.produceJob`（700ms 轮询已在 store 内）。不加新轮询。
- [ ] **Step 2** 移植 demo「任务中心下拉」块（`.task-pop/.tj/.bar`）：运行中任务（进度条 + 百分比）在上、已完成置灰在下；点击顶栏 `.runstat` 开合；`document` 点击空白关闭。
- [ ] **Step 3** AppShell 挂到 `<!-- TASKCENTER -->` 锚点；`.runstat` 显示运行中数量 + 呼吸点。
- [ ] **Step 4** build + 手测（启动一次生成任务观察计数与进度）。
- [ ] **Step 5** Commit: `feat(ui): global task center fed by studioStore jobs`

### Task 5: AiPanel 共享对话组件

**Files:** 新 `ai/AiPanel.vue`；改 `AppShell.vue`（浮球+抽屉）

- [ ] **Step 1** 协议照抄 `store.js` `askNode`（1100-1180 行）模式：无 sessionId 时 `postJSON("/api/sessions", { canvas, title: "制片台助手" })`；随后
```js
await streamNDJSON("/api/agent",
  { prompt: text, session_id: sid || null, canvas, media_policy: "plan_only" },
  (ev) => {
    if (ev.type === "message_update") reply.text += ev.delta || "";
    else if (ev.type === "tool_execution_end") emit("canvas-changed");
  },
  (err) => { reply.error = err.message; },
  controller.signal);
```
followup/取消分别走 `/api/agent/followup`、`/api/agent/cancel`（同 store.js 1288/1355）。
- [ ] **Step 2** Props：`mode: "dock" | "drawer"`、`canvas: string`、`chips: string[]`（按页面注入的快捷指令）。Emits：`apply-diff({ segmentId, field, value })`、`canvas-changed`。
- [ ] **Step 3** 模板移植 demo `.ai-panel`（`.ai-head/.ai-body/.msg/.ai-quick/.q-chip/.ai-input`）；diff 卡片 `.diff/.d-old/.d-new/.d-acts` 渲染：当 AI 回复文本匹配「建议修改」结构（本期用约定 JSON 块 \`\`\`dramaflow-diff {segmentId, field, old, new} \`\`\`，解析函数写在小工具 `ai/diff.js` 并加单测 `web-vue/tests/diff.test.mjs`：能解析→渲染；无 diff→纯文本）。
- [ ] **Step 4** 「应用修改」→ `emit("apply-diff")`，由宿主页调 studioStore 的提示词保存动作；成功后本地卡片置 `.applied` + toast「已应用 · 提示词转为人改，服务端不再覆盖」。
- [ ] **Step 5** AppShell：右下 `.ai-fab`（浮球）+ `.ai-drawer`（挂同一 AiPanel mode="drawer"，canvas 取当前路由项目）；dock 形态留给剧本/画布页自挂。
- [ ] **Step 6** `node --test tests/diff.test.mjs` 过 + build + 手测对话（连 :8000 后端）。
- [ ] **Step 7** Commit: `feat(ui): shared AiPanel with streaming agent chat and diff-apply cards`

### Task 6: HomeView 海报墙 + NewProjectDialog 风格卡

**Files:** 新 `views/HomeView.vue`；改 `shell/NewProjectDialog.vue`；删 `shell/HomeView.vue`

- [ ] **Step 1** 把旧 `shell/HomeView.vue` 的数据逻辑（项目列表加载、新建提交、导入剧本参数）整体迁入新 `views/HomeView.vue`；模板换 demo `═══ 屏0 ═══`（`.poster-grid/.poster/.fx-*/.bar/.foot` + `.poster.new`）。
- [ ] **Step 2** NewProjectDialog 移植 demo 弹窗块：新增 `.style-cards` 四张风格卡（点击选中，值写入提交体的画风字段；第四张禁用 + 「规划」角标）；其余字段（名称/比例/画质/模型/分集符号/上传）沿用旧提交逻辑。
- [ ] **Step 3** build + 手测新建→跳转 `/project/:name/script`。
- [ ] **Step 4** Commit: `feat(ui): poster-wall home with style-skill picker`

### Task 7: ScriptView 剧本页

**Files:** 新 `views/ScriptView.vue`；挂 AiPanel dock

- [ ] **Step 1** 数据：复用 studioStore 的 script 加载（`getJSON(/api/studio/script...)`）与 import-script 动作。
- [ ] **Step 2** 模板移植 demo `═══ 屏1 ═══` 三栏：集列表（`.ep-item`，含集状态）、正文（`.script-doc` 宋体 16px/2.15 行高，对白 `<b>` 鎏金；episode 无值时顺序渲染段落与对白）、底部操作条（编辑模式 = `contenteditable` 正文 + 「重新分集」提交 `POST /api/studio/import-script` 全文 + warnings 展示）。
- [ ] **Step 3** 右栏挂 `<AiPanel mode="dock" :canvas :chips="['分析本集节奏','检查对白长度','第二集拆分镜']"/>`。
- [ ] **Step 4** build + 手测（读/编辑/重新分集/AI 对话）。
- [ ] **Step 5** Commit: `feat(ui): immersive serif script view with docked AI`

### Task 8: ProductionView 四栏外壳（队列+预览栏先行）

**Files:** 新 `views/ProductionView.vue`（本 task 建骨架与两侧栏；中央提示词编辑器在 Task 9）

- [ ] **Step 1** 模板移植 demo `═══ 屏2 ═══` 的 `.prod-top`（集选择、进度、首帧墙/画布视图按钮）+ `.prod4` 四栏框架；左栏概要 + 中左片段队列（`.seg-item` 状态点/时长，点击切 `studio.activeSegmentId`）+ 批量出片按钮（沿用旧 ProductionView 的批量动作，grep 旧文件 `批量`/`generate` 找到现成函数迁移）。
- [ ] **Step 2** 右栏视频预览：复用旧 ProductionView 的视频取数逻辑，皮换 demo `.vplayer/.v-acts/.mk-chips`（标记 chip 点击 = 现有 tags 动作）。
- [ ] **Step 3** 中央栏本 task 先放只读提示词（detail 已有数据）+ gen-bar + lint 行，Task 9 再做编辑器。
- [ ] **Step 4** build + 手测选段/生成/标记。
- [ ] **Step 5** Commit: `feat(ui): production 4-column shell with queue and preview rails`

### Task 9: 提示词编辑器（本重设计的心脏）

**Files:** 完善 `views/ProductionView.vue` 中央栏；`ai/diff.js` 复用

- [ ] **Step 1** 结构化/原文双模式（demo `.mode-sw`）：结构化 = demo `.prompt-doc .sec`（禁止/人物/场景/道具/画风/站位/画面/台词 字段行，人物/场景/道具链接到资产）；原文 = `textarea` 整段。
- [ ] **Step 2** 字段编辑：hover 出「点击编辑」→ `textarea.sec-edit` → 保存调 studioStore 现有提示词保存动作（旧 ProductionView 内 grep `prompt`；后端 `POST /segments/{id}/prompt`，保存体带该字段全文）→ toast「人改优先，服务端不再覆盖」。取消 = 还原。
- [ ] **Step 3** 分镜表行内编辑（demo `.shot-row`：景别/运镜 select、时长 input）——改后并入保存体；时长改出 3-8s 时显示 `.lint-line.bad`（数据来自 generate 422 的 `detail.findings`，若编辑态本地可判则先行本地提示 + 快捷修复按钮）。
- [ ] **Step 4** 版本区（demo `.vers/.ver-card`）：takes 列表缩略图 + 当前采用徽标 + hover「采用此版」（`POST /segments/{id}/take`）+ 重新生成（不抢占采用版文案固定展示）。字段旁「让 AI 改」→ 打开抽屉 AiPanel 并预填 `把片段 {no} 的【{field}】改得好一点`。
- [ ] **Step 5** AiPanel 的 `apply-diff` 在本页接线 → 调 Step 2 保存动作。
- [ ] **Step 6** build + 手测全流程：改字段→保存→生成→新版本出现且采用版不变→手动采用→lint 拦截→AI diff 应用。
- [ ] **Step 7** Commit: `feat(ui): prompt editor with human-edit priority, lint, takes and AI diff apply`

### Task 10: KeyframeWall 首帧墙

**Files:** 新 `views/KeyframeWall.vue`

- [ ] **Step 1** 数据：`GET /api/studio/episodes/{no}/storyboard`（旧调用点在旧 studio 视图里 grep `storyboard`）；cell 三态：fresh（绿描边）/ stale（warn 描边 + 「过期·分镜已变更」）/ miss（斜纹占位 + 点击补齐 = keyframe 生成动作）。
- [ ] **Step 2** 模板移植 demo `═══ 屏3 ═══`（`.wall/.kf/.wall-legend` + 头部「补齐缺的 N 张」批量按钮 = 逐段 keyframe 任务，进 TaskCenter）。
- [ ] **Step 3** 点击 fresh/stale cell → 跳 `/project/:name/production` 并置 `studio.activeSegmentId`。
- [ ] **Step 4** build + 手测三态与补齐。
- [ ] **Step 5** Commit: `feat(ui): keyframe wall with stale semantics and batch fill`

### Task 11: AssetsView 资产双栏

**Files:** 新 `views/AssetsView.vue`；删旧 `studio/AssetsView.vue`

- [ ] **Step 1** 数据逻辑从旧 `studio/AssetsView.vue` 迁移（列表/详情/looks/音色 PATCH/生成）；模板移植 demo `═══ 屏4 ═══`：counted tabs（`.ast-tab` 角色 2/2 等）、搜索、`.ast-grid`、详情区（形象 tabs、人物描述、音色 select + 试听按钮、出场章节、一致性标签、生成提示词 + 复制、图库、底部 gen-bar）。
- [ ] **Step 2** 音色试听：调现有 TTS 试听路径（旧 AssetsView 内 grep `tts|试听`；无则按钮调 `/api/tts/voices` 相邻的合成端点，mock 下为占位 toast 并注释 TODO-后端）。
- [ ] **Step 3** build + 手测（换音色→PATCH 生效；生成形象→任务进中心；已出图形象不重画文案展示）。
- [ ] **Step 4** Commit: `feat(ui): two-pane assets view with looks and voice binding`

### Task 12: Dashboard + Preview + 画布挂接

**Files:** 新 `views/DashboardView.vue`、`views/PreviewView.vue`；删旧 `studio/PreviewView.vue`；改 `CanvasWorkspace.vue` 外壳对齐

- [ ] **Step 1** Dashboard 移植 demo `═══ 屏5 ═══`（四 stat + 断点条 + 动态/成片两栏），数据全部取自 studioStore 已有派生值与 project/preview 数据，不新造规则。
- [ ] **Step 2** PreviewView 迁移旧 PreviewView 逻辑（assemble/approvals/compose/渲染历史），皮换 demo `═══ 屏6 ═══`（`.pv-hero`）。
- [ ] **Step 3** 画布：路由 `/project/:name/canvas` 挂 `CanvasWorkspace.vue`——把它当前从 App.vue 接的 props/初始化（grep 旧 App.vue）改为从 `route.params.name` 取；确认 FloatingAgent 会话正常。
- [ ] **Step 4** 画布外壳对齐（最小改动）：TopBar/边栏的硬编码色值换成 tokens 变量（只动颜色与字体栈，不动布局逻辑）；`styles/canvas.css` 作用域规则保留。
- [ ] **Step 5** build + 手测画布全功能（节点拖拽/生成/AI 对话/审批抽屉）。
- [ ] **Step 6** Commit: `feat(ui): dashboard, preview delivery page, canvas on router with warm-ink shell`

### Task 13: 旧壳退场 + 全链验收

**Files:** 删 `studio/ProductionView.vue`、`studio/ScriptView.vue`、`studio/StudioPanel.vue`、`studio/studio.css`（确认无引用后）；`tokens.css` 兼容别名段清零

- [ ] **Step 1** grep 确认旧 studio 视图零引用后删除；tokens.css 兼容别名段此时应为空（grep 旧变量名无命中），留空注释段即可。
- [ ] **Step 2** 连真实后端（:8000 mock）全链走查 spec §7：新建项目→导入剧本→produce→改提示词→生成→采用→首帧墙→assemble→compose→下载；画布页回归。
- [ ] **Step 3** `npm run build` + `node --test tests/**/*.test.mjs` 全绿。
- [ ] **Step 4** Commit: `chore(ui): retire legacy studio shell`

---

## Self-Review 结论

- Spec 覆盖：§2 八屏 ↔ Task 6-12；全局件 ↔ Task 4/5；§4.3 清理 ↔ Task 3/13；§4.4 AI ↔ Task 5/9；§7 验收 ↔ Task 13。无缺口。
- 类型/名称一致性：路由名、store 字段（`studio.activeSegmentId`、`studio.jobs`）、api.js 函数名均按现有代码核实。
- 关键风险已内置验证：Task 1 Step 3（旧变量名兼容）、Task 3 Step 3（组件逐个 grep）、Task 12 Step 3（画布挂接参数）、Task 13 Step 2（真实后端全链）。
