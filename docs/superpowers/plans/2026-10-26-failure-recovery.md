# 失败恢复闭环实施计划：失败阶段可编辑重跑（A）+ 意见注入重试（B）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 运行失败后用户不再只有盲重试——可以查看/编辑失败阶段的草稿、附一条意见，重跑时执行器从修改后的草稿续跑、意见进上下文；门照常守门。

**Architecture:** 失败时执行器把最终草稿随 StageResult 返回，runner 落进 run 行新列 `stage_drafts`；重试请求体扩展 `draft` + `instruction`（落库 `retry_instruction` 列），重跑时作为种子注入 `run_stage`（草稿预填 state + 种子 user 消息）；前端失败卡加草稿编辑器与意见输入。成功后种子自动清除。

**Tech Stack:** 同主线（FastAPI/asyncpg/Vue3/DeepSeek）。

## Global Constraints

- 画布冻结红线零触碰：`web-vue/src/canvas2/`、`web-vue/src/store.js`、`web-vue/src/ai/FloatingAgent.vue`、`web-vue/src/ai/ChatPanel.vue`、`server/registry.py`、`tools/`、`skills/`、`core/skills.py`、`teams/`；`pipeline_skills/**` 只读
- 门语义不变：草稿种子只是起点，finish 组装检查/质量门/预算全部照旧；失败安全（门不过不落库）不变；确认门（outline→script）不变
- 测试惯例：asyncio.run 风格、ScriptedLLM、真 Postgres、真 node 门；基线 **280 passed**
- 迁移惯例：0007 跟随连接池启动自动应用（advisory lock + schema_migrations 记账）
- Commits：lowercase conventional prefix，每任务一个提交
- 涉前端任务 `cd web-vue && npm run build` 零错误

---

### Task 1: 执行器——草稿种子 + StageResult.draft

**Files:**
- Modify: `core/skill_executor.py`（StageResult ~line 207；run_stage 签名 ~line 543 与种子注入 ~line 572-575；两条 return 路径 ~line 644/670）
- Test: `tests/pipeline/test_skill_executor.py`

**Interfaces:**
- Produces: `run_stage(db, project_id, stage, inputs, *, llm, max_iterations, on_event=None, initial_draft: dict | None = None, user_instruction: str = "")`；`StageResult(ok, artifact, iterations, gate_log, findings, draft: dict | None = None)`。既有调用方（smoke CLI、runner）不传新参行为不变

- [ ] **Step 1: 失败测试**

```python
def test_run_stage_seeds_initial_draft_and_instruction():
    # ScriptedLLM：initial_draft={"source":"最后一单","lang":"zh","params":{...}} +
    # user_instruction="第 4 集 warnings 加「雨戏」" →
    # 断言：r1 的 messages 里最后一条 user 消息含草稿 JSON 与意见文本；
    #       模型只补缺键即 finish → 门过 → 落库产物含种子键（模型没重写的键保留种子值）

def test_stage_result_carries_draft_on_failure():
    # 令门失败且预算耗尽（既有 gate-fail 替身模式）→
    # 断言：result.ok is False 且 result.draft 含模型写过的键（非 None/空）
```

- [ ] **Step 2: 跑失败** → TypeError（不认 initial_draft）/ AttributeError（无 draft 字段）

- [ ] **Step 3: 实现**

```python
class StageResult(NamedTuple):
    ok: bool
    artifact: str
    iterations: int
    gate_log: list[str]
    findings: list[dict]
    draft: dict | None = None   # 失败时的最终草稿（A：给编辑器续跑用）；成功路径 None
```

run_stage 签名加 `initial_draft: dict | None = None, user_instruction: str = ""`；messages 初始化后：

```python
state["draft"] = dict(initial_draft) if initial_draft else {}
seed_parts = []
if initial_draft:
    seed_parts.append("【用户修改后的当前草稿】\n"
                      + json.dumps(initial_draft, ensure_ascii=False, indent=2))
if user_instruction.strip():
    seed_parts.append("【用户意见】\n" + user_instruction.strip())
if seed_parts:
    seed_parts.append("请基于这份草稿继续：只修复被点名的问题，"
                      "没有点名且已写好的键不要重写。")
    messages.append({"role": "user", "content": "\n\n".join(seed_parts)})
```

