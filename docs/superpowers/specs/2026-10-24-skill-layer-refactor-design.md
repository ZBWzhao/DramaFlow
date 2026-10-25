# DramaFlow 技能层重构设计：管线为主体，画布末端化

- 日期：2026-10-24
- 状态：已评审（方向获确认，本文为定稿）
- 实施基地：Desktop 主检出（`C:\Users\Lenovo\Desktop\toonflow-py-lab`，studio 制片台与剧本页仅在该检出）；本文档提交于 orca worktree 快照分支，随 squash 回流主检出

## 1. 背景与问题

系统最初只有画布，Agent 与 SKILL 均围绕画布构建：

- `teams/dramaTeam` 六个成员全部持有画布工具（`get_canvas/add_nodes/set_text/set_storyboard`），编剧的职责是"把剧本写进画布 text 节点"；
- `skills/` 下 `drama_script`/`storyboard`/`asset_design`/`shot_prompt` 本质是画布操作手册，描述"如何操作编辑器"而非"如何生产短剧"；
- 后加的剧本页（`web-vue/src/views/script`）、制片台（`web-vue/src/studio`）复用画布 Agent，剧本页 AI 助手的"大脑"仍是画布 Agent——产品管线（小说→大纲→剧本→分镜→成片）从未成为一等公民。

目标形态（已确认）：**终端用户在网页里从导入小说到出片全自动**；shuohao-skills（github.com/eternityspring/shuohao-skills）作为主力内容生产技能集；画布降级为系统末端的一个视图，存量问题冻结。

## 2. 设计原则

1. **产物即契约**：shuohao 各层产出的 JSON 即系统数据模型，先定契约再写代码；
2. **画布冻结**：不修、不删、不接新功能；旧画布技能原样保留供画布使用；
3. **每阶段有验收线**：达不到不进下一阶段；
4. **路径 = 先体外后体内**（已确认）：第一阶段在 Claude Code 中生产产物、系统导入消费；第二阶段技能收编进产品 Agent 运行时；数据契约两阶段不变，前端零改动升级。

## 3. 目标架构

```
终端用户（网页）
   │ 粘贴小说 / 确认大纲 / 触发制片
   ▼
剧本页（大纲、剧本）        制片台（角色、美术、站位、分镜、成片）
   └────────────┬───────────────┘
                ▼
   产品 Agent 运行时（阶段 2 起）
   单编排 Agent + 阶段技能（novel-outline → novel-script
   → novel-characters / novel-art / novel-staging
   → novel-storyboard → character-refs 出图）
   每层：结构化 JSON 产物 + 脚本质量门 + 评审报告
                ▼
   Postgres（project_id 关联的产物表）→ 媒体管线（图/视频/TTS/合成）
                ▼
   画布（阶段 4 才回归：分镜/资产可视化编辑器，消费产物）
```

### 技能集划分（新旧映射）

| 管线环节 | 主力技能（shuohao） | 旧技能处置 |
|---|---|---|
| 大纲 | `novel-outline` | `drama_script` 标 legacy（画布专用） |
| 剧本 | `novel-script` | 同上 |
| 角色 | `novel-characters`（提示词）、`character-refs`（出图，阶段 3） | `asset_design` 标 legacy |
| 美术 | `novel-art` | 同上 |
| 站位 | `novel-staging`（旧系统无此层，纯新增） | — |
| 分镜+视频提示词 | `novel-storyboard` | `storyboard`、`shot_prompt` 标 legacy |
| 画布编排 | （阶段 4 再议） | `workflow` 保留 |
| 工程审阅 | — | `code_review` 保留（与管线无关） |

## 4. 数据契约

- 7 类产物 JSON：`outline.json`、`script.json`、`characters.json`、`art.json`、`staging.json`、`storyboard.json`、`asset.json`（character-refs）。阶段 0 逐个审阅字段后定稿字段清单；
- 存储：Postgres，每类产物一张表（或统一 `pipeline_artifacts` 大表，阶段 0 定），字段含 `project_id` 外键、`schema_version`、`content jsonb`、`created_at/updated_at`；
- `schema_version` 应对 shuohao 上游升级；导入时校验 + 版本迁移；
- Markdown 评审报告与 HTML 工具页作为派生物存储（对象引用或文件路径），非契约主体。

## 5. 阶段规划

### 阶段 0：定契约、落技能（约 2–3 天）

