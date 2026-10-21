# 剧本→资产流水线与 Skill 体系重构 设计文档

日期：2026-10-21
分支：feat/frontend-redesign（设计先行，实施计划另行拆分）
状态：待用户评审

## 1. 背景与目标

当前系统的 AI 产出是一条黑盒式流水线：`produceEpisode` 一次 agent run 把「拆分镜 + 提资产 + 写提示词」全部做完，用户只能在结果上打补丁。用户（产品负责人）定义的目标链路是**三段式人机分工**：

```
新建项目（选 Skill）→ 上传剧本 → 剧本理解（世界观/梗概/分集大纲/视听风格）
    → 资产表（角色/场景/道具草稿表，用户增删改）→ 同步资产库
    → 单集制作（分集提示词，Phase 2）
```

核心原则（与既有「人改优先 / AI 先 diff」一致）：

- **Agent 只产出需求与草稿，不执行生成**。生图/出片的执行权始终在用户。
- **表是草稿区，库是资产**。资产表可反复重解析，资产库只由用户手动同步。
- **结构化产物走按钮触发的 pipeline**（逐卡流式、可打断）；自由修改走聊天。

本期（Phase 1）交付「剧本→资产」全链路；单集制作（分集提示词）为 Phase 2。

简历价值点（实现时保留证据）：最小权限的 Skill 工具白名单、结构化输出的确定性 LLM 链、草稿/资产两层数据模型与幂等同步、可打断 pipeline。

## 2. Skill 体系重构

### 2.1 Skill = 提示词包 + 工具白名单 + 产出契约

现有 `core/skills.py` 的 SKILL.md frontmatter（name/description）扩展为：

```markdown
---
name: script-analysis
version: 1.0.0
role: script-analysis          # script-analysis | director | asset
description: 通读全剧本，产出世界观/故事梗概/分集大纲/视听风格四件套
allowed_tools: [read_file, list_dir, get_canvas, update_story_analysis]
---
（正文：流程与工艺指令）
```

- `version`：语义化版本，纯文本标记；换版本不迁移旧产物，产物带「由 vX.Y.Z 生成」戳。
- `allowed_tools`：该 Skill 驱动 agent 时可用的工具白名单（**结构性边界**：资产 Skill 永远拿不到生图/视频工具）。缺省 = 该角色默认集。
- 产出契约：写在 SKILL.md 正文约定 + 服务端 pydantic schema 校验（不进 frontmatter）。

### 2.2 三个角色 Skill（重构掉 workflow / drama_craft）

| Skill | 职责 | 驱动方式 |
|---|---|---|
| `script-analysis` 剧本理解 | 四件套产物（§3） | 按钮 pipeline（确定性 LLM 链）+ 聊天修改 |
| `director` 导演 | 单集细化→片段拆分→视频提示词（Phase 2） | 按钮 pipeline（Phase 2 接管 produceEpisode） |
| `asset` 资产 | 剧本→资产三表抽取；形象提示词打磨 | 按钮 pipeline（抽取）+ 资产库内轻量对话（Phase 2 接生图） |

迁移映射：`workflow`（SKILL.md 流程路由表）拆进三个角色 Skill 的「流程」段；`drama_craft` 的工艺内容（首帧八段式、引用与文字锁、分镜信息块）并入 `director` 与 `asset` 的正文；`code_review` 技能保留不动。旧目录删除，`read_skill` 机制不变。

### 2.3 项目绑定与边界执行

- `canvas.meta.project.skills = {script_analysis: "script-analysis@1.0.0", director: "director@1.2.0", asset: "asset@1.1.0"}`，新建项目时选择（NewProjectDialog 三个下拉，含版本），项目信息/设置里可改。
- 新建弹窗保留「创建后自动生成资产表」勾选 → `meta.project.auto_asset_table: true`；首次 `import-script` 成功后自动触发 §3 pipeline + §4 解析（仍停在表，不生图）。
- `server/registry.py` 的 `tools_for()` 扩展：`tools_for(skill=...)` 按所选 Skill 的 `allowed_tools` ∩ 全量工具装配会话；白名单为空视为「该 Skill 不驱动 agent，只用于 pipeline prompt」。

## 3. 剧本理解 pipeline（script-analysis）

### 3.1 执行模型：确定性 LLM 链，不是自由 agent loop

四张卡依次生成，每卡 = 一次结构化 LLM 调用：

