# 质量门分级实施计划：blocking 拦下 / advisory 标记落库

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 门规则分级——blocking 违规照旧拦下（失败安全不变），advisory 违规不进修复轮、产物照常落库并带 ⚠️ 标记，产物页显示标记由人工处理。

**Architecture:** 门脚本 validate 在人读输出末尾追加一行 `<<GATE-RULES>>[...]` JSON 哨兵（additive，规则逻辑零改动）；执行器解析哨兵、按 `StageSpec.advisory_rules` 白名单分级：blocking 挂 → 旧失败路径；仅 advisory 挂 → 走通过路径落库，标记写进 `pipeline_artifacts.findings` 新列（0008）；前端产物面板读 findings 渲染标记。

**Tech Stack:** 同主线（FastAPI/asyncpg/Vue3/node 门脚本）。

**Spec:** `docs/superpowers/specs/2026-10-26-gate-severity-tiers-design.md`（用户已批，含红线改动清单 §5）

## Global Constraints

- 冻结切口（spec §5.2 已批）：`pipeline_skills/*/scripts/*.mjs` 仅允许 validate 命令输出层追加哨兵行；规则判定逻辑、SKILL.md、其余技能文件零改动。每个门脚本的 diff 审查时逐行核对这一点
- 失败安全（改动后）：blocking 门不过 → 不落库、带草稿退出；仅 advisory 挂 → 落库带标记。确认门、agent 政策循环、阈值全部不动
- advisory 白名单（v1，spec §4）：outline `{risk-flag}`；script `{line-length, speaker, beats-claimed}`；characters `{引文逐字 id, voice 长度 id}`（Task 1 盘点锁定后填入）；art/staging/storyboard = 空
- 测试惯例：asyncio.run 风格、ScriptedLLM、真 Postgres、真 node 门；基线 **287 passed**
- 迁移惯例：0008 跟随连接池启动自动应用；手动同步 pipeline_test 库（0006/0007 先例）
- Commits：lowercase conventional prefix，每任务一个提交；涉前端任务 `cd web-vue && npm run build` 零错误

---

### Task 1: 门脚本哨兵输出 + rule id 盘点锁定

**Files:**
- Modify: `pipeline_skills/novel-outline/scripts/novel-outline.mjs`、`novel-script/scripts/novel-script.mjs`、`novel-characters/scripts/novel-characters.mjs`、`novel-art/scripts/*.mjs`、`novel-staging/scripts/*.mjs`、`novel-storyboard/scripts/novel-storyboard.mjs`（各文件 validate 命令的输出段——novel-script 的参照位置：`for (const g of gates) console.log(...)` 块，约 line 1031）
- Test: `tests/pipeline/test_gate_sentinel.py`（新建）

**Interfaces:**
- Produces: 每个门 validate 在 `✗ N 项未过`/`✓ 全部通过` 之后、`process.exit` 之前打印一行
  `<<GATE-RULES>>` + `JSON.stringify(gates.map(g => ({id: g.id, ok: g.ok, detail: g.detail})))`（无空格讲究，`JSON.stringify` 默认即可）。人读输出其余部分逐字不变
- Produces（给 Task 2）: `tests/pipeline/test_gate_sentinel.py` 顶部导出常量 `CHARACTERS_ADVISORY_IDS`（引文逐字 + voice 长度两条规则的 id，从实际输出盘点）；全量 id 清单注释

- [ ] **Step 1: 失败测试**——六个门各跑一次 validate，断言：输出存在 `<<GATE-RULES>>` 行且可解析为 `[{id, ok, detail}]` 数组。fixture 从 `{}` 起步；若某门对 `{}` 崩溃（非 gate-fail 的异常退出码），改用该技能 `references/schema.md` 示例的骨架缺关键字段。outline/script/characters 三个门额外断言已知 id 集合（novel-script 全 10 条：duration、line-length、speaker、hook-cliff、hook-open、has-action、action-prose、beats-claimed、refs-characters、refs-scenes；novel-outline 含 risk-flag；characters 的实际 id 以跑出的为准写进常量）
- [ ] **Step 2: 跑失败**（六门输出无哨兵）
- [ ] **Step 3: 实现**——各门 validate 输出段统一三行模式：

```js
console.log('<<GATE-RULES>>' + JSON.stringify(gates.map(g => ({ id: g.id, ok: g.ok, detail: g.detail }))));
```

  注意 `process.exit(1)` 的门要在 exit 前打印；novel-characters.mjs 是 UTF-8 带 BOM 的"binary file"（grep 语义），编辑时确认编码不破
- [ ] **Step 4: 全套测试 + 提交**

```bash
git add pipeline_skills/*/scripts/*.mjs tests/pipeline/test_gate_sentinel.py
git commit -m "feat: gate validate emits <<GATE-RULES>> sentinel with per-rule id/ok/detail"
```

