# DramaFlow 前端重设计 · 设计文档

日期：2026-10-21
状态：已批准（用户逐轮确认 demo v4 后定稿）
设计基线：`docs/design-demo/2026-10-20-redesign-demo-v4.html`（85KB，8 屏全部可交互）

## 1. 背景与目标

现有 `web-vue` 前端三代 UI 并存、风格割裂：`styles/tokens.css`（暗色）与
`styles/theme.css`（亮色）在 `:root` 定义同名变量互相覆盖，胜负取决于 import
顺序；无路由，`App.vue` 用 22 行手写 view switching；`studio/ProductionView.vue`
1382 行巨石组件；`src/skeleton/`（约 1400 行）零引用。

目标：按已批准的 v4 demo 重写前端外壳与项目内页面，统一为「暖墨 + 朱砂 + 宋体」
的专业短剧制作工具风格，页面职责按产品流程划分，AI 助手成为全局可对话的一等公民。

## 2. 信息架构（8 屏 + 3 个全局件）

主导航（项目内顶栏）：**剧本 / 制作 / 画布 / 预览**（衬线字主导航）+
右侧工具区：项目资产 / 数据看板 / 项目信息 / 规格 / 任务中心。

| 屏 | 职责（一句话） | 对应 demo |
|---|---|---|
| 首页 | 海报墙项目列表 + 新建项目（风格 Skill 卡选择） | s0 |
| 剧本 | 沉浸阅读与编辑：集列表 + 宋体大字正文 + 停靠 AI | s1 |
| 制作 | 四栏：概要 / 片段队列 / 提示词编辑器（结构化·原文双模式）/ 视频预览 | s2 |
| 首帧墙 | 整集静帧质检，批量出片前的构图关卡（制作页子页） | s3 |
| 资产 | 双栏： counted tabs（角色/场景/道具/音色）+ 详情（形象/音色/提示词/图库） | s4 |
| 数据看板 | 进度/资产/断点/成片 统计 + 动态 | s5 |
| 画布 | Agent 工作台：节点图（剧本→资产→片段→成片）+ 停靠画布 AI；双击片段节点回制作 | s7 |
| 预览 | 成片交付：海报 + 规格 + 审批/合成记录 + 下载 | s6 |

全局件：
- **AI 助手**：同一对话组件两种形态——剧本页/画布页右侧停靠；其余页面右下浮球唤起全局抽屉。对话走现有 `/api/agent` 会话协议；涉及修改的操作以 **diff 卡片**呈现，应用/放弃由用户决定。
- **任务中心**：顶栏下拉，统一展示生成 / Agent produce / 合成 job 进度（轮询 `GET /jobs/{id}`）。
- **新建项目弹窗**：含视觉风格 Skill 卡（4 张，最后一张标「规划」）、分集符号、模型与画质。

## 3. 视觉语言（token 表，唯一真源）

```
底色    --bg #0B0A09   --bg-2 #0F0E0C   --panel #121110
卡片    --card #161513 --card-2 #1B1917 --card-3 #211F1C
发丝线  --line rgba(255,251,243,.06/.11/.17)   （代替卡片描边阴影体系）
正文    --text #EDE8DF --text-2 #A89F91 --text-3 #6F685D
主色    --accent #C9553D（朱砂）  hover #DD6A50  软底 rgba(201,85,61,.13)
鎏金    --gold #C2A36B —— 仅用于对白/台词/音色/试听
状态    ok #6FA284  warn #C98A4B  bad #C25B54（低饱和，无大红大绿）
字体    标题与剧本=宋体栈 "Noto Serif SC/Source Han Serif SC/Songti SC/STSong/SimSun"
        正文=PingFang SC/MiSans/HarmonyOS Sans SC；数字与参数=ui-monospace
圆角    4 / 6 / 8；间距 4 的倍数；微标签 11px/.14em 字距
禁      渐变、emoji、投影、蓝紫色、圆角卡片阴影堆叠
按钮    .btn 9×20px/14px 起，.btn.sm 6×14px/13px（用户反馈：适当加大）
主色可切换：accent/accent-hi 通过 CSS var setProperty 换挡（朱砂/鎏金/苔青）
```

## 4. 技术方案

### 4.1 分支与范围
- 新分支 `feat/frontend-redesign`，基于 `feat/visual-canvas-v2`。
- **画布引擎不重写**：`CanvasWorkspace.vue → canvas2/* → components/NodeStudio,
  GenerationHistoryDrawer + store.js` 链路保持功能不变，仅做外壳对齐（TopBar
  配色/字体切到新 token）。画布 AI（FloatingAgent/ChatPanel）与新 AI 面板共用
  会话协议，UI 各自独立。
- Element Plus 保留（画布与既有组件依赖）；**新建页面不引入 Element Plus 组件**，
  全部手写 token CSS（用户已选定）。

