# 质量门分级设计：blocking 拦下 / advisory 标记落库（门分级 v1）

> 状态：待用户审（2026-10-26）
> 动机实证：2026-10-26 story-01（minutesPerEpisode 单位陷阱 → 5400s 目标必不过）、story-22 run b17f3551（模型只写 2/4 集且编号乱，4 条违规同一根因）。
> 用户需求原话：「能不能不要一个地方有问题就全部失败，只有一个地方有问题就标记那个地方，其他的地方继续生成，生成的产物显示问题标记由用户人工来改。」

## 1. 核心判据（一句话）

**这条违规会不会让下游阶段机械地做不下去？会 → blocking（拦）；只是内容不好 → advisory（标记）。**

- advisory 流到成片：内容有瑕疵，人工能救。
- blocking 流到成片：下游机械解引用失败 / 媒体生成烧真金白银且救不回来。

## 2. 分级粒度：按规则（rule id），不按单条违规

每条门规则整体定性为 advisory 或 blocking。理由：门规则内部已是稳定 id（`add(id, label, ok, detail)`），per-rule 分级可实现、可测试、可锁；per-violation 分级要求门脚本理解语义，越界。

捆绑型规则（一条规则既查「引用了不存在的 X」又查「定义了没用到 X」）整体按 blocking 处理——完整性方向更要紧，零使用残留在大纲分集对齐后自然消失（story-22 的 4 条违规就是缺集的连锁，不是 4 个独立问题）。

## 3. 机制

### 3.1 门脚本：additive JSON 哨兵输出（解冻切口，需批）

`pipeline_skills/*/scripts/*.mjs` 的 validate 命令在现有**人读输出末尾追加一行**：

```
<<GATE-RULES>>[{"id":"duration","ok":false,"detail":"第 1 集欠 3.2 秒…"}, …]
```

- **规则逻辑零改动**——只是把已有的 `gates` 数组（id/ok/detail 都在内存里）多打一行 JSON。
- 执行器解析哨兵行；无哨兵（未知/旧脚本）= 退化为全 blocking（向后兼容，分层上线不绑定）。
- 解冻范围：仅各门脚本 CLI 输出层这一处 additive 改动。冻结红线的其余部分（SKILL.md 提示词、规则判定逻辑）仍只读。

### 3.2 执行器：白名单分级（默认全 blocking）

`StageSpec` 新增 `advisory_rules: frozenset[str]`。判定：

- 门跑完 → 哨兵解析 → `failed = [r for r in rules if not r.ok]`
- `blocking_failed = [r for r in failed if r.id not in advisory_rules]`
- blocking_failed 非空 → **照旧失败**（fail-safe 不动：不落库、带草稿退出、可编辑重试）。
- blocking_failed 为空但 advisory_failed 非空 → **stage 通过**：产物正常落库，advisory 违规作为标记随产物存（挂 findings，import_findings 已有 ok=True 带 findings 的先例），不触发修复轮。

**advisory 不进修复轮**：修复轮预算只花在 blocking 上——轮次继续/耗尽的判定只看 blocking；修复轮的消息照旧带全部门输出，模型可顺带修 advisory（零额外成本），但系统不强制。用户明确「由用户人工来改」，advisory 专程重修既费轮次又抢 blocking 预算；产物页随时可对单阶段重跑重新抽卡。

### 3.3 呈现

- 事件流：stage 通过事件带「N 条内容标记已随产物落库」，findings 明细照常入事件。
- 产物页（管线产物 tab）：每类产物下方显示 ⚠️ 标记列表（读 artifacts 的 findings），标注「内容标记 · 不阻塞，可人工修改或重跑本阶段」。
- run 状态语义不变（gated/failed/done）；「带标记通过」不是新状态，只是通过的产物携带 findings。

## 4. v1 规则分类表

| 阶段 | 规则 id | 级别 | 理由 |
|---|---|---|---|
| outline | risk-flag（生成难点预警清单） | **advisory** | 内容标注；story-01/b17f3551 两次实证它纠缠修复轮 |
| outline | 结构类（episodes 数 vs params.episodes、编号连续、引用完整性等） | blocking | 剧本阶段机械消费分集 |
| outline | 其余（scene-cap、beat-gap、ep1-hook…） | blocking | v1 保守，实证后再降 |
| script | duration | blocking | 媒体管线依赖时长；目标修正后可收敛（12 轮 6 门实证） |
| script | line-length、speaker、beats-claimed | **advisory** | 内容瑕疵；对话/提示词层人工可改 |
| script | hook-cliff、hook-open、has-action、action-prose | blocking | 结构性叙事契约，缺了下游接不上 |
| script | refs-characters、refs-scenes | blocking | 下游解引用 |
| characters | 引文逐字、voice.prompt 长度 | **advisory** | 复冒烟 III 的原始痛点；标记后人工换引文 |
| characters | 其余（summary 缺失等结构类） | blocking | 卡不完整产物 |
| art / staging / storyboard | 全部 | blocking（v1 不分类） | 无实证；白名单制下天然保守 |

实施第一步：全量盘点各门 rule id 写进 lock 测试（断言已知 id 集合，防 .mjs 演化漂移）；characters 门规则 id 以盘点为准补进表。

## 5. 红线改动清单（请批）

1. **失败安全**：「门不过不落库」→「blocking 不过不落库；advisory 挂 = 落库带标记」。这是本次唯一的原则性改动。
2. **pipeline_skills 只读**：允许「CLI 输出层 additive 哨兵行」这一处例外，规则判定逻辑与 SKILL.md 仍只读。
3. 质量门阈值：不改。
4. 确认门：不动。
5. agent 政策循环语义：不动（修复轮只对 blocking，advisory 不消耗轮次——这是收紧不是放松）。

## 6. 测试策略

- 哨兵解析：有/无哨兵两态；坏 JSON 退化全 blocking。
- 分级判定：advisory-only 挂 → ok=True + findings 落库 + 零修复轮（ScriptedLLM 断言不再发修复消息）；blocking 挂 → 照旧失败不落库。
- 混合挂（advisory+blocking 同时）→ 失败，findings 含两者。
- rule-id lock 测试：每门已知 id 集合断言。
- 事件流/产物 findings 呈现：前端 build + 浏览器复验（一次性项目 → 造 advisory 违规 → 标记可见）。

## 7. 非目标

- 不做 per-violation 分级、不做 advisory 自动重修、不改草稿编辑器、不做产物内联标注（v1 标记挂在产物级 findings，不定位到集/场内联）。
- duration 容差放宽（±20%）不在本提案（队列 8 已决）。