```
卡1 世界观 worldview     ← 输入：全剧本
卡2 故事梗概 synopsis    ← 输入：全剧本 + worldview
卡3 分集大纲 episode_outline ← 输入：全剧本 + worldview + synopsis
卡4 视听风格 audiovisual_style ← 输入：全剧本 + 前三卡
```

选确定性链而非 agent loop 的理由：产物全部可 schema 校验、无需画布工具、成本与时长可预算、可打断点天然落在卡间。

### 3.2 产物 schema（存 `canvas.meta.story_analysis`）

```jsonc
{
  "skill": {"name": "script-analysis", "version": "1.0.0"},
  "updated_at": 1730000000,
  "worldview": "string（≤800字）",
  "synopsis": "string（≤600字）",
  "episode_outline": [{"no": 1, "title": "…", "hook": "本集钩子", "beats": ["…"]}],
  "audiovisual_style": {"visual": "画面风格描述", "camera": "镜头语言", "pacing": "节奏"}
}
```

服务端 pydantic 校验；单卡 JSON 解析失败自动重试 1 次（错误信息回喂），再失败则该卡标记失败、pipeline 继续下一卡。

### 3.3 流式与打断

- `POST /api/studio/story-analysis/run {canvas}` → NDJSON 流：`card_start {key}` / `card_delta {key, text}` / `card_done {key, data}` / `analysis_done` / `card_failed {key, error}`。
- 打断：`POST /api/studio/story-analysis/cancel {canvas}`；卡间检查取消标志，已完成卡保留落盘。
- 单飞约束：同一画布同时只允许一个分析/解析任务（409，沿用 produce 的防并发写模式）。
- 重跑粒度：`{cards: ["worldview", …]}` 可选参数，缺省全量。
- 剧本超长（>5 万字符）时分集大纲采用两段式（先按集摘要、再汇总），Phase 1 先实现阈值内的单次调用，阈值策略留参数。

### 3.4 自由修改走聊天

聊天 Agent 新增工具 `update_story_analysis {field, value}`（仅 script-analysis 白名单内的会话可拿到），写入前过同一 pydantic schema。聊天里「把世界观改成赛博背景」→ agent 调工具改字段 → 前端卡片刷新。

## 4. 资产表（草稿区）

### 4.1 数据模型（存 `canvas.meta.asset_tables`）

```jsonc
{
  "skill": {"name": "asset", "version": "1.1.0"},
  "updated_at": 1730000000,
  "characters": [{
    "name": "林生", "description": "林俊的丈夫、千千的父", "bio": "身高体材：约175cm…",
    "looks": [{"name": "宴会便装"}, {"name": "家居围裙"}]   // 形象只存名字，提示词在库
  }],
  "scenes":    [{"name": "陈家客厅", "description": "…"}],
  "props":     [{"name": "茶杯", "description": "…"}],
  "synced_at": 0        // 最近一次同步时间；0=未同步
}
```

- 存储归一：角色一条记录，形象是其子数组；UI 表格按「角色×形象」平铺，重复列显示「同上」（折叠展示，非存储形态）。
- 同一形象名在解析中重复出现 → 去重为一条。

### 4.2 解析（parse）

`POST /api/studio/asset-tables/parse {canvas}` → NDJSON 流（`parse_progress {episode_no}` / `parse_done {tables}`）。

- LLM 结构化抽取，按集分块（每集一次调用，跨集合并同名项），输入 = 该集剧本正文 + 已有表（作为「已见角色提示」，减少同名变体）。
- **只覆写 `meta.asset_tables`，绝不触碰资产库**（原则：表是草稿区）。

### 4.3 编辑

`PUT /api/studio/asset-tables {canvas, tables}` — UI 网格编辑整表提交，服务端 schema 校验后覆写。乐观锁（revision）沿用画布机制。

### 4.4 同步资产库（sync）——幂等合并 + 别名归一

`POST /api/studio/asset-tables/sync {canvas}` → JSON 结果：

```jsonc
{ "created_assets": 3, "created_looks": 5, "merged": [{"from": "苏 晚", "into": "苏晚"}],
  "skipped": [], "synced_at": 1730000000 }
```

合并规则（纯函数 `merge_asset_tables(canvas, tables) -> plan`，可单测）：

1. **键 = (kind, 规范化名称)**：规范化 = trim + 全角转半角 + 去内部空格。同名即同一资产（别名归一入口）。
2. 库中已有该资产：只**新增缺失的形象**、补全资产节点的空字段（description/bio）；非空字段与已有形象一律不动，**图片永远不碰**。
3. 库中不存在：创建 asset 节点 + looks（沿用现有 asset 节点结构与 `based_on` 链）。
4. 表中删掉的行**不删除**库中资产（库只增由同步负责，删减由用户在资产库操作）。
5. 结果报告 created/merged/skipped，前端 toast + 表格标记同步状态。