两条 return 都带 `draft=state["draft"]`（成功路径带 None 或不带均可——定为成功 `draft=None`，失败 `draft=state["draft"]`，见 NamedTuple 注释）。

- [ ] **Step 4: 全套测试 + 提交**（280+ 全绿）

```bash
git add core/skill_executor.py tests/pipeline/test_skill_executor.py
git commit -m "feat: run_stage draft seeding and StageResult.draft for failure recovery"
```

---

### Task 2: 存储 + 重试管道（0007 迁移 / RunStore / Runner）

**Files:**
- Create: `migrations/0007_failure_recovery.sql`
- Modify: `core/pipeline_run.py`（RunStore._row/create + 新方法；PipelineRunner.retry ~line 271、_drive_inner executor 调用 ~line 383 与结果落账 ~line 397-410）
- Test: `tests/pipeline/test_pipeline_run.py`

**Interfaces:**
- Produces: `RunStore.save_stage_draft(run_id, stage, draft)`、`RunStore.get_retry_seed(run_id, stage) -> dict`（返回 `{"draft": ..., "instruction": ...}`）、`RunStore.clear_retry_seed(run_id, stage)`；`PipelineRunner.retry(run_id, stage, draft=None, instruction="")`；run 行新键 `stage_drafts`、`retry_instruction`
- 数据形状：`stage_drafts[stage]` = 草稿对象；`retry_instruction` = 单值 TEXT（同一时刻只有一个失败阶段可重试）

- [ ] **Step 1: 0007 迁移**（幂等）

```sql
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS stage_drafts JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS retry_instruction TEXT NOT NULL DEFAULT '';
```

- [ ] **Step 2: 失败测试**

```python
def test_retry_seed_roundtrip_and_clear():
    # store.save_stage_draft(rid, "outline", {"episodes": []}) →
    # get_retry_seed → {"draft": {"episodes": []}, "instruction": ""}；
    # 再存 instruction 后 get_retry_seed 带出；clear 后两键归零

def test_runner_failure_persists_draft_and_success_clears(monkeypatch):
    # 假 executor：第一次返回 StageResult(ok=False, ..., draft={"source":"x"})，
    # retry(draft={"source":"y"}, instruction="只补 warnings") 后第二次被调时
    # 收到 initial_draft={"source":"y"} 与 user_instruction → 返回 ok=True →
    # 断言：stage_drafts 清空、retry_instruction==''、产物落库

def test_retry_endpoint_accepts_draft_and_instruction():
    # POST /runs/{id}/retry {"stage": ..., "draft": {...}, "instruction": "..."}
    # → runner 收到二者（假 runner 捕获参数）；draft 非对象 → 400
```

- [ ] **Step 3: 实现**

RunStore：`_row()` 带 `stage_drafts`/`retry_instruction`；三个新方法用
`stage_drafts = COALESCE(stage_drafts,'{}'::jsonb) || jsonb_build_object($2, $3::jsonb)`、
`stage_drafts - $2`、`retry_instruction=$2` 组合更新。

PipelineRunner.retry 加参：draft 非空 dict 时 `save_stage_draft`；instruction 非空时连同清位一起写 `retry_instruction`（建议 `set_retry_seed` 一个方法内做两件事，避免多次 UPDATE）。`_drive_inner` 的 executor 调用处：

```python
seed = await self.store.get_retry_seed(run_id, stage)
result = await self.executor(
    self.db, run["project_id"], stage, inputs,
    llm=self.llm, max_iterations=self.max_iterations, on_event=_on_event,
    initial_draft=seed["draft"], user_instruction=seed["instruction"],
)
```

结果落账处：`ok=True` → `clear_retry_seed`；`ok=False` → `save_stage_draft(run_id, stage, result.draft or {})`（编辑器始终拿到最新草稿）。基建异常路径不动（无草稿可存）。

- [ ] **Step 4: 全套测试 + 提交**