任务分解：
1. 将 shuohao 7 技能 vendored 进仓库 `pipeline_skills/`（不进 `skills/`，避免画布装载器扫描；原样拷贝记录上游 SHA）；
2. 逐个审阅 7 类产物 JSON schema，输出字段清单文档；
3. 设计 Postgres 表并撰写 migration 草稿；
4. 撰写数据契约文档（schema→表映射、导入/导出与幂等规则）；
5. 选定样例小说，在 Claude Code 中端到端跑通技能链，产物留作阶段 1 测试夹具。

验收清单：
- [x] `pipeline_skills/` 下 7 个新技能就位且有清单说明；
- [x] 数据契约文档定稿；
- [x] 样例小说端到端跑通 6 个文本管线技能（outline/script/characters/art/staging/storyboard），产出 JSON + 评审报告；`character-refs` 依赖出图后端（ComfyUI/OpenAI），后端就绪则跑通，未就绪则只验收其提示词产物并在文档记录缺口；
- [x] migration 脚本草稿完成（阶段 1 执行）。

### 阶段 1：链路打通（约 1 周）

任务分解：
1. 后端导入 API：`POST /projects/{id}/artifacts`（schema 校验、版本检查、幂等导入）；
2. 剧本页：渲染大纲五件套 + 分集剧本，编辑落库；
3. 制片台：渲染角色卡 / 美术设定 / 站位 / 分镜表（只读先行）；
4. 剧本页 AI 助手与画布解绑：降级为只读问答，禁止调用画布工具；
5. 导出：产物可导出 JSON，回 Claude Code 修订后重导入。

验收清单（2026-10-24 Task 8 勾选，证据见括注）：
- [x] 样例产物导入成功；非法 JSON 被拒且报错明确到字段（`tests/pipeline/test_phase1_acceptance.py` + `test_pipeline_api.py`：7 类 fixture 导入 200，staging 顶层数组 422 且 findings 非空，未知类型 404）；
- [x] 剧本页完整显示大纲+剧本，可编辑并保存（API 数据路径实测：GET script 附逐集正文视图、PUT episodes/2 落 edited_text；`web-vue` 构建通过；浏览器视觉验收待用户）；
- [x] 制片台显示四类产物（characters/art/staging/storyboard 四类 GET 含 content 实测 + 前端构建通过；浏览器视觉验收待用户）；
- [x] 剧本页助手对话全程零画布工具调用（`tests/pipeline/test_script_qa_mode.py`：script_qa 模式工具集为空 + 只读系统提示，回归红线锁定）；
- [x] "生产→导入→修订→重导"全程人工干预 < 1 小时（构造性成立：fixtures 就绪后导入+验证即 7 次 POST + 自动化验收测试，秒级完成；导出往返保形已测）。

### 阶段 2：大脑入产品（约 2–3 周）（2026-10-25 已由第 8 节修订，以下为原稿存档）

任务分解：
1. 技能执行器：容器内执行质量门脚本（compose 已有容器底座），产物写库；
2. 异步任务队列 + 进度流（复用 `core/events.py`、`core/outbox.py`、`ProductionProgress.vue`）；
3. 单编排 Agent：绑定 7 技能，按管线分阶段触发（大纲→用户确认→剧本→制片各环节）；
4. 剧本页助手接新脑：领域 Agent 绑 `novel-outline`/`novel-script`，改稿走体检模式（改完自动跑质量门）；
5. 确认门与失败重试：大纲确认后才写剧本；任一环节失败可重试且不产生脏数据；
6. LLM 接入与额度配置（`core/llm.py`）。

验收清单：
- [ ] 网页粘贴小说自动产出大纲落库，全程无 Claude Code 参与；
- [ ] 大纲确认后自动出分集剧本，质量门报告在前端可见；
- [ ] 制片四环节依次自动执行，产出分镜投产包；
- [ ] 任一环节失败可重试，无脏数据残留；
- [ ] 长任务断线重连后进度可恢复显示。

### 阶段 3：出片闭环（周期取决于媒体服务接入）

任务分解：
1. `character-refs` 接 ComfyUI / OpenAI Images 出角色图，进现有 media 管线（`core/media.py`、`media_job_store`）；
2. H3 视频提示词 → MiniMax 视频生成任务；
3. 音色提示词 → TTS（`core/tts.py`）；
4. 合成：分镜视频 + 配音（可选 BGM）→ 带配音成片；
5. 重点解决 P0 断点：静音出片。

