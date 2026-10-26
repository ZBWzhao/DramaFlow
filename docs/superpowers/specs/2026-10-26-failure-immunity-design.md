# 失败免疫设计：内容失败趋近零，只留环境性失败（A1+A2+A3+B）

> 状态：待用户审（2026-10-26）
> 动机：用户原话「现在管线还会生成内容失败吗，能不能不让他生成失败」，批准「A 全做 + B 批准」。
> 实证：story-01（5400s 时长目标必不过烧穿轮次）、story-22（2/4 集结构违规判死）、门分级上线后 advisory 不再拦但 blocking 仍判死。

## 0. 失败的分类（本设计的公理）

| 类别 | 例子 | 处置 |
|---|---|---|
| 内容不好 | 叙事质量类违规（钩子/节奏/描写）、数量超标类 | **不该失败**：A1 降 advisory，剩余 blocking 走 B 兜底 |
| 内容结构坏 | 缺集、编号乱、引用不存在的角色/场景、duration 失衡 | 修复轮尽力修；修不好 → **B 兜底落库带 ⛔ 标记**，人工兜底（确认门/出片确认） |
| 环境坏了 | API key 失效、余额耗尽、网络不可达、参数 4xx | **必须失败**：继续跑只会白烧钱；照旧不落库、可重试 |

## 1. A1：规则分级 v2（白名单扩充，阈值零改动）

判据不变：「下游会不会机械做不下去」。v1 保守挂 blocking 的叙事质量类按此降 advisory。

### outline（14 条 → advisory 10 / blocking 4）

| 规则 id | v1 | v2 | 理由 |
|---|---|---|---|
| risk-flag | advisory | advisory | 不变 |
| lead-cap / support-cap / functional-cap / prop-cap | blocking | **advisory** | 数量超标是内容密度问题，LLM 下游照常消费 |
| scene-cap | blocking | **advisory** | 单集场景过多→剧本偏长，质量/体量问题非机械阻断 |
| beat-gap | blocking | **advisory** | 节奏间隔，纯内容质量 |
| ep1-hook | blocking | **advisory** | 第一集钩子，叙事质量 |
| major-early | blocking | **advisory** | 主角出场时机，叙事质量 |
| crowd-plan | blocking | **advisory** | 群像规划质量 |
| no-dialogue | blocking | **advisory** | 大纲写台词是格式瑕疵，剧本阶段不受机械影响 |
| ep-fields | blocking | blocking | 分集字段缺失→剧本不知道写什么，机械阻断 |
| once-scene | blocking | blocking | 语义偏完整性约束，v2 不动（存疑不降） |
| refs | blocking | blocking | 引用完整性，下游解引用 |

### script（10 条 → advisory 7 / blocking 3）

| 规则 id | v1 | v2 | 理由 |
|---|---|---|---|
| line-length / speaker / beats-claimed | advisory | advisory | 不变 |
| hook-cliff / hook-open | blocking | **advisory** | 钩子缺失，叙事质量；下游 LLM 照常写角色/美术 |
| has-action / action-prose | blocking | **advisory** | 动作描写质量 |
| duration | blocking | blocking | 媒体管线时长口径；B 兜底保证不再判死 run |
| refs-characters / refs-scenes | blocking | blocking | 下游解引用 |

### characters / art / staging / storyboard：v2 不动

characters 已分级（cast-structure blocking + 两条 advisory）。art/staging/storyboard 无失败实证，维持全 blocking；它们的「不失败」由 B 兜底保证，而非盲目降级（v1 spec「实证后再降」路线不变）。

**落点**：`core/skill_executor.py` STAGE_SPECS 的 `advisory_rules`（outline/script 两条扩充为上述 frozenset）；`tests/pipeline/test_gate_sentinel.py` 的 id lock 集合补 advisory 子集断言；三方锁测试（STAGE_SPECS == skill_executor 常量 == 测试常量）随 id 集合更新。

## 2. A2：修复轮早停（不再烧满轮次）