```bash
git add migrations/0007_failure_recovery.sql core/pipeline_run.py tests/pipeline/test_pipeline_run.py
git commit -m "feat: retry seed pipeline — stage_drafts column, runner seed injection, draft persistence on failure"
```

---

### Task 3: API——retry 扩展 + 草稿读取端点

**Files:**
- Modify: `server/pipeline_runs.py`（RetryBody ~line 115；retry 端点 ~line 219；新增 GET）
- Test: `tests/pipeline/test_pipeline_runs_api.py`

**Interfaces:**
- Produces: `POST /runs/{id}/retry` body `{stage, instruction?: str, draft?: object}`（draft 必须是 JSON 对象、序列化 ≤ 256KB，否则 400）；`GET /runs/{id}/drafts/{stage}` → `{draft: object|null, instruction: str}`（run 不存在 404；无种子则空默认）

- [ ] **Step 1: 失败测试**（见 Task 2 Step 2 第三条；另加 GET 端点：有种子/无种子/404 三态）
- [ ] **Step 2: 跑失败** → 422/404
- [ ] **Step 3: 实现**——RetryBody 加 `instruction: str = ""`、`draft: dict | None = None`；端点校验 draft 形状与大小后透传 `runner.retry(run_id, body.stage, draft=body.draft, instruction=body.instruction)`；新增 GET 端点读 `store.get_retry_seed`
- [ ] **Step 4: 全套测试 + 提交**

```bash
git add server/pipeline_runs.py tests/pipeline/test_pipeline_runs_api.py
git commit -m "feat: retry accepts draft+instruction; GET run stage draft endpoint"
```

---

### Task 4: 前端——失败卡草稿编辑器 + 意见重试

**Files:**
- Modify: `web-vue/src/views/pipeline/CreateRunPanel.vue`（失败卡 ~line 227-243；`retryFailedStage` ~line 151）
- Test: 无（build + 浏览器复验）

**Interfaces:** Consumes `GET /api/pipeline/runs/{id}/drafts/{stage}`、retry body 新字段；文件内既有 `retrying` ref 与按钮防抖模式照抄。

- [ ] **Step 1: 失败卡 UI**——「查看/编辑草稿」折叠区：textarea 预填 `JSON.stringify(draft, null, 2)`（onMounted 失败态时 GET 拉取；解析失败红字提示、重试禁用）；下方「给重试的意见（可选）」单行 input；按钮「带着修改重试」复用 retrying 防抖，body 带 `{stage, draft, instruction}`（draft 为用户编辑后的解析结果；未编辑则不带）
- [ ] **Step 2: build 零错误 + 提交**

```bash
cd web-vue && npm run build
git add web-vue/src/views/pipeline/CreateRunPanel.vue
git commit -m "feat: failed-stage draft editor and instruction-aware retry UI"
```

---

### Task 5: 文档 + 收尾验证

**Files:**
- Modify: `docs/pipeline/phase2-acceptance.md`（队列 3 意见断线注记；新增「失败恢复闭环」小节）

- [ ] **Step 1:** 验收文档记录：A/B 行为、用户操作路径（失败 → 编辑草稿/写意见 → 带着修改重试）、门仍守门的语义
- [ ] **Step 2:** 提交

```bash
git add docs/pipeline/phase2-acceptance.md
git commit -m "docs: failure recovery loop — editable failed-stage drafts and instruction-aware retry"
```

- [ ] **Step 3（controller 执行）：** 合并回 feat/frontend-redesign、重启 Desktop 后端、CDP/浏览器复验（建一次性项目 → 造失败 → 编辑草稿重试）。

---

## 验收清单

- [ ] 全套 pytest ≥ 284 全绿（280 + Task 1 两条 + Task 2 三条 + Task 3 若干）
- [ ] 失败 run 的草稿可从 API 取到、编辑后随重试生效（执行器收到种子，ScriptedLLM 测试锁定）
- [ ] 意见随重试进入模型上下文（消息内容断言锁定）
- [ ] 成功后种子清除；失败后草稿始终是最新一版
- [ ] 门语义零变化：种子只改起点，finish 检查/门/预算/失败安全照旧（既有测试全绿即证）
- [ ] 红线文件零触碰