---

### Task 2: 执行器分级（解析哨兵 + advisory 白名单 + run_stage 判定）

**Files:**
- Modify: `core/skill_executor.py`（`_run_gate` ~line 426、`_ToolBox.run_gate` ~line 352 与 ~line 328、`run_gate_only` ~line 451、STAGE_SPECS 的 outline/script/characters 三条 ~line 60-130、run_stage 权威门块 ~line 657-690）
- Test: `tests/pipeline/test_skill_executor.py`

**Interfaces:**
- Consumes: Task 1 哨兵行格式与 `CHARACTERS_ADVISORY_IDS`
- Produces: `_run_gate(spec, workdir) -> tuple[bool, str, list[dict] | None]`（第三返回：解析出的 `[{id, ok, detail}]`，无哨兵 = None；哨兵行从 output 中剥除，output 保持纯人读文本）；`_split_gate_rules(output) -> tuple[str, list[dict] | None]`（净文本, rules）；`_grade_gate(rules, spec) -> tuple[list, list]`（blocking_failed, advisory_failed；rules=None 时 blocking_failed=[]——退化为旧行为）；`StageSpec["advisory_rules"]`
- 波及：`run_gate_only`（~line 451）与它的既有调用方（grep 定位：smoke 入口等）同步改三元组解包，逐处核对没有漏改第三个元素的解包点

- [ ] **Step 1: 失败测试**

```python
def test_split_gate_rules_parses_sentinel_and_strips():
    # 人读文本 + 哨兵行 → (_split, ...) 净文本无哨兵、rules 带 id/ok/detail；
    # 无哨兵输入 → (原文本, None)；哨兵 JSON 坏 → (原文本含哨兵行, None)（退化全 blocking）

def test_grade_gate_splits_by_whitelist():
    # rules=[{id:"risk-flag",ok:False,...},{id:"scene-cap",ok:True,...}]，outline 白名单 {risk-flag}
    # → blocking_failed=[]、advisory_failed=[risk-flag]；rules=None → 两空表

def test_run_stage_advisory_only_failure_passes_with_markers(pipeline_test_database, monkeypatch):
    # monkeypatch core.skill_executor._run_gate 返回 (False, 人读输出, rules)：
    # 仅 advisory 规则挂（outline 用 risk-flag）→
    # 断言：StageResult.ok is True；findings 携带 [{"gate":"validate","rule":"risk-flag",
    # "severity":"advisory","detail":...}]；产物已落库；事件流含 gate ok=True；
    # **没有修复轮**——LLM 调用次数 = 写稿+finish 两轮即结束。
    # （分级逻辑用确定性 monkeypatch 锁定；真门哨兵链路由 Task 1 测试 + _grade 单测覆盖。）

def test_run_stage_mixed_failure_still_fails(pipeline_test_database):
    # advisory + blocking 同时挂 → ok=False、findings 含两者、产物未落库（既有断言模式）
```

- [ ] **Step 2: 跑失败**
- [ ] **Step 3: 实现**——`_run_gate` 尾部改走 `_split_gate_rules`；run_stage 权威门块：

```python
ok, output, rules = await box.run_gate()
gate_log.append(output[-GATE_TAIL:])
blocking_failed, advisory_failed = _grade_gate(rules, spec)
on_event and on_event({"type": "gate", "ok": ok, "round": rnd,
                       "advisory": len(advisory_failed) if not ok else 0})
if blocking_failed and not ok:
    ...  # 既有失败路径原样：findings 文本含全部输出（含 advisory 行），喂回修复
if not ok and advisory_failed and not blocking_failed:
    gate_findings = [{"gate": "validate", "rule": r["id"], "severity": "advisory",
                      "detail": r["detail"]} for r in advisory_failed]
    # 落到既有通过路径：与 import_findings 合并后随 StageResult 返回（Task 3 再入库）
```

  通过路径的 `StageResult.findings = gate_findings + import_findings`；纯通过（门全绿）= 空表，行为零变化。stage spec 填 `advisory_rules`（outline/script 从 Global Constraints 表；characters 用 `CHARACTERS_ADVISORY_IDS`）
- [ ] **Step 4: 全套测试 + 提交**

```bash
git add core/skill_executor.py tests/pipeline/test_skill_executor.py
git commit -m "feat: executor grades gate rules — advisory-only failures pass with markers, no repair rounds"
```

---

### Task 3: 标记随产物落库（0008 / repository / API）

**Files:**
- Create: `migrations/0008_gate_markers.sql`
- Modify: `core/pipeline_repository.py`（save ~line 27、get ~line 62）
- Modify: `server/pipeline.py`（GET `/projects/{canvas}/artifacts` 列表 ~line 170、GET 单条 ~line 134 附近的响应形状）
- Modify: `core/skill_executor.py`（通过路径 `pipeline_repository.save` 调用点，~line 690）
- Test: `tests/pipeline/test_pipeline_repository.py`（如无则新建）、`tests/pipeline/` 既有 API 测试文件

