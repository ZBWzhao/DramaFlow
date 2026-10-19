# 画布可复用源码：逐文件「能搬 / 要改 / 只借鉴」清单

> 用途：画布重做（节点即工具契约）前，先明确从两个开源项目里**哪个文件直接拿来、哪个要改、哪个只能看**，避免开工后返工。
> 本文只做清单与结论，不含代码改动。

## 0. 结论速览

| 来源 | 文件数 | 直接搬 | 改后搬 | 只借鉴 | 不采用 |
| --- | --- | --- | --- | --- | --- |
| `HBAI-Ltd/Toonflow-app` 画布交互层 | 22 | 4 | 12 | 3 | 3 |
| `869413421/ai-moive-studio`(AICON) 画布组件 + 依赖 | 20 | 5 | 7 | 7 | 1 |

**一句话：Toonflow 给我们「画布的骨骼」（菜单、选区、素材库、自动布局、撤销）；AICON 给我们「单卡工作台的肉」（图片/视频/文本三个 Studio + @ 引用 prompt 编辑器 + 生成中遮罩 + 历史版本抽屉）。** 两者都在 Vue3 + Element Plus 上，且都不需要我们换画布底座。

## 1. 源码位置（本次已拉取到本地，未改动任何业务代码）

- Toonflow（用户本地已有）：`C:\Users\Lenovo\Desktop\Toonflow-app`，HEAD `f37b772 2026-10-02`，655 文件 / 18.6 MB，remote `https://github.com/HBAI-Ltd/Toonflow-app.git`
  - 画布层：`apps/web/src/pages/workspace/panels/canvas/`
  - 快捷键：`apps/web/src/lib/canvasShortcuts.ts`、画布文件规则：`apps/web/src/pages/workspace/canvasFile.ts`