验收清单：
- [ ] 单集自动产出全部分镜图与角色图；
- [ ] 视频段生成且与配音对齐；
- [ ] 合成导出带配音成片；
- [ ] 单集端到端耗时与成本记录在案。

### 阶段 4：画布回归（远期，只立项不展开）

画布以"分镜/资产可视化编辑器"身份重新接入，消费 pipeline 产物；旧画布技能届时基于产物重写或废弃。本阶段不在本规划内展开。

## 6. 关键取舍记录

| 决策点 | 结论 | 理由 |
|---|---|---|
| 产物存储 | Postgres 表 | 多用户产品必须；compose.postgres 已有 |
| 阶段 2 Agent 拓扑 | 单编排 Agent + 阶段技能 | 不复制 dramaTeam 多成员模式；质量门脚本已确定性承担 reviewer 职责 |
| 剧本页助手（阶段 1） | 降级只读问答 | 画布脑留着喂画布，不许碰剧本页数据 |
| 旧技能处置 | `workflow`/`code_review` 保留；四个内容类标 legacy | 画布仍可用，但不再与产品管线绑定 |
| 质量门脚本执行 | 容器内 | 技能含 Bash 步骤，不裸跑服务进程 |
| 实施检出 | Desktop 主检出 | studio/剧本页仅在该检出 |

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| shuohao 上游升级打破 schema | `schema_version` 字段 + 导入迁移 |
| 全本改编长耗时 | 阶段 2 直接上异步队列，不做同步请求 |
| 过渡期画布体验受损 | 阶段 1–3 画布原样可用，新旧入口并存 |
| 媒体服务（图/视频/TTS）接入不确定 | 阶段 3 独立立项，阶段 2 产物（提示词包）本身已可交付 |

## 8. 增补（2026-10-25）：Agent 化修订与三套脑合并

> 本章由阶段 1 交付后的全系统查重与求职定位确认（Agent 开发方向）触发，修订第 5 节阶段 2 的架构与任务、第 6 节两条取舍，并给阶段 3 补接缝。第 1–7 节其余内容不变。

### 8.1 定位修订：Workflow 当脊柱，Agent 当关节

- 求职定位确认为 **Agent 开发**。纯"状态机+确定性链"是 workflow 工程，不构成 Agent 作品集；纯 agent loop 跑生产管线则贵、慢、不可复现。
- 裁决：**编排层保持确定性状态机；创作类阶段执行器升级为 gated agent loop**——编剧 Agent 带工具集（`read_novel` / `read_upstream_artifacts` / `write_draft_section` / `run_quality_gate` / `read_gate_report` / `finish`）在质量门约束下自主迭代，终止条件 = 门全过，或迭代上限（默认 5 轮）后带失败报告交编排层处置（重试/修复 agent/人工）。
- 填表型阶段（characters/art 的表格部分）用"链 + 一次修复"控制成本；创作型阶段（outline/script/staging/storyboard）上完整 loop。
- 评测叙事：门即环境反馈的 tool-grounded self-correction；每次 run 的迭代数/门通过率/单阶段成本经既有 `invocation_store`（调用账本）聚合，前端运行详情可见。

### 8.2 三套脑合并（先于执行器开工）

系统查重结论：studio 侧 2026-10-21 script-asset-pipeline 与 shuohao 管线在"大纲/角色/美术"三层语义重叠（`core/story_analysis.py` 剧本理解四卡、`core/asset_tables.py` 资产表→资产库、`core/production.py` 十阶段机）。分层裁决：

| 层 | 胜者 | 处置 |
|---|---|---|
| 内容生产 | shuohao | 四卡执行路径在阶段 2 执行器落地后退役 |
| 资产沉淀 | studio 资产库语义 | `merge_asset_tables`（"表是草稿、库是资产"，只增不改不删）复用为管线产物 → 资产库的同步桥 |
| 执行工程 | studio 链式哲学 | SSE 流式/阶段间打断/提示词可调（prompts API）移植进技能执行器 |
| 编排 | 两者皆让位 | 单编排状态机接管；production.py 的 WAITING_APPROVAL 审批门思想保留；dramaTeam 相关属画布冻结区不动 |

UI 切换点：剧本页 `StoryAnalysisCards.vue` / `AssetTablesPanel.vue` 改接管线产物后四卡退役（不受画布冻结红线保护，可动；执行器落地前不拆）。相邻合流：AssetsView↔CastPanel、KeyframeWall↔StoryboardPanel（阶段 3）。

