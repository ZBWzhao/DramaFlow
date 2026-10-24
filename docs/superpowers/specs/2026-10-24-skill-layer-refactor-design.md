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
1. 将 shuohao 7 技能并入仓库 `skills/`（保留各技能目录结构：SKILL.md / references / scripts）；
2. 逐个审阅 7 类产物 JSON schema，输出字段清单文档；
3. 设计 Postgres 表并撰写 migration 草稿；
4. 撰写数据契约文档（schema→表映射、导入/导出与幂等规则）；
5. 选定样例小说，在 Claude Code 中端到端跑通技能链，产物留作阶段 1 测试夹具。

验收清单：
- [ ] `skills/` 下 7 个新技能就位且有清单说明；
- [ ] 数据契约文档定稿；
- [ ] 样例小说端到端跑通 6 个文本管线技能（outline/script/characters/art/staging/storyboard），产出 JSON + 评审报告；`character-refs` 依赖出图后端（ComfyUI/OpenAI），后端就绪则跑通，未就绪则只验收其提示词产物并在文档记录缺口；
- [ ] migration 脚本草稿完成（阶段 1 执行）。

### 阶段 1：链路打通（约 1 周）

任务分解：
1. 后端导入 API：`POST /projects/{id}/artifacts`（schema 校验、版本检查、幂等导入）；
2. 剧本页：渲染大纲五件套 + 分集剧本，编辑落库；
3. 制片台：渲染角色卡 / 美术设定 / 站位 / 分镜表（只读先行）；
4. 剧本页 AI 助手与画布解绑：降级为只读问答，禁止调用画布工具；
5. 导出：产物可导出 JSON，回 Claude Code 修订后重导入。

验收清单：
- [ ] 样例产物导入成功；非法 JSON 被拒且报错明确到字段；
- [ ] 剧本页完整显示大纲+剧本，可编辑并保存；
- [ ] 制片台显示四类产物；
- [ ] 剧本页助手对话全程零画布工具调用；
- [ ] "生产→导入→修订→重导"全程人工干预 < 1 小时。

### 阶段 2：大脑入产品（约 2–3 周）

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