- AICON（本次快照到临时目录，可直接看）：
  - 组件：`tmp\vendor\aicon-canvas\`（12 个 `.vue`）
  - 依赖：`tmp\vendor\aicon-deps\`（8 个 `.js`：composables + utils）
  - 清单：`tmp\aicon-canvas-manifest.json`；Toonflow 导入表：`tmp\toonflow-canvas-imports.json`

> 想长期留存可以把 `tmp\vendor\` 挪到 `docs\vendor\`，但那会把第三方源码放进仓库，建议保持 `tmp`（已被 git 忽略）。

## 2. 依赖差量（决定「能不能直接搬」的关键）

我们 `web-vue/package.json` 现有：`@vue-flow/{core 1.48.2, minimap 1.5.4, background 1.3.2, controls 1.1.3}`、`element-plus 2.14.7`、`@element-plus/icons-vue`、`vue 3.5.43`、`marked`、`dompurify`。

| 对方依赖 | 用途 | 我们的处置 |
| --- | --- | --- |
| `@vue-flow/core` `minimap` `background` | 画布底座 | ✅ 已有，同大版本 |
| `element-plus` | UI 组件 | ✅ 已有 |
| `@element-plus/icons-vue`（AICON 用） | 图标 | ✅ 已有 |
| `@tabler/icons-vue`（Toonflow 用） | 图标 | ⚠️ **不加新依赖**，统一换成 `@element-plus/icons-vue`（两者图标语义可对应） |
| `@dagrejs/dagre ^3.1.1` | 自动布局（`arrangeCanvas`） | ➕ **建议新增**（MIT，纯算法，几十 KB） |
| `lodash-es`（`debounce`） | 画布防抖 | ⚠️ 不新增，自己写 20 行 `debounce` |
| `axios` | 请求 | 🔁 换成我们 `web-vue/src/api.js`（`getJSON/postJSON/postFile`） |
| `pinia` | 状态 | 🔁 换成我们 `web-vue/src/store.js`（reactive 单例） |
| `tdesign-vue-next`（`QRCode`） | 控制条里的二维码 | ❌ 丢弃该入口 |
| `dockview-vue` / `three` / `@tiptap/*` / `@tanstack/vue-virtual` | 面板/导演台/富文本/虚拟列表 | ❌ 本期不引入（需要时再单独评估） |
| `@toonflow/{nodes-scaffold,tool-canvas,tools-scaffold,server,i18n}` | 它自己的内核 | ❌ **绝不引入**，这层是「改后搬」的主要工作量 |

**结论**：新增依赖只有 `@dagrejs/dagre` 一个，其余全是「换实现」。

## 3. 清单 A：Toonflow 画布交互层（`apps/web/src/pages/workspace/panels/canvas/`）

判定口径：
- **直接搬** = 只依赖 `@vue-flow` + `element-plus` + `vue`，拷过来改 import 路径即可
- **改后搬** = 逻辑可照搬，但要替换依赖（axios→api.js、pinia→store.js、`@toonflow/*`→我们自己的 node spec）
- **只借鉴** = 读设计、重写实现
- **不采用** = 与我们的架构冲突或本期不需要

| # | 源文件 | 行数 | 依赖 | 判定 | 落地目标 / 改造点 |
| --- | --- | --- | --- | --- | --- |
| A1 | `index.vue` | 1028 | pinia、axios、`@toonflow/*`、lodash | 只借鉴 | 画布主容器。只抄它的 `<VueFlow>` 参数与事件绑定范式（`@dblclick` 开节点菜单、`@wheel.capture` 缩放、`@pointerdown.capture` 更新选区、`@dragover/@drop` 收文件），主体留在我们的 `web-vue/src/components/CanvasPanel.vue` |
| A2 | `canvasHost.vue` | 183 | `@toonflow/tool-canvas`、`@toonflow/server/agent/mentionSources` | 只借鉴 | 它负责「画布上下文 + @ 引用节点」。@ 引用改由 AICON 的 `CanvasPromptMentionEditor` 承担，本文件不搬 |
| A3 | `canvasQueries.ts` | 306 | `@toonflow/*` runtime | 不采用 | MCP 画布工具调用层，我们没有对应协议 |
| A4 | `canvasDrop.ts` | 91 | axios、`@toonflow/nodes-scaffold/workspaceFiles` | **改后搬** | 拖文件进画布 → 上传 → 建节点 → 自动连线。改 axios→`api.js postFile`、uploads→`store.uploadMedia`（已存在），落点 `web-vue/src/utils/canvasDrop.js` |
| A5 | `arrangeCanvas.ts` | 91 | `@dagrejs/dagre` + `@vue-flow` 类型 | 改后搬 | **注意**：全局整理服务端已有（`POST /api/canvas/arrange` → `core/canvas.py: arrange()` 拓扑分层）。这个文件只在「**选区局部整理**」时用得上 |
| A6 | `arrangeSelection.ts` | 76 | `@vue-flow` 类型 | **直接搬** | 只整理选中节点；服务端没有选区整理，落点 `web-vue/src/utils/arrangeSelection.js` |
| A7 | `selectionNodes.ts` | 108 | 它的 node 类型/spec | 改后搬 | 选区判定（框选命中、组、可否删除/整理），改成读我们 `core/canvas.py` 的 `NODE_SPEC` 镜像 |
| A8 | `selectionConnections.ts` | 42 | `@vue-flow` | **直接搬** | 选区涉及的内外连线收集，落点 `web-vue/src/utils/selectionConnections.js` |
| A9 | `nodeClipboard.ts` | 91 | 它的 node/edge 结构 | 改后搬 | 复制/粘贴节点（含连线、相对偏移），适配我们的 `node/edge` JSON，落点 `web-vue/src/utils/nodeClipboard.js` |
| A10 | `useCanvasHistory.ts` | 167 | 它的画布状态 + 服务端保存 | 改后搬 | 撤销/重做（batch 合并）。我们都走 `/api/canvas/nodes` 的 revision 乐观锁，先做**本地栈**（快照 nodes+edges），落点 `web-vue/src/composables/useCanvasHistory.js` |
| A11 | `useCanvasTools.ts` | 250 | i18n、通知 | 改后搬 | **高价值**：工具模式（选择/抓手/连线/加节点/框选）+ 快捷键状态机，落点 `web-vue/src/composables/useCanvasTools.js` |
| A12 | `loadNodeComponent.ts` | 41 | 动态加载扩展包 | 不采用 | 我们节点类型是静态的（`NODE_SPEC` 11 种），用静态映射表即可 |
| A13 | `components/canvasControls.vue` | 342 | `@vue-flow/core` `Panel`、MiniMap、tdesign QRCode | 改后搬 | 右下控制条：缩放、磁性对齐、居中、全图。删掉二维码/帮助弹窗，图标换 element-plus |
| A14 | `components/canvasMenu.vue` | 544 | axios、workspace store、文件系统 | 只借鉴 | 画布级菜单（新建/重命名/另存/导出）。我们已有 `/api/canvases` 与 `store.deleteCanvas`，按需重写 |
| A15 | `components/nodeMenu.vue` | 409 | `@toonflow/nodes-scaffold/runtime`（握手兼容校验）、`el-dropdown` | **改后搬** | **核心**：双击画布/选区右键 → 4 级菜单（加节点 / 选区操作 / 整理 / 动作）。端口兼容校验 `isTypeCompatible` 用我们 `NODE_SPEC` 的 in/out 端口实现 |
| A16 | `components/nodeSearch.vue` | 123 | `@vue-flow` + `el-input` | **直接搬** | Cmd/Ctrl+K 搜节点并居中，落点 `web-vue/src/components/NodeSearch.vue` |
| A17 | `components/selectionHandle.vue` | 176 | `@vue-flow`（`getBezierPath`/`getRectOfNodes`）、connection 类型 | 改后搬 | **「括号式收束连线」的关键参考**：从选区边缘拖一根线出去连到目标节点，多个来源可以并成一股 |
| A18 | `components/selectionToolbar.vue` | 291 | `@vue-flow`、`useNodeEvent` | **直接搬**（去掉事件钩子） | 选区浮动工具条：创建副本 / 整理 / 取消选择，落点 `web-vue/src/components/SelectionToolbar.vue` |
| A19 | `components/groupNode.vue` | 224 | `@vue-flow`、node model | 改后搬 | 组节点（框选成组、组内折叠），对应「一组资产共同决定一个镜头」 |
| A20 | `components/remoteNode.vue` | 67 | 扩展包 runtime | 不采用 | 远程/插件节点兜底组件，我们没有动态节点 |
| A21 | `components/assetLibrary.vue` | 504 | axios、`@toonflow/nodes-scaffold/values`、`@tabler/icons-vue` | **改后搬** | **素材库**：文件夹树（`el-tree`）+ 搜索 + 缩略图懒加载 + 拖入画布 + 双击预览 + 右键菜单。axios→`api.js`，outputs 结构→我们的 `node.outputs`，图标换 element-plus |
| A22 | `components/assetMenu.vue` | 196 | axios、i18n、saveFile | 改后搬 | 素材右键菜单（重命名/删除/下载/新建文件夹） |

补充（不在该目录但需要）：`apps/web/src/lib/canvasShortcuts.ts`（快捷键表，改后搬）、`apps/web/src/pages/workspace/canvasFile.ts`（画布与资产目录约定，只借鉴）。

## 4. 清单 B：AICON 画布组件（`frontend/src/components/canvas/`）

AICON 的文件外部依赖极少（多数零依赖，或只依赖 `@element-plus/icons-vue`），是**移植性价比最高的一批**。

| # | 源文件 | 行数 | 依赖 | 判定 | 落地目标 / 改造点 |
| --- | --- | --- | --- | --- | --- |
| B1 | `CanvasImageStudio.vue` | 729 | `@/composables/useCanvasStudioCommitBoundary`、`CanvasPromptMentionEditor`、`CanvasGeneratingOverlay` | **改后搬** | **单图工作台（本期核心）**。顶栏=类型+可编辑标题+上传；预览卡=图/生成中遮罩；停靠面板=`@` 引用 prompt 编辑器 + 模型/比例工具条；emits `commit/delete/generate/history/upload` 直接映射到我们 `store.updateNode / deleteNode / runCanvas(only=[id])` 与 `/api/studio/assets/{id}/generate` |
| B2 | `CanvasVideoStudio.vue` | 661 | 同上 | **改后搬** | 视频版工作台，映射 `/api/studio/segments/{id}/generate` + `/keyframe` |
| B3 | `CanvasTextStudio.vue` | 589 | 同上 + `canvasStageMedia`（`resolveCanvasRichTextHtml`） | **改后搬** | 文本/提示词编辑（富文本查看），我们用 `marked` + `dompurify`（已有）替代它的富文本解析 |
| B4 | `CanvasPromptMentionEditor.vue` | 826 | 无（只依赖 vue） | **改后搬** | **亮点**：prompt 里输入 `@` 引用画布上其它节点（资产图/参考视频/文本），token 化渲染。要把它的 token 结构换成我们的 `node.outputs` 引用 |
| B5 | `CanvasGeneratingOverlay.vue` | 152 | 无 | **直接搬** | 生成中遮罩（「AI 正在生成图像 / 预计 30 秒至 2 分钟」），零依赖 |
| B6 | `CanvasGenerationHistoryDrawer.vue` | 204 | 无 | **直接搬** | 候选版本/生成历史抽屉（props `visible/title/subtitle/items`，emits `refresh/select`）→ 接 `server/studio.py` 已有的 `data.takes` + `/take` |
| B7 | `CanvasNodeCard.vue` | 236 | 无 | 只借鉴 | 它是 Konva 画布的自绘卡片；我们卡片是 VueFlow 的 `ToonNode.vue`，只借鉴其「图优先 + 标题压图 + 状态点」结构 |
| B8 | `CanvasConnectionActions.vue` | 49 | `@element-plus/icons-vue` | **直接搬** | 连线中点上的删除按钮 |
| B9 | `CanvasLinkCreateMenu.vue` | 82 | 无 | **直接搬** | 拖线松手后「要连到哪个节点 / 新建什么节点」的菜单 |
| B10 | `CanvasLinkDragOverlay.vue` | 36 | 无 | **直接搬** | 拖拽连线的路径预览层 |
| B11 | `CanvasWorkbenchLayout.vue` | 405 | `@element-plus/icons-vue` | 只借鉴 | 顶栏+左侧工具栏框架；我们已有 `CanvasWorkspace.vue`，只取「左侧竖排工具栏」布局 |
| B12 | `KonvaCanvasStage.vue` | 903 | konva / vue-konva | 不采用 | 它的自研 Konva 画布，我们继续用 VueFlow |

配套 helper（`frontend/src/composables|utils/`）：

| # | 源文件 | 行数 | 判定 | 说明 |
| --- | --- | --- | --- | --- |
| C1 | `useCanvasStudioCommitBoundary.js` | 98 | **改后搬** | 工作室的「提交边界」：编辑草稿 → 失焦/回车才 PATCH，避免每敲一个字都打接口。**必搬**，否则 B1–B3 会疯狂请求 `/api/canvas/nodes/{id}` |
| C2 | `canvasStageMedia.js` | 383 | 改后搬 | 媒体 URL 解析/尺寸/富文本 HTML。我们 `web-vue/src/utils.js` 已有 `mediaOf/assetUrl`，按需补 |
| C3 | `canvasGenerationHistory.js` | 84 | 改后搬 | 历史项构造（时间/模型/状态文案） |
| C4 | `canvasImageNodeLayout.js` | 92 | 只借鉴 | Konva 卡片尺寸计算，我们用固定 `--node-w` |
| C5 | `canvasVideoNodeLayout.js` | 108 | 只借鉴 | 同上 |
| C6 | `canvasGenerationPayload.js` | 92 | 只借鉴 | 生成参数组装；我们有 `/api/canvas/generation-plan`，不重复造 |
| C7 | `useCanvasGeneration.js` | 382 | 只借鉴 | 生成提交/轮询状态机；我们已有 `runCanvas` 与 job 轮询 |
| C8 | `useCanvasEditor.js` | 422 | 只借鉴 | 编辑器状态（选中/拖拽/连线）；VueFlow 已提供大部分 |

## 5. 适配层设计（改造的真正工作量）

两边都缺的「胶水」，是我们自己写的一层，约 400–600 行：

1. **`web-vue/src/composables/useNodeSpec.js`**（新）：把 `core/canvas.py` 的 `NODE_SPEC`（11 种类型的 in/out 端口、颜色、图标、可用工具）暴露给前端，替代 Toonflow 的 `@toonflow/nodes-scaffold`。供 A15/A17/A21 与 Studio 的工具条使用。
2. **`web-vue/src/composables/useCanvasContext.js`**（新）：`provide` 画布上下文（`nodes/edges/selection/viewport/actions`），替代 Toonflow 在 `index.vue` 里 provide 的 `canvas`、`nodeConfig`、`batchCanvasHistory`、`copyNodeToClipboard`、`reloadRemoteNode`、`retainNodeFiles`、`canvasAssetNodes`、`canvasList`。
3. **Studio draft 映射表**（新，约 120 行）：AICON 的 `draft`/`item` 契约 ↔ 我们的 node JSON。

   | AICON draft 字段 | 我们的 node 字段 |
   | --- | --- |
   | `title` | `node.label` |
   | `promptPlainText` / `promptTokens` | `node.data.prompt` + `node.data.prompt_tokens`（新） |
   | `model` | `node.data.model` |
   | `aspectRatio` | `node.data.aspect_ratio` |
   | `resultImageUrl` / `resultVideoUrl` | `node.outputs.image.value.path` / `node.outputs.video.value.path`（经 `assetUrl()`） |
   | `referenceImageUrl` / `styleReferencePreviewUrl` | `node.data.references[]`（新） |
   | `apiKeyId` | 不落地（我们用项目级 provider 配置） |

4. **生成调用适配**（约 80 行）：`generate` emit → 单卡执行 = `/api/canvas/run` 传 `only: [nodeId]`（服务端已支持）或 `/api/studio/segments/{id}/generate`（首选，有硬校验与 takes）；`history` emit → 读 `node.data.takes`。

### 5.1 服务端要补的东西（很少，但必须补，否则前端搬了也没用）

| 缺口 | 现状 | 要补 |
| --- | --- | --- |
| **删节点** | 只有 `POST /api/canvas/nodes/{node_id}`（更新）；`server/app.py` 没有节点级 DELETE，前端 `store.js` 也没有 `deleteNode` | `DELETE /api/canvas/nodes/{node_id}` + `store.deleteNode()`。这是选区工具条「删除选中节点」（A18）与 Studio 的 `delete` emit（B1–B3）的前提 |
| **选区整理** | `POST /api/canvas/arrange` 是**全局**拓扑分层（`core/canvas.py: arrange()`） | 二选一：前端 dagre 算好位置后批量 `POST /api/canvas/nodes/{id}/position`；或加 `POST /api/canvas/arrange-selection` |
| **节点级 take 列表** | `/api/studio/segments/{id}/take` 已有，但画布侧没有读 takes 的路由 | 画布详情接口把 `data.takes` 透出即可（`data` 本就直通，无需新接口） |
| **prompt token / 参考图** | 无 | `data.prompt_tokens`、`data.references` 两个字段（`data: dict` 自由扩展，**不需要迁移**） |

## 6. 分期与工作量（按「看得见的收益」排序）

| 期 | 内容 | 复用文件 | 工期 |
| --- | --- | --- | --- |
| **P0** | 图优先卡片 + 收束曲线连线 + 单卡工作台（图/视频/文本三态）+ 生成中遮罩 + 历史抽屉 | B1 B2 B3 B4 B5 B6 C1 C3 + 现有 `ToonNode.vue`/`canvas.css` | 3–4 天 |
| **P1** | 双击画布加节点菜单、节点搜索、选区工具条、自动布局（dagre）、撤销重做 | A15 A16 A18 A5 A6 A8 A10 A11 A13 | 3–4 天 |
| **P2** | 左侧素材库 + 拖入画布、选区拉线（括号式收束）、组节点、复制粘贴、连线中点删除/新建菜单 | A21 A22 A4 A17 A19 A9 B8 B9 B10 | 3–4 天 |
| **P3** | 快捷键、空画布引导、画布级菜单（新建/重命名/导出）、视觉收尾 | `canvasShortcuts.ts` A14、A13 收尾 | 1–2 天 |

合计约 **10–14 人天**（一人独做按 2–3 周算）。服务端改动很小但有几处必补项（见 §5.1）：节点删除路由、选区整理、把 `data.takes` 透出；`data` 是自由 dict，`prompt_tokens` / `references` **不需要迁移**。

## 7. 许可与合规

- Toonflow-app：**MIT**（`Copyright (c) 2026 HBAI-Ltd`）。搬代码需在文件头或 `THIRD_PARTY_NOTICES` 保留原版权与许可声明。
- AICON（`ai-moive-studio`）：**Apache-2.0**。需保留 `NOTICE`，修改过的文件需注明改动。
- 两者都是「同赛道产品」：只复用**代码与交互范式**，不复制它们的文案/图标/品牌色，避免"看起来像同一个产品"。

## 8. 不采用的清单（省得以后再讨论）

| 文件/依赖 | 理由 |
| --- | --- |
| `canvasQueries.ts`、`remoteNode.vue`、`loadNodeComponent.ts` | 依赖 Toonflow 的插件/MCP/远程节点内核，我们没有这套机制 |
| `KonvaCanvasStage.vue`、`canvasImageNodeLayout.js`、`canvasVideoNodeLayout.js` | 自研 Konva 画布，与 VueFlow 互斥；我们的 LOD 已用 CSS 实现 |
| `useCanvasGeneration.js`、`canvasGenerationPayload.js` | 生成编排我们服务端已有（`/api/canvas/run`、`generation-plan`、`/studio/segments/{id}/generate`），重复造会分叉 |
| `dockview-vue`、`three`、`@tiptap/*`、`tdesign-vue-next`、`pinia`、`axios`、`@tabler/icons-vue`、`lodash-es` | 要么我们不缺，要么引入成本大于收益（图标可换 element-plus，防抖自己写） |

## 9. 每期怎么算「搬成功了」

- **P0**：点开一张资产卡 → 出现该节点的工具条与对话框 → 改 prompt 后点「生成」只跑这一张（网络面板里 `only=[<nodeId>]` 或 `/studio/.../generate` 只发一次）→ 生成中可见遮罩文案 → 完成后卡片图更新、历史抽屉里多一版。
- **P1**：双击画布空白 → 弹出「加节点」菜单并可建立 image_gen/video_gen；Ctrl+K 能搜到节点并居中；框选 3 张卡 → 工具条出现「创建副本 / 整理 / 删除」；撤销能回退到上一步。
- **P2**：从左侧素材库拖一张图到画布 → 自动上传并落成 image 节点；框选 5 张资产 → 从选区边缘拉一根线 → 收束到同一个 prompt 节点（括号式）；复制粘贴一组节点连线一起过来。
- **P3**：`?`/`H` 打开快捷键表；空画布有引导；`npm run build` 体积增量 < 60 KB（除 dagre）。