### 8.3 阶段 2 修订架构

```
粘贴小说 → POST /api/pipeline/runs
core/pipeline_run.py（新，0006 migration：pipeline_runs）
  状态机：novel → outline → ⛔确认门 → script → characters → art → staging → storyboard
  阶段失败可重试/交修复 agent；findings 落库不产脏数据；进度复用 events/outbox → SSE
core/skill_executor.py（新）
  ① 提示词：SKILL.md 口径编译为代码内提示词（注释与 SKILL.md 同步约束，学 story_analysis 的 _CARD_INSTRUCTION）
  ② 执行：gated agent loop（创作型阶段）/ 链+一次修复（填表型阶段）
  ③ 质量门：node 子进程（超时 / 可执行文件允许列表 / 临时目录）
  ④ 落库：pipeline_repository.save(source_run_id)；工具调用账本入 invocation_store
前端
  创作入口：粘贴小说 → 直播进度（流式 + 门报告 + 迭代轮数）→ 大纲确认
  确认门对话框：改稿脑锚定当前产物修订后再确认
  剧本页 AiPanel 升级改稿脑：提修改 → 用户审批 → 自动重跑质量门（"体检模式"）
  制片台：运行中/重试/修复 agent 决策展示；运行详情抽屉（评测聚合）
LLM：复用 core/llm（TF_ROUTER 已配）；前置清障（阶段 1 交接 ①②③⑥）为第一批任务
```

改稿脑三拍子是全产品统一的写入模式：**提修改 → 审批 → 自动体检**；agent 的一切写入必须过审批门。

### 8.4 阶段 2 任务分解（修订，8 任务）

1. 前置清障：连接池竞态→lifespan、`_resolve_schema_version`→core、api.js toError 扩展、产物 delete 路由、0006 pipeline_runs migration；
2. 执行器骨架 + novel-outline 单阶段端到端（gated loop、账本、落库）；
3. run 状态机 + 确认门 + SSE 进度（后端）；
4. 创作入口前端：粘贴小说 → 直播进度 → 大纲确认（含对话框）；
5. novel-script 执行器（节拍流/时长门是硬仗）+ outline→script 自动推进；
6. 制片四层执行器（characters/art/staging/storyboard）+ 制片台运行/重试/修复 UI；
7. 三套脑切换：剧本页面板换源、资产库 merge 桥、四卡下线；
8. 收尾：改稿脑升级（工具+审批+体检）、produce 按钮接管线、运行详情（评测聚合）、端到端验收测试。

验收清单（原 5 条 + 新 3 条）：
- [ ] 网页粘贴小说自动产出大纲落库，全程无 Claude Code 参与
- [ ] 大纲确认后自动出分集剧本，质量门报告前端可见
- [ ] 制片四环节依次自动执行，产出分镜投产包
- [ ] 任一环节失败可重试或交修复 agent，无脏数据残留
- [ ] 长任务断线重连后进度可恢复显示
- [ ] 三套脑合并完成：剧本页单一内容来源，资产库由管线产物同步
- [ ] 创作阶段为带工具 agent 自主迭代；单次 run 的迭代数/门通过率/成本在运行详情可见
- [ ] 改稿脑"提修改→审批→自动体检"可用，agent 写入必须过审批门

### 8.5 阶段 3 接缝增补

- character-refs 出图接资产库既有生成链（`/assets/{node_id}/generate`、looks），不另建图库；
- storyboard 产物与 KeyframeWall 合流（生成前提示词 ↔ 生成后关键帧，同一条链的上下游）；
- 视频提示词 → media job；静音出片 P0 不变。

### 8.6 取舍记录（修订/新增）

| 决策点 | 结论 | 理由 |
|---|---|---|
| 质量门脚本执行（修订第 6 节"容器内"） | node 子进程直跑 | node 已是产品依赖；技能第一方 vendored（SHA 锁定）可审计；Windows 开发机容器徒增复杂度 |
| 编排不用 agent loop | 确定性状态机 | 顺序固定、门是代码、成本可审计；agent loop 留给改稿对话与异常修复 |
| 创作阶段用 gated loop | 带工具迭代至过门 | 门即环境反馈的自我修正，是可验证的 agent 能力展示 |
| 填表型阶段不上 loop | 链 + 一次修复 | 无判断空间，loop 纯烧钱 |
| 四卡退役节奏 | 阶段 2 内切换收尾 | 避免三套脑长期并存堆积重复 |