- 现状：迭代上限默认 5，耗尽才判失败。
- 新增：**blocking 失败集合连续 3 轮无缩小 → 早停**。逐轮记录 blocking failed id 集（哨兵解析产物）；本轮集合 ⊇ 上一轮（无改善）计数 +1，集合缩小清零；计数达 2（即连续 3 轮同/更差）停止修复。
- 早停不是失败：直接进入 B 兜底路径（见 §4）。
- 环境类失败不进早停统计——LLM 调用异常照旧抛出走 §5。

## 3. A3：LLM 传输层退避重试（`core/llm.py`）

- 现状：单发 `timeout=180`，任何瞬断/5xx 直接判阶段失败。
- 新增：聊天补全请求包重试循环——**瞬态错误重试，共 3 次尝试，退避 2s/4s/8s ± 抖动**。
- 瞬态 = 连接错误、读超时、HTTP 429、5xx。
- **不重试** = 401/403（凭据）、400/404（参数/模型名）——这些是环境/配置错误，立即抛出（环境性失败，运行照旧失败）。
- 范围仅 `core/llm.py` 调用点；agent 政策循环、修复轮语义零改动。

## 4. B：兜底落库（红线改动，用户已批）

### 触发

blocking 修复轮耗尽（含 A2 早停触发）时，不再 `StageResult(False)`。

### 行为

1. 产物照常落库（既有 latest-wins upsert）；`findings` 携带 blocking 标记，形状与 advisory 同构、severity 不同：
   `{"gate": "validate", "rule": "<id>", "severity": "blocking", "detail": ...}`
2. 门不可用（哨兵缺失/门脚本崩溃/超时 → rules=None）：同样兜底，标记 `rule: "gate-unavailable"`、detail 带人读输出尾部。
3. `StageResult(ok=True, degraded=True, findings=...)`；`pipeline_runs.stage_status` 该阶段记 `degraded: true` + blocking 数（stage_status 是 JSONB，无迁移）。
4. 事件流：`stage_done` 增键 `degraded: true`、`blocking: N`（既有键零改动，纯 additive）。
5. run 终态仍是 `done`（0006 CHECK 不动）；「完成 · N 个阶段带 ⛔ 阻断标记」由前端从 stage_status/findings 派生。
6. **下游照常推进**；下游阶段的提示词已携带上游产物正文，不额外注入标记（v1 不做标记透传，见非目标）。

### 环境性失败保持现状

LLM 异常（含 A3 重试后仍失败）、执行器异常：照旧 run failed、不落库、失败卡重试。**兜底只兜「门说不好」，不兜「跑不动」。**

### 红线修订（§5.1 第二次修订，本会话批准）

「blocking 门不过不落库」→「blocking 修复轮耗尽 = 落库带 ⛔ 阻断标记，运行继续；环境性失败仍不落库」。失败安全的精神不变：坏东西不悄悄流动——⛔ 标记全程可见。

### 确认门联动（零改动，天然协同）

大纲即使 degraded 也停确认门：人看到的大纲草案带着 ⛔ 标记再决定放行。确认门语义、唯一入口不变。

## 5. 测试策略

- 分级：advisory-only / mixed 既有测试模式套 v2 白名单；id lock 集合更新（outline 10 advisory、script 7 advisory）。
- 早停：ScriptedLLM 构造连续同集合 blocking 失败 → 断言第 3 轮后停止、走兜底、LLM 调用次数上界。
- 兜底：轮耗尽 → StageResult(ok=True, degraded=True)、产物落库、findings 含 blocking 标记、stage_done 事件带 degraded/blocking；下游阶段照常推进。
- 门不可用：monkeypatch _run_gate 返回 (False, 输出, None) 耗尽 → 兜底 + gate-unavailable 标记。
- 环境失败：LLM 抛异常 → 照旧 failed 不落库（既有测试全绿即证）。
- A3：假传输层连挂 2 次 → 第 3 次成功；401 不重试。
- 基线：303 passed 起，全套 ≥ 315。

## 6. 非目标

- per-violation 分级、advisory/blocking 自动重修、阈值改动：不做。
- 标记向下游提示词透传：v1 不做（先看人工兜底够不够）。
- `pipeline_editor.run_gate_only` 保持全 blocking 旧语义（既有 Deferred 不变，需独立 spec）。
- 出片前确认/媒体阶段：阶段 3 范围。