### 4.2 文件结构（新建/改动）
```
web-vue/src/
  main.js                 改：挂 router；theme.css 删除后只留统一 tokens
  router.js               新：vue-router 4（依赖已存在），hash history
  styles/tokens.css       重写：上表 token 唯一真源（合并现 tokens.css 有效部分）
  styles/theme.css        删除（903 行亮色规则，冲突根源）
  styles/canvas.css       保留（作用域 .canvas-app）
  App.vue                 改：<router-view> + AppShell 外壳
  shell/AppShell.vue      新：顶栏（品牌/主导航/工具区/任务中心）+ AI 抽屉 + 浮球
  shell/TaskCenter.vue    新：任务中心下拉（jobs 轮询）
  shell/NewProjectDialog.vue  新：风格 Skill 卡 + 上传 + 分集符号
  ai/AiPanel.vue          新：共享对话组件（dock / drawer 两形态；/api/agent 流式；
                              diff 卡片渲染 + 应用→PATCH 提示词）
  views/HomeView.vue      新：海报墙
  views/ScriptView.vue    新：集列表 + 宋体正文 + 停靠 AI（content.md 可编辑）
  views/ProductionView.vue    新：四栏制作（替换现 studio/ProductionView.vue）
  views/KeyframeWall.vue  新：首帧墙（storyboard cells + stale 语义）
  views/AssetsView.vue    新：双栏资产（替换现 studio/AssetsView.vue）
  views/DashboardView.vue 新：看板
  views/PreviewView.vue   新：预览（替换现 studio/PreviewView.vue）
  stores/studioStore.js   演进：承接新页面的数据获取与 job 轮询
```
路由：`/`（首页）；`/project/:name/script|production|wall|assets|dashboard|preview`；
`/project/:name/canvas`（挂现有 CanvasWorkspace）。

### 4.3 死代码清理（删除前逐项 grep 复核）
- `src/skeleton/` 整目录（含其 main.js，唯一引用自身）
- `styles/theme.css`
- `components/` 中未被 `CanvasWorkspace.vue` / `canvas2/*` 引用者
  （引用面核查：目前仅 NodeStudio、GenerationHistoryDrawer 被画布引用；
  FloatingAgent/ChatPanel/AssistantMessage 等被 CanvasWorkspace 引用，保留）
- `App.vue` 手写 view switching（被 router 替代）

### 4.4 AI 面板与会话
- 协议复用：`POST /api/sessions`（建会话）→ `POST /api/agent`（SSE 流式）→
  `/api/agent/cancel`、`/api/agent/followup`；断线 `GET /api/sessions/{id}/events/.../resume`。
- 会话按画布（项目）维度 bind，与画布页共享上下文。
- **diff 卡片**是本设计的核心交互：AI 建议修改提示词字段时，前端渲染
  旧值（划线）/新值 + 应用/放弃。应用 = 调 `POST /segments/{id}/prompt`
  （人改优先，服务端不再覆盖）+ toast 反馈。放弃 = 仅丢弃卡片。
- 面板按所在页注入上下文快捷指令（剧本页：节奏分析/分集；制作页：改提示词/机位；
  画布页：批量重生成/一致性检查）。

## 5. API 映射（全部为现有后端能力）

| 界面 | 端点 |
|---|---|
| 首页/项目 | GET/POST /projects、POST /import-script |
| 剧本 | GET /script、编辑保存走 POST /import-script（重新分集+告警即保存语义，demo「编辑剧本→重新分集」）；轻量「保存不重分」需新增 PUT /script（小后端补充，可延后） |
| 制作·提示词 | GET /segments(/{id})、POST /segments/{id}/prompt（人改） |
| 制作·校验 | generate 422 detail.findings 内联展示；force 跳过需显式确认 |
| 制作·生成 | POST /segments/{id}/generate、GET /jobs/{id} 轮询 |
| 制作·版本 | POST /segments/{id}/take（采用）、tags；重生成不抢占采用版 |
| 首帧墙 | GET /episodes/{no}/storyboard、POST /segments/{id}/keyframe |
| 资产 | GET /assets、PATCH /assets/{id}（音色等）、looks / looks/new / generate |
| 预览 | POST /assemble、approvals、POST /compose、渲染历史 |
| 会话 | /api/sessions*、/api/agent* |

## 6. 交互语义不变量（实现必须保持）

1. **人改优先**：字段编辑保存后标记 human，服务端不覆盖（demo 中 toast 明示）。
2. **采用语义**：首次生成自动采用；重生成只追加版本、绝不抢占；采用动作显式。
3. **过期可见**：分镜/形象变更后旧素材标「过期·分镜已变更」，不自动重做，处理动作由用户触发。
4. **硬校验显式**：lint 拦截内联红条 + 快捷修复，不静默失败；force 必须用户主动。
5. **AI 不落笔**：一切 AI 修改先 diff 后应用。
6. **鎏金=声音**：对白、台词、音色一律 gold 色系。

## 7. 测试与验收

- `npm run build` 通过；`npm run dev` 下逐屏人工走查（对照 demo 8 屏）。
- 连真实后端（:8000，mock provider）验证：新建项目→导入剧本→produce→改提示词→
  生成→采用→首帧墙→assemble→compose 全链可点通。
- 回归：画布页全功能可用（节点/生成/AI 对话/审批）。

## 8. 非目标（demo 中标「规划」角标，本期不做）

- 港风霓虹等更多风格 Skill 包
- 更换风格后一键重渲染全片提示词
- 团队资产库复用、提审本集、数据看板成本估算