### 4.5 UI（剧本页）

- 新增「资产表」区：三 tab（角色/场景/道具）+ 可编辑网格（角色表 4 列：角色名/角色描述/人物小传/形象名，形象多行平铺+「同上」折叠）；顶部显示「由 asset@1.1.0 解析 · 上次同步 xx」。
- 操作按钮：`重新解析`（流式进度）、`同步资产库`（结果报告）。
- 卡片流（§3 四件套）展示在右栏 AI 助手中，卡下挂 `生成资产表` 按钮（= 触发 parse）。

## 5. 生图占位（不接 API）

- 资产库形象详情沿用现有提示词编辑（结构化分段提示词保持现状）。
- `生成形象图` 按钮 → **导出提示词文件**（`GET /api/studio/looks/export-prompt?canvas=&asset=&look=` → `text/plain` 下载，文件名 `资产名-形象名-提示词.txt`），界面提示「生图 API 未接入，已导出提示词」。
- 预留扩展点：`lookGenJob {status, prompt, model, params}` 结构定义好，接真实 API 时只换实现；图生图（参考图迭代）属于该扩展，本期不做。

## 6. 端点清单

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/skills` | GET | Skill 清单（role/name/version/description），供新建弹窗与项目设置 |
| `/api/studio/project/skills` | POST | 保存项目 Skill 选择 → `meta.project.skills` |
| `/api/studio/story-analysis/run` | POST(NDJSON) | 四卡 pipeline 流式执行 |
| `/api/studio/story-analysis/cancel` | POST | 打断 |
| `/api/studio/asset-tables/parse` | POST(NDJSON) | 解析剧本→三表（只写草稿） |
| `/api/studio/asset-tables` | PUT | 用户编辑整表提交 |
| `/api/studio/asset-tables/sync` | POST | 幂等同步入资产库 |
| `/api/studio/looks/export-prompt` | GET | 导出形象提示词文件 |
| `get_project` | 扩展 | 返回 `story_analysis` + `asset_tables` |

聊天会话的画布绑定与 media_policy 机制不变；`update_story_analysis` 作为新工具插件挂入 `tools/`。

## 7. 文件布局

- `core/skills.py`：frontmatter 扩展（version/role/allowed_tools）、SkillEntry 扩展、装配校验（白名单里的工具必须存在——启动即测，杜绝「read_node 幽灵引用」类缺陷）。
- `skills/{script-analysis,director,asset}/SKILL.md`（workflow/drama_craft 内容迁入后删除旧目录；workflow/references 分册按归属迁到 director/asset 下）。
- `core/story_analysis.py`：四卡 schema + 确定性链 runner（可注入 fake LLM 测试）。
- `core/asset_tables.py`：三表 schema + parse prompt 组装 + `merge_asset_tables` 纯函数。
- `tools/story_analysis/tool.py`：`update_story_analysis` 工具插件。
- `server/studio.py`：新端点；`server/registry.py`：`tools_for(skill=)`。
- `web-vue`：`NewProjectDialog`（Skill 下拉×3 + 自动生成勾选）、`ScriptView`（四卡流 + 资产表区 + 按钮）、`AssetsView`（导出提示词）、`studioStore`（新 actions + 流式消费）。
- 测试：`tests/test_story_analysis.py`（schema/打断/重试，fake LLM）、`tests/test_asset_tables.py`（归一化合并纯函数）、`tests/test_skills_manifest.py`（装载/白名单装配）；前端沿用 vitest 管道。

## 8. 非目标（本期不做）

真实生图/图生图 API、音色体系、片段备选编号（1-2A）、擦除、制作门禁细节、不规范剧本格式化、画布产品形态重构、提示词模型方言渲染层（Phase 2 与分集提示词一起设计）、TeamRunner 多代理重构（Phase 2 重切 produce）。

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| 长剧本超上下文 | 阈值参数 + 两段式摘要路径预留（§3.3）；Phase 1 剧本规模内单次调用 |
| LLM 结构化输出不稳定 | pydantic 校验 + 解析失败回喂重试 1 次 + 失败卡单独标记不阻塞其他卡 |
| 同步误伤用户数据 | 合并只增不覆盖；`merge_asset_tables` 纯函数全分支单测；报告 visible |
| Skill 白名单配错 | 装载期校验（工具必须存在）+ 装配测试 |