**Interfaces:**
- Produces: `pipeline_artifacts.findings JSONB NOT NULL DEFAULT '[]'`；`repository.save(db, project_id, art_type, content, *, source_run_id="", findings=None)`——**findings=None = 保留行内既有 findings（UPDATE 不触碰该列）**，`findings=[]` = 显式清空；默认 None 保证既有调用点（导入、集级编辑、改稿脑 apply）不会误清标记；`get` 返回多 `findings` 键；GET 单条/列表响应多 `findings` 字段

- [ ] **Step 1: 失败测试**——repository roundtrip（save 带 findings → get 带回；先存 findings 再 `save(findings=None)` → **保留**；`save(findings=[])` → 清空）；API 单条 GET 响应含 findings（真 Postgres，沿用既有 API 测试的 fixture 模式）；0008 手动同步 pipeline_test 库（0007 先例）
- [ ] **Step 2: 跑失败**
- [ ] **Step 3: 实现**——0008：

```sql
ALTER TABLE pipeline_artifacts ADD COLUMN IF NOT EXISTS findings JSONB NOT NULL DEFAULT '[]';
```

  repository upsert 加 findings 列（EXCLUDED.findings）；run_stage 通过路径 `save(..., findings=result_findings)`——注意 advisory 标记在**重跑覆盖时随 latest-wins 刷新**（新跑无标记即清空，语义正确：标记属于最新产物）
- [ ] **Step 4: 全套测试 + 提交**

```bash
git add migrations/0008_gate_markers.sql core/pipeline_repository.py server/pipeline.py core/skill_executor.py tests/
git commit -m "feat: persist advisory gate markers on artifacts (0008) and expose via API"
```

---

### Task 4: 前端产物面板标记

**Files:**
- Create: `web-vue/src/views/pipeline/GateMarkers.vue`
- Modify: `web-vue/src/views/pipeline/CastPanel.vue`、`ArtPanel.vue`、`StagingPanel.vue`、`StoryboardPanel.vue`
- Test: 无（build + 浏览器复验）

**Interfaces:** Consumes `GET /api/pipeline/projects/{canvas}/artifacts/{type}` 响应的 `findings` 字段（studioStore pipeline.detail 已取全形状，无需改 store）。

- [ ] **Step 1: GateMarkers.vue**——props `findings: Array`；空数组不渲染；非空渲染折叠区「⚠️ N 条内容标记（不阻塞 · 可人工修改或重跑本阶段）」+ 每条 `rule` + `detail`（detail 截 200 字，点击展开全文）。样式照抄面板既有卡片段落类名
- [ ] **Step 2: 四个面板接入**——detail.findings 存在时在产物内容下方挂 `<GateMarkers :findings="detail.findings" />`；outline/script 的标记 v1 只走事件流，不加面板
- [ ] **Step 3: build 零错误 + 提交**

```bash
cd web-vue && npm run build
git add web-vue/src/views/pipeline/
git commit -m "feat: artifact panels render advisory gate markers"
```

---

### Task 5: 文档 + 收尾验证

**Files:**
- Modify: `docs/pipeline/phase2-acceptance.md`（新小节「门分级」：判据、分类表、红线改动记录、e2e 证据；队列补Deferred：art/staging/storyboard 分类、outline/script 标记面板化）

- [ ] **Step 1:** 文档记录（引用 spec 与本计划提交号）
- [ ] **Step 2: 提交**

```bash
git add docs/pipeline/phase2-acceptance.md
git commit -m "docs: gate severity tiers — blocking vs advisory markers"
```

- [ ] **Step 3（controller 执行）：** 合并回 feat/frontend-redesign、重启 Desktop 后端（核验 mtime < 进程启动）、CDP/浏览器复验（建一次性项目 → 跑真模型大概率触发 risk-flag advisory → 产物落库且运行不失败 → 事件流显示「N 条内容标记」；另 SQL 造 findings 验面板渲染）；全套 pytest 终态 ≥ 295

---

## 验收清单

- [ ] 六门 validate 输出带哨兵行；outline/script/characters id 集合被 lock 测试锁定
- [ ] advisory-only 挂 → 产物落库 + findings 标记 + 零修复轮（ScriptedLLM 锁定）；blocking 挂 → 照旧失败不落库（既有测试全绿即证）
- [ ] 无哨兵门（假想旧脚本）→ 全 blocking 退化行为
- [ ] API 与产物面板透出 findings；面板空态不渲染
- [ ] 红线：规则判定逻辑零改动（Task 1 diff 逐行核对）；阈值/确认门/agent 政策零变化（既有测试全绿即证）
