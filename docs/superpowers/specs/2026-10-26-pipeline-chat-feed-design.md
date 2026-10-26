# 管线运行对话流设计：制片台 RunChat（微信式气泡 × 站内风格）

> 状态：待用户审（2026-10-26）
> 动机：用户原话「能不能把管线也改成对话Agent的样子，现在输入输出的显示都很麻烦，且不直观」；指定方案 B（产物融入对话流，四页签保留）+ 微信式气泡（头像两侧、气泡贴头像、内边距紧凑、时间/系统行居中），「注意风格统一，主要是字与边框间距的问题，还有消息显示的问题」。
> 范围：纯前端。后端、事件契约、store 数据模型零改动（失败免疫 spec 的 additive 事件键仅按可选键消费）。

## 1. 页面结构

```
制片台 · 管线产物页（PipelineView.vue）
├─ 入口卡（CreateRunPanel ①，现状不动）——开始后小说文本成为流里第一条用户气泡
├─ RunChat（新，替换 CreateRunPanel 的 ②运行卡 + ③gated 大纲悬浮区）
├─ 四页签产物区（现状完全不动）
└─ 运行详情抽屉（现状不动）
```

CreateRunPanel 退化为：① 入口卡 + 「本项目有活动 run 时整块让位给 RunChat」；②③ 的全部交互（确认门/意见/失败草稿编辑/重试）**原样迁移**为 RunChat 流内交互卡。

## 2. 消息分组规则（`studio.run.events` 正序 → 消息列表）

| 事件帧 | 渲染 |
|---|---|
| （运行起点） | 右侧用户气泡：「你」头像 + 小说全文，**默认折叠 6 行**，点击展开 |
| `stage` | 新开一条左侧 Agent 消息，头部「{阶段名} · 运行中 {计时}s」 |
| `tool` / `tool_result` | 消息内可折叠工具卡：折叠头 = 图标 + 工具名 + args_summary + 状态；展开 = 回执全文 + 复制按钮 |
| `stage_gate` | 消息内状态行：未过（红，「第 N 轮质量门：未过」）/ 通过（绿）/ 通过 · N 条内容标记（鎏金，advisory） |
| `stage_done` | 消息结束章：✓ 产物已落库 · 用时；`degraded: true` 时改 ⛔「产物已落库 · N 项阻断标记（见产物卡）」 |
| 修复轮 | 不新开消息——同一阶段消息继续追加工具卡 + 下轮门行 |
| `hello` / 重连恢复 | 居中系统行（灰）：连接/快照状态；恢复态追加「已接入运行现场，此前过程不回放」 |
| `gate`（run 级确认门） | **确认门交互卡**（鎏金）：迁现有全部动作——「查看大纲并确认」滚动、「确认，开写剧本」唯一放行入口、意见记录框；卡内嵌大纲产物卡（PipelineOutlinePanel + findings 标记） |
| `failed` | 失败交互卡（红）：findings 列表 + 折叠草稿编辑器 + 盲重试/带修改重试，逻辑逐字迁移 |
| `done` | 完成卡：全部阶段正常 → 绿「本轮运行完成」；有 degraded 阶段 → 黄「完成 · N 个阶段带 ⛔ 阻断标记」 |
| 未知类型 | 灰色小气泡渲染 `JSON.stringify` 截 160 字——绝不让整流崩掉 |

顺序规则：**正序**（旧运行卡是倒序，废）；同阶段内事件按到达序追加。

## 3. 气泡规格（微信间距 × DramaFlow 变量）

- 行结构：头像 36px 圆 + 气泡，气泡贴头像 10px；「你」= accent 底白字靠右，「管线」= `--card-2` 底 + 工具图标靠左
- 气泡：`padding: 9px 13px`；圆角 10px、贴头像侧 4px 小角；`max-width: 68%`
- 文字：`--fs-13`、`line-height: 1.65`；工具回执/JSON 用 `--mono` + `word-break` 防撑爆
- 配色：用户气泡 `color-mix(in srgb, var(--accent) 12%, var(--card))`；Agent 气泡 `var(--card)` + `1px var(--line)` 边——不引入站外色
- 系统行/分隔：居中、`--fs-11`、`--text-3`
- 产物卡：头像行内的全宽卡（不受 68% 限制），头部 = 产物名 + 一句摘要 + 展开收起；内容区复用现有面板组件（CastPanel/ArtPanel/StagingPanel/StoryboardPanel + GateMarkers，props 原样）

## 4. 活的信号（照搬 ChatPanel 成熟三件套）

1. 活动阶段消息头部实时计时 + 当前活动行
2. 消息末尾常驻「执行中」行（哪怕已有工具卡）
3. 12s 无新事件提示「已 Ns 无新事件」（200ms tick，仅运行中走）
4. 断线横幅：`link=reconnecting`（黄「直播连接断开，自动重连中…」）/ `lost`（红，迁现有文案）置于流顶

## 5. 边界态

- F5 恢复（events 不回放）：系统行 + 当前状态卡，不留白屏
- 全程空 feed（新建 run 未连上）：占位行「正在连接执行器，等待第一帧…」
- 未知事件：见 §2 末行
- 超长内容：小说 6 行折叠；工具回执展开区内滚；findings 沿用 GateMarkers 200 字截断
- 归属红线沿用：`run.project_id === studio.canvas` 才渲染流，A 项目 run 绝不出现在 B 项目页

## 6. 组件与文件

```
web-vue/src/views/pipeline/chat/
├─ RunChat.vue          # 流容器：events→消息分组 computed、正序滚动、stick-bottom、断线横幅
├─ ChatAvatar.vue       # 36px 圆头像（你/管线两态）
├─ UserNovelBubble.vue  # 用户小说气泡（6 行折叠）
├─ StageMessage.vue     # 阶段消息：头部计时 + 工具卡/门行/结束章聚合
├─ ToolCard.vue         # 工具调用折叠卡（复刻 ToolPart 视觉，props 驱动、不碰 store.js）
├─ GateRow.vue          # stage_gate 状态行（含 advisory/阻断两态）
├─ SystemNote.vue       # 居中系统行
├─ GateConfirmCard.vue  # 确认门交互卡（迁 CreateRunPanel gate 段 + 大纲产物卡）
├─ FailCard.vue         # 失败交互卡（迁失败段：findings+草稿编辑+重试）
├─ DoneCard.vue         # 完成卡（含 degraded 黄态）
└─ ArtifactCard.vue     # 产物卡（复用四面板 + GateMarkers + PipelineOutlinePanel）
```

- **禁改**：`store.js`、`canvas2/`、`ai/`、`components/ChatPanel.vue` 及其子件（AssistantMessage/ToolPart/ThinkingPart/SubAgentPart/QuestionCard/MarkdownText）——只复刻视觉模式，全部新组件 props 驱动
- 修改：`CreateRunPanel.vue`（删 ②③ 留 ①）、`PipelineView.vue`（挂 RunChat）、`GateMarkers.vue`（severity=blocking 的 ⛔ 样式变体，小改）、`studioStore.js` 仅当失败免疫 spec 落地时同步消费 `degraded/blocking` 可选键（纯前端本 spec 不改 store）
- 路由/页面骨架/四页签面板零改动

## 7. 红线

- 确认门：放行唯一入口「确认，开写剧本」不可跳过——迁移时逐字保留文案与行为
- 事件数据不做前端重组存储：消息列表是 `run.events` 的纯 computed 派生，store 事件账本结构不动
- 后端零改动；本 spec 不含任何 API/DB 变更

## 8. 测试与验收

- `cd web-vue && npm run build` 零错误；全套 pytest 不受影响（纯前端）
- CDP 真机（一次性项目）：
  1. 真模型跑全程：流正序渲染、阶段消息聚合、工具卡折叠、确认门卡唯一入口、产物卡随 stage_done 出现
  2. SQL 造 findings（advisory + blocking 两种 severity）→ 产物卡/完成卡标记渲染正确
  3. F5 恢复态：系统行 + 状态卡不留白
  4. CDP 注入未知事件帧 → 灰气泡不崩
  5. 截图自检（demo 硬套教训：空态/等待/错误态齐备）
