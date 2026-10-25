# 阶段 2「大脑入产品」实施计划（gated agent 架构）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 网页粘贴小说 → 状态机编排七阶段管线，创作阶段由带工具的 Agent 在质量门约束下自主迭代，全程无 Claude Code。

**Architecture:** `core/pipeline_run.py` 确定性状态机（0006 表 `pipeline_runs`）驱动阶段顺序与确认门；`core/skill_executor.py` 按阶段规格执行 gated agent loop（创作型）或链+修复（填表型），质量门为 node 子进程，产物经 `pipeline_repository.save(source_run_id=...)` 落库；SSE 直播进度；剧本页 AiPanel 升级为带审批写工具的改稿脑；三套脑在任务 7 收敛为单一来源。

**Tech Stack:** FastAPI + asyncpg（PostgresDatabase，dramaflow-postgres:5432）、Pinia setup store + Vue3 `<script setup>`、Node ≥18 质量门脚本（`pipeline_skills/*/scripts/*.mjs`）、core/llm（TF_ROUTER）。

**Spec:** `docs/superpowers/specs/2026-10-24-skill-layer-refactor-design.md` 第 8 节（2026-10-25 增补）。

## Global Constraints

- 画布冻结红线——不许触碰：`web-vue/src/canvas2/`、`web-vue/src/store.js`、`web-vue/src/ai/FloatingAgent.vue`、`web-vue/src/ai/ChatPanel.vue`、`server/registry.py`、`tools/`、`skills/`、`core/skills.py`、`teams/`。
- 改稿脑不进 registry：自带工具环（`core/pipeline_editor.py`），复用执行器的 loop 机制。
- 质量门只经 node 子进程：`asyncio.create_subprocess_exec("node", ...)`，timeout=120s，每阶段一次 `tempfile.mkdtemp`，禁其他可执行文件。
- agent loop 迭代上限默认 **5**；超限带失败报告退出，不落产物（无脏数据）。
- 所有产物写入必须走 `core.pipeline_repository.save(db, project_id, artifact_type, content, source_run_id=...)`。
- outline → script 之间必须有人工确认门（`POST /runs/{id}/approve`），不可跳过。
- LLM 只经 `core/llm` 既有客户端（TF_ROUTER），不引入新 SDK/key。
- 提示词口径：运行时读取 `pipeline_skills/<skill>/SKILL.md` 与 `references/schema.md` 全文作为 system 上下文（vendored 文件即口径，零漂移），不手抄副本。
- 改稿脑的一切写入过审批（提修改 → 用户批准 → 落稿 → 自动重跑质量门）。
- 每任务收尾：`python -m pytest tests/ -q` 全绿（基线 205，只增不减）+ `cd web-vue && npm run build` 零错误（动前端时）。
- 画布相关行为零回归：现有 canvas 模式聊天与页面行为不变。

---

### Task 1: 前置清障（阶段 1 交接 ①②③⑥ + 0006 migration）

**Files:**
- Modify: `server/pipeline.py`（删 `ensure_started` 的懒启动竞态；`_resolve_schema_version` 迁出）
- Modify: `server/app.py`（lifespan 启动管线连接池）
- Modify: `web-vue/src/api.js`（`toError` 顶层 message/findings）
- Modify: `core/pipeline_artifacts.py`（接收 `resolve_schema_version`）
- Create: `migrations/0006_pipeline_runs.sql`
- Test: `tests/pipeline/test_pipeline_api.py`（追加 delete/schema 用例）

**Interfaces:**
- Produces: `core.pipeline_artifacts.resolve_schema_version(art: ArtifactType, requested: str) -> tuple[str, list[dict]]`（含 frontmatter 锚点缓存）；`DELETE /api/pipeline/projects/{canvas}/artifacts/{type}`；`pipeline_api.startup()` 供 lifespan 调用；`migrations/0006_pipeline_runs.sql` 的 `pipeline_runs` 表（Task 3 消费）。

- [ ] **Step 1: 0006 migration**

```sql
-- migrations/0006_pipeline_runs.sql
CREATE TABLE IF NOT EXISTS pipeline_runs (
  run_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  novel_text TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'running'
    CHECK (status IN ('running','gated','failed','done','cancelled')),
  stage TEXT NOT NULL DEFAULT 'outline',
  stage_status JSONB NOT NULL DEFAULT '{}'::jsonb,
  feedback TEXT NOT NULL DEFAULT '',
  findings JSONB,
  created_at DOUBLE PRECISION NOT NULL DEFAULT (EXTRACT(EPOCH FROM now())),
  updated_at DOUBLE PRECISION NOT NULL DEFAULT (EXTRACT(EPOCH FROM now()))
);
CREATE INDEX IF NOT EXISTS pipeline_runs_project_idx ON pipeline_runs(project_id, updated_at);
```

- [ ] **Step 2: 连接池竞态修复**——`server/pipeline.py` 新增模块级启动函数，lifespan 里调用一次；`ensure_started` 保留为幂等兜底但不再有 "两个并发首请求都见 pool is None" 的窗口（用 `asyncio.Lock`）：

```python
_pool_lock: asyncio.Lock | None = None

async def startup() -> None:
    """lifespan 调用：启动管线数据库池（幂等）。"""
    global _pool_lock
    _pool_lock = _pool_lock or asyncio.Lock()
    await ensure_started()

async def ensure_started() -> None:
    global _pool_lock
    _pool_lock = _pool_lock or asyncio.Lock()
    if _database is not None and _database.pool is not None:
        return
    async with _pool_lock:
        if _database is not None and _database.pool is None:
            await _database.start()
```

`server/app.py` lifespan（现有 startup 钩子处）追加一行 `await pipeline_api.startup()`。

- [ ] **Step 3: `_resolve_schema_version` 迁至 core**——把 `server/pipeline.py` 的 `_resolve_schema_version` 及 frontmatter 锚点缓存整体搬到 `core/pipeline_artifacts.py`，重命名 `resolve_schema_version`；`server/pipeline.py` 改为 `from core.pipeline_artifacts import resolve_schema_version` 并保留 `_resolve_schema_version = resolve_schema_version` 兼容别名。既有 schema_version API 测试不改应全绿。

- [ ] **Step 4: delete 路由**——`server/pipeline.py` 追加：

```python
@router.delete("/projects/{canvas}/artifacts/{artifact_type}")
async def delete_artifact(canvas: str, artifact_type: str):
    art = _require_type(artifact_type)          # 未知类型沿用 _unknown_type 404
    if _database is None or _database.pool is None:
        return _postgres_required()
    await pipeline_repository.delete(_database, canvas, art.name)
    return {"message": f"已删除 {canvas}/{art.name}"}
```

（`pipeline_repository.delete` 阶段 0 已有，直接消费。）

- [ ] **Step 5: api.js toError 扩展**

```javascript
// web-vue/src/api.js — toError 内，读 detail 之后追加：
  if (payload && typeof payload === 'object' && payload.message) {
    const findings = Array.isArray(payload.findings) ? `（${payload.findings.length} 条 findings）` : ''
    return new Error(`${payload.message}${findings}`)
  }
```

- [ ] **Step 6: 测试**——`tests/pipeline/test_pipeline_api.py` 追加：

```python
async def test_delete_artifact_roundtrip(client, seeded_project):
    post = await client.post("/api/pipeline/projects/p1/artifacts/outline",
                             json={"content": seeded_project["outline"]})
    assert post.status_code == 200
    dele = await client.delete("/api/pipeline/projects/p1/artifacts/outline")
    assert dele.status_code == 200
    got = await client.get("/api/pipeline/projects/p1/artifacts")
    assert got.json()["artifacts"] == []
```

（`seeded_project` 夹具若不存在则按该文件既有 fixture 风格内联构造 outline 最小内容。）

- [ ] **Step 7: 全量验证 + 提交**

Run: `python -m pytest tests/ -q`（205+ 全绿）→ `git add -A && git commit -m "feat: phase-2 groundwork — pool startup lock, schema resolver to core, delete route, 0006"`

---

### Task 2: 技能执行器骨架 + novel-outline 单阶段端到端

**Files:**
- Create: `core/skill_executor.py`
- Create: `tests/pipeline/test_skill_executor.py`

**Interfaces:**
- Consumes: `core.pipeline_repository.save(db, project_id, artifact_type, content, source_run_id)`；`pipeline_skills/novel-outline/`（SKILL.md、references/schema.md、scripts/novel-outline.mjs）。
- Produces:
  - `@dataclass StageResult: ok: bool; artifact_type: str; iterations: int; gate_log: list[str]; findings: list[dict]`
  - `async def run_stage(db, project_id: str, stage: str, inputs: dict, *, llm, max_iterations: int = 5, on_event=None) -> StageResult`
  - `LLMPort` 协议：`async def complete(messages: list[dict], tools: list[dict]) -> object`，返回对象需带 `.text: str` 与 `.tool_calls: list[dict]`（每项 `{"name": str, "args": dict}`）；实现者写 `_RouterLLM` 适配器把 core/llm 既有响应整形为此协议（Task 3 起 runner 注入）。
  - 事件回调：`on_event(event: dict)`，事件形如 `{"type":"tool","name":...,"args_summary":...}` / `{"type":"gate","ok":bool,"round":n}` / `{"type":"done","ok":bool}`。

- [ ] **Step 1: 阶段规格表（写死在模块顶部）**

```python
STAGE_SPECS: dict[str, dict] = {
    "outline": {
        "skill": "novel-outline", "artifact": "outline", "policy": "agent",
        "playbook": ["SKILL.md", "references/schema.md"],
        "sections": ["adaptation", "characters", "scenes", "props", "beats", "episodes"],
        "gate": ["scripts/novel-outline.mjs", "validate", "outline.json", "--stage", "full"],
        "gate_needs": [],   # 无上游文件
        "task": "按 playbook 把用户小说改编为短剧大纲五件套，写全六节后跑质量门，全过才 finish。",
    },
}
```

- [ ] **Step 2: 失败测试**——`tests/pipeline/test_skill_executor.py`（ScriptedLLM 按队列吐轮次）：

```python
class ToolCallReq(dict):
    pass

class ScriptedLLM:
    """complete() 按预设轮次返回；记录 messages 供断言。"""
    def __init__(self, turns):
        self.turns = list(turns); self.calls = []
    async def complete(self, messages, tools):
        self.calls.append(list(messages))
        return SimpleNamespace(text="", tool_calls=[self.turns.pop(0)])

@pytest.mark.asyncio
async def test_outline_stage_agent_loop_saves_artifact(fake_db, novel_text):
    good_outline = json.loads(pathlib.Path("tests/fixtures/pipeline_run1/outline.json").read_text("utf-8"))
    llm = ScriptedLLM([
        {"name": "write_draft", "args": {"section": "episodes", "data": good_outline["episodes"]}},
        {"name": "write_draft", "args": {"section": "characters", "data": good_outline["characters"]}},
        # …… adaptation/scenes/props/beats 各一轮（fixture 有则全量写，无则最小合法集）
        {"name": "run_quality_gate", "args": {}},
        {"name": "finish", "args": {}},
    ])
    events = []
    result = await run_stage(fake_db, "proj-x", "outline", {"novel_text": novel_text},
                             llm=llm, on_event=events.append)
    assert result.ok and result.artifact_type == "outline"
    assert any(e["type"] == "gate" and e["ok"] for e in events)
    saved = await pipeline_repository.get(fake_db, "proj-x", "outline")
    assert saved["content"]["source"] == good_outline["source"]

@pytest.mark.asyncio
async def test_gate_failure_exhausts_iterations_without_saving(fake_db, novel_text):
    llm = ScriptedLLM([{"name": "run_quality_gate", "args": {}} for _ in range(5)]
                      + [{"name": "finish", "args": {}}])
    result = await run_stage(fake_db, "proj-x", "outline", {"novel_text": novel_text}, llm=llm)
    assert not result.ok and result.findings
    with pytest.raises(KeyError):
        await pipeline_repository.get(fake_db, "proj-x", "outline")
```

`fake_db` 夹具沿用 `tests/pipeline/test_pipeline_repository.py` 的 pipeline_test 库装配方式（同文件内已存在，导入复用）。

- [ ] **Step 3: 跑失败**——`pytest tests/pipeline/test_skill_executor.py -v`，预期 FAIL（module 不存在）。

- [ ] **Step 4: 实现 `core/skill_executor.py`**

核心结构（工具分发 + loop + 门）：

```python
MAX_GATE_TIMEOUT = 120

class _ToolBox:
    def __init__(self, spec, workdir, inputs, state):
        self.spec, self.workdir, self.inputs, self.state = spec, workdir, inputs, state
    def specs(self) -> list[dict]:   # OpenAI 风格 tool 定义
        ...
    async def dispatch(self, name: str, args: dict) -> str:
        # write_draft: state["draft"][args["section"]] = args["data"]; 回 "ok"
        # run_quality_gate: 组装 draft→outline.json；返回门原始输出尾部（≤2000 字符）
        # read_gate_report: 同上门输出全文
        # finish: state["finished"] = True; 回 "ok"
        ...

async def run_stage(db, project_id, stage, inputs, *, llm, max_iterations=5, on_event=None) -> StageResult:
    spec = STAGE_SPECS[stage]
    workdir = pathlib.Path(tempfile.mkdtemp(prefix=f"stage-{stage}-"))
    state = {"draft": {}, "finished": False}
    box = _ToolBox(spec, workdir, inputs, state)
    system = _compose_system(spec)      # SKILL.md + schema.md 全文 + JSON 纪律 + 工具说明
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": spec["task"] + "\n\n【小说原文】\n" + inputs["novel_text"]}]
    gate_log, findings = [], []
    for rnd in range(1, max_iterations + 1):
        resp = await llm.complete(messages, tools=box.specs())
        messages.append({"role": "assistant", "content": resp.text or ""})
        for call in resp.tool_calls:
            out = await box.dispatch(call["name"], call.get("args", {}))
            on_event and on_event({"type": "tool", "name": call["name"], "args_summary": str(call.get("args"))[:120]})
            messages.append({"role": "tool", "name": call["name"], "content": out})
        if not state["finished"]:
            continue
        ok, output = _run_gate(spec, workdir)          # node 子进程，见下
        gate_log.append(output[-2000:])
        on_event and on_event({"type": "gate", "ok": ok, "round": rnd})
        if ok:
            content = _finalize_draft(spec, state)     # 拼 sections → 完整 JSON
            await pipeline_repository.save(db, project_id, spec["artifact"], content,
                                           source_run_id=f"stage:{stage}")
            return StageResult(True, spec["artifact"], rnd, gate_log, [])
        findings = [{"gate": "validate", "detail": output[-800:]}]
        messages.append({"role": "user", "content": "质量门未过，报告见上。修订相关 section 后重新 finish。"})
        state["finished"] = False
    return StageResult(False, spec["artifact"], max_iterations, gate_log, findings)

def _run_gate(spec, workdir) -> tuple[bool, str]:
    cmd = ["node", str(_SKILLS_ROOT / spec["skill"] / *spec["gate"])]   # gate 列表首元素拼路径
    proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                          timeout=MAX_GATE_TIMEOUT, encoding="utf-8")
    return proc.returncode == 0, (proc.stdout + "\n" + proc.stderr)
```

注意：`_run_gate` 用同步 `subprocess.run`（门是秒级脚本，事件循环内可接受；若要纯异步换 `asyncio.create_subprocess_exec`，二选一并在代码注释说明取舍）。`_compose_system` 用 `pathlib.Path(__name__).…` 定位 `pipeline_skills/<skill>/`，读 playbook 文件全文拼接。

- [ ] **Step 5: 跑绿**——`pytest tests/pipeline/test_skill_executor.py -v` PASS；`python -m pytest tests/ -q` 全绿。
- [ ] **Step 6: 真链路冒烟（不算测试，手动一次）**——用 `TF_ROUTER` 真跑一次 outline 阶段（脚本 `python -m core.skill_executor outline tests/fixtures/sample_novel_v1.txt` 入口 `__main__`），确认真 LLM 下 14 门通过、产物落 pipeline_test 库。跑不通（真 LLM 轮次乱）就修提示词纪律，直到冒烟过。
- [ ] **Step 7: 提交**——`git commit -m "feat: skill executor — gated agent loop, outline stage e2e"`

---

### Task 3: run 状态机 + 确认门 + SSE（后端）

**Files:**
- Create: `core/pipeline_run.py`
- Create: `server/pipeline_runs.py`
- Modify: `server/app.py`（挂载 runs 路由）
- Create: `tests/pipeline/test_pipeline_run.py`、`tests/pipeline/test_pipeline_runs_api.py`

**Interfaces:**
- Consumes: Task 2 的 `run_stage`；Task 1 的 `pipeline_runs` 表；`core.postgres.PostgresDatabase`。
- Produces:
  - `STAGE_ORDER = ["outline", "script", "characters", "art", "staging", "storyboard"]`；确认门在 outline 之后。
  - `class RunStore: create/get/update_stage/set_status/append_feedback`（async，asyncpg）
  - `class PipelineRunner: async def start(run_id: str) -> None`（asyncio.create_task 后台推进；内部逐阶段调 `run_stage`，on_event 同时写 stage_status 与 broker）
  - `class EventBroker: publish(run_id, event: dict) / subscribe(run_id) -> asyncio.Queue`（进程内；实现前先读 `core/events.py`，若其已有等价进程内 pubsub 则委托并在注释标注）
  - REST：`POST /api/pipeline/runs {project_id, novel_text}`、`GET /runs/{id}`、`GET /runs/{id}/events`（SSE）、`POST /runs/{id}/approve`、`POST /runs/{id}/retry {stage}`、`POST /runs/{id}/feedback {text}`

- [ ] **Step 1: 失败测试（状态机单测，注入 fake executor）**

```python
class FakeExecutor:
    def __init__(self): self.calls = []
    async def __call__(self, db, project, stage, inputs, **kw):
        self.calls.append(stage)
        if stage == "outline" and getattr(self, "fail_first", False):
            self.fail_first = False
            return StageResult(False, stage, 5, ["gate: fail"], [{"gate": "x"}])
        return StageResult(True, stage, 1, ["ok"], [])

@pytest.mark.asyncio
async def test_runner_pauses_at_confirmation_gate(fake_db):
    store, broker = make_store(fake_db), EventBroker()
    runner = PipelineRunner(store, fake_db, executor=FakeExecutor(), broker=broker)
    run = await store.create("p1", "小说……")
    await runner.start(run["run_id"]); await runner.wait_idle()
    state = await store.get(run["run_id"])
    assert state["status"] == "gated" and state["stage"] == "outline"
    assert fake-run executor.calls == ["outline"]          # 确认门前不推进
    await store.approve(run["run_id"]); await runner.resume(run["run_id"]); await runner.wait_idle()
    state = await store.get(run["run_id"])
    assert state["status"] == "done"
    assert [c["stage"] for c in store.stage_history(run["run_id"])] == STAGE_ORDER
```

（`stage_history` 从 stage_status JSONB 派生；表结构不变。）

- [ ] **Step 2: 实现 `core/pipeline_run.py`**——RunStore（SQL 直写 pipeline_runs，UPSERT 更新 updated_at）、EventBroker（`dict[str, list[asyncio.Queue]]` + `publish` 扇播 + `subscribe` 注册）、PipelineRunner：

```python
class PipelineRunner:
    def __init__(self, store, db, *, executor, broker, llm, max_iterations=5): ...
    async def start(self, run_id):           # create_task(_drive(run_id))，任务句柄存 self._tasks
        self._tasks[run_id] = asyncio.create_task(self._drive(run_id))
    async def _drive(self, run_id):
        run = await self.store.get(run_id)
        stages = STAGE_ORDER[STAGE_ORDER.index(run["stage"]):]
        for stage in stages:
            await self._set_stage(run_id, stage, "running")
            inputs = {"novel_text": run["novel_text"], **await self._upstream(run_id, stage)}
            result = await self.executor(self.db, run["project_id"], stage, inputs,
                                         llm=self.llm, on_event=lambda e: self.broker.publish(run_id, e))
            await self.store.update_stage(run_id, stage, {"status": "done" if result.ok else "failed",
                                          "attempts": result.iterations, "findings": result.findings})
            if not result.ok:
                await self.store.set_status(run_id, "failed", stage=stage, findings=result.findings)
                self.broker.publish(run_id, {"type": "failed", "stage": stage}); return
            if stage == "outline":
                await self.store.set_status(run_id, "gated", stage=stage)
                self.broker.publish(run_id, {"type": "gate", "stage": "outline"}); return
        await self.store.set_status(run_id, "done", stage="storyboard")
        self.broker.publish(run_id, {"type": "done"})
    async def resume(self, run_id): self.start(run_id)   # approve 后从断点阶段继续
    async def retry(self, run_id, stage): ...            # 置该阶段 attempts=0 后 start
```

`_upstream` 按阶段从 pipeline_artifacts 读上游产物塞进 inputs（如 script 阶段带 outline content）。

- [ ] **Step 3: SSE 路由**——`server/pipeline_runs.py`，SSE 端点仿 `server/studio.py:822` 的 StreamingResponse 模式：

```python
@router.get("/runs/{run_id}/events")
async def run_events(run_id: str):
    queue = _broker.subscribe(run_id)
    async def gen():
        yield f"data: {json.dumps({'type': 'hello'})}\n\n"
        while True:
            event = await queue.get()
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            if event.get("type") in ("done", "failed", "gate"):
                if event["type"] == "done": break
    return StreamingResponse(gen(), media_type="text/event-stream")
```

（连接生命周期：客户端断开由 StreamingResponse 取消生成器；队列积压用 `queue.get` 前清空防串台。）路由以 `APIRouter(prefix="/api/pipeline")` 新建，`server/app.py` 在 pipeline_api 之后挂载；`configure(store, runner, broker)` 注入依赖，工厂 `build_pipeline_runs(db, llm)` 供 lifespan 装配。

- [ ] **Step 4: API 测试**——`tests/pipeline/test_pipeline_runs_api.py`：POST /runs 200 且库里见行；fake executor 直达 gated；POST approve 后（wait_idle）status done；POST retry 对 failed 阶段重置重跑；SSE 端点用 `async with client.stream("GET", ...)` 收到 hello + gate 事件。
- [ ] **Step 5: 全绿 + 提交**——`git commit -m "feat: pipeline run state machine with confirmation gate and SSE"`

---

### Task 4: 创作入口前端（粘贴小说 → 直播 → 确认门）

**Files:**
- Modify: `web-vue/src/studio/studioStore.js`（追加 run 状态与动作）
- Create: `web-vue/src/views/pipeline/CreateRunPanel.vue`
- Modify: `web-vue/src/views/PipelineView.vue`（顶部挂 CreateRunPanel）
- Modify: `web-vue/src/api.js`（无新增函数则不动；SSE 用原生 EventSource）

**Interfaces:**
- Consumes: Task 3 REST/SSE。
- Produces: studioStore 新增 `run: {id, status, stage, stages: {}, events: []}` 与 `startRun(projectId, novel) / approveRun() / retryRun(stage) / sendRunFeedback(text) / closeRunStream()`；`CreateRunPanel.vue` 供 PipelineView 复用。

- [ ] **Step 1: studioStore 追加**（沿用文件内既有 state/箭头函数风格）：

```javascript
    run: { id: '', status: '', stage: '', stages: {}, events: [] },
    async startRun(projectId, novel) {
      const data = await postJSON(`/api/pipeline/runs`, { project_id: projectId, novel_text: novel })
      this.run = { id: data.run_id, status: 'running', stage: '', stages: {}, events: [] }
      this._openRunStream()
    },
    _openRunStream() {
      this._es?.close()
      const es = new EventSource(`/api/pipeline/runs/${this.run.id}/events`)
      es.onmessage = (e) => {
        const msg = JSON.parse(e.data)
        this.run.events.push(msg)
        if (msg.type === 'gate') { this.run.status = 'gated'; this.run.stage = msg.stage; void this.loadPipeline() }
        if (msg.type === 'done') { this.run.status = 'done'; es.close(); void this.loadPipeline() }
        if (msg.type === 'failed') { this.run.status = 'failed'; this.run.stage = msg.stage; es.close() }
      }
      this._es = es
    },
    async approveRun() { await postJSON(`/api/pipeline/runs/${this.run.id}/approve`, {}); this.run.status = 'running'; this._openRunStream() },
    async retryRun(stage) { await postJSON(`/api/pipeline/runs/${this.run.id}/retry`, { stage }); this.run.status = 'running'; this._openRunStream() },
    async sendRunFeedback(text) { await postJSON(`/api/pipeline/runs/${this.run.id}/feedback`, { text }) },
```

（`postJSON` 为 api.js 既有导出；若仅 `request` 封装则按文件现状等价实现。）

- [ ] **Step 2: `CreateRunPanel.vue`**——三段式：`<textarea>`（小说，≥50 字才可点开始）→ 运行中（events 倒序展示：工具调用摘要行 + 门结果行，gated 时高亮）→ gated（「查看大纲并确认」= 滚动到 PipelineOutlinePanel + 「确认，开写剧本」按钮调 approveRun；输入框 + 「带意见重写」调 sendRunFeedback）；failed 时「重试本阶段」调 retryRun。
- [ ] **Step 3: PipelineView 顶部挂载**`<CreateRunPanel v-if="!hasArtifacts || run.id" />`（有产物且无活动 run 时不占位）。
- [ ] **Step 4: 构建验证**——`cd web-vue && npm run build` 零错误；`python -m pytest tests/ -q` 全绿。
- [ ] **Step 5: 提交**——`git commit -m "feat: creation wizard — paste novel, live agent progress, confirmation gate"`

---

### Task 5: novel-script 执行器 + outline→script 自动推进

**Files:**
- Modify: `core/skill_executor.py`（追加 script 规格）
- Modify: `tests/pipeline/test_skill_executor.py`

**Interfaces:**
- Consumes: Task 2 机制；`pipeline_skills/novel-script/`（SKILL.md、references/schema.md、scripts/novel-script.mjs）。
- Produces: `STAGE_SPECS["script"]`；上游注入约定——`inputs["outline"]` 为已落库 outline content，executor 在 workdir 写 `outline.json` 供门用。

- [ ] **Step 1: 规格**

```python
"script": {
    "skill": "novel-script", "artifact": "script", "policy": "agent",
    "playbook": ["SKILL.md", "references/schema.md"],
    "sections": ["episodes"],           # 按 ep 逐集 write_draft(section="episodes", data=[...])
    "gate": ["scripts/novel-script.mjs", "validate", "script.json", "--outline", "outline.json"],
    "gate_needs": ["outline"],
    "task": "按 playbook 把大纲落成分集剧本（结构化节拍流），逐集写 episodes 后跑质量门，全过才 finish。",
},
```

`_ToolBox.dispatch` 增补：workdir 初始化时把 `inputs["outline"]` 写为 `outline.json`（gate_needs 驱动，通用逻辑不是 script 特判）。

- [ ] **Step 2: 测试**——ScriptedLLM 逐集写 2 集 → gate（fixture 用 `tests/fixtures/pipeline_run1/script.json` 的对应集裁剪）→ finish；另加时长门失败→修订→过 的轮次脚本；断言落库 script content 的 `episodes[].ep` 与 inputs outline 一致。
- [ ] **Step 3: 自动推进已由 Task 3 runner 覆盖**（STAGE_ORDER 顺序 + approve 后 resume），本任务只验证 runner 用真 STAGE_SPECS 时 outline→script 串联（fake LLM 全程脚本化）。
- [ ] **Step 4: 全绿 + 提交**——`git commit -m "feat: script stage executor with duration gates"`

---

### Task 6: 制片四层执行器 + 制片台运行/重试 UI

**Files:**
- Modify: `core/skill_executor.py`（characters/art/staging/storyboard 规格）
- Modify: `web-vue/src/views/PipelineView.vue`（面板状态化）
- Modify: `web-vue/src/views/pipeline/{CastPanel,ArtPanel,StagingPanel,StoryboardPanel}.vue`（接收 `state` prop：idle/running/done/failed）
- Test: `tests/pipeline/test_skill_executor.py` 追加

**Interfaces:**
- Consumes: Task 5 机制；四个 shuohao 技能目录。
- Produces: `STAGE_SPECS["characters"|"art"|"staging"|"storyboard"]`；上游链：characters←outline、art←outline、staging←script+art、storyboard←script+art+staging（`gate_needs` 声明，executor 通用落盘）。

- [ ] **Step 1: 规格（gate 命令按各技能 SKILL.md 的 validate 用法抄写）**

```python
"characters": {"skill": "novel-characters", "artifact": "characters", "policy": "chain_repair",
               "playbook": ["SKILL.md", "references/schema.md"], "sections": ["characters"],
               "gate": ["scripts/novel-characters.mjs", "validate", "cast.json"], "gate_needs": ["outline"],
               "task": "从小说与大纲拆角色表（cast.json 形状），过质量门后 finish。"},
"art":         {"skill": "novel-art", "artifact": "art", "policy": "chain_repair",
                "playbook": ["SKILL.md", "references/schema.md"], "sections": ["art"],
                "gate": ["scripts/novel-art.mjs", "validate", "art.json"], "gate_needs": ["outline"],
                "task": "产出美术设定（场景锚点/光照状态/道具），过质量门后 finish。"},
"staging":     {"skill": "novel-staging", "artifact": "staging", "policy": "agent",
                "playbook": ["SKILL.md", "references/schema.md"], "sections": ["staging"],
                "gate": ["scripts/novel-staging.mjs", "validate", "staging.json", "--script", "script.json"],
                "gate_needs": ["script", "art"],
                "task": "为每场戏排站位与走位，过质量门后 finish。"},
"storyboard":  {"skill": "novel-storyboard", "artifact": "storyboard", "policy": "agent",
                "playbook": ["SKILL.md", "references/schema.md"], "sections": ["storyboard"],
                "gate": ["scripts/novel-storyboard.mjs", "validate", "storyboard.json"],
                "gate_needs": ["script", "art", "staging"],
                "task": "把剧本+站位落成镜头表与首帧/视频提示词，过质量门后 finish。"},
```

（实现 Step 里先读各技能 SKILL.md 校正 validate 实参——文件名/旗标以技能文档为准，上面写错就以文档改正并回改本规格。`policy: "chain_repair"` 的执行 = 单次 complete 产全文 → 门 → 未过则把门报告作为 user 消息再 complete 一次（恰好一轮修复），复用同一 loop 骨架，只是禁用 write_draft 分节与多轮。）
- [ ] **Step 2: 测试**——characters 用 chain_repair 轮次脚本（1 次产出 + 门过）；staging 用 agent 轮次脚本 + 门失败修复过；断言 source_run_id 形如 `stage:characters`。
- [ ] **Step 3: PipelineView 状态化**——面板容器从 run.stages 读状态：`idle`（灰）/`running`（进度条 + 最近一条 event）/`failed`（红 + 「重试」按钮调 retryRun）/`done`（现有内容渲染）。无 run 时保持现状只读。
- [ ] **Step 4: 构建 + 全绿 + 提交**——`git commit -m "feat: production stages executor and studio run states"`

---

### Task 7: 三套脑切换（UI 换源 + 资产库桥 + 四卡下线）

**Files:**
- Modify: `web-vue/src/views/ScriptView.vue`（摘除 StoryAnalysisCards/AssetTablesPanel 挂载，换管线源组件）
- Create: `server/pipeline_assets.py`（同步桥路由）
- Modify: `server/app.py`（挂载）
- Create: `tests/pipeline/test_pipeline_assets.py`

**Interfaces:**
- Consumes: `core.asset_tables.merge_asset_tables(existing, tables)`；studio 侧既有 sync 执行路径（`server/studio.py` 的 `/asset-tables/sync` handler，读它拿到 plan 执行的内部函数名并复用）。
- Produces: `POST /api/pipeline/projects/{canvas}/artifacts/{type}/sync-assets`（type ∈ characters|art）→ `{"message": "...", "added": n}`。

- [ ] **Step 1: 映射与桥**——`core/asset_tables.py` 的行模型为准（先读该文件；已知 CharacterRow: name/description/bio，LookRow: name），映射：

```python
def characters_to_rows(content: dict) -> list[dict]:
    return [{"name": c["name"],
             "description": (c.get("role") or "")[:500],
             "bio": (c.get("arc") or "")[:1500]}
            for c in content.get("characters", [])]

def art_to_look_rows(content: dict) -> list[dict]:
    names = []
    for scene in content.get("scenes", []):
        if scene.get("style") and scene["style"] not in names:
            names.append(scene["style"])
    return [{"name": n} for n in names]
```

（art.json 真实字段以 `docs/pipeline/data-contract.md` 为准校正取值路径；映射只增不改不删——交给 `merge_asset_tables`。）
- [ ] **Step 2: 路由**——读产物 → 组 `AssetTables` → `merge_asset_tables` → 复用 studio 的 plan 执行函数落库 → 返回 added 数。资产库不可用时 503（沿用管线 503 形态 `{"message": ...}`）。
- [ ] **Step 3: 测试**——fake_db 种子资产库行 + characters 产物 → sync → 断言只新增不存在的行、已存在行字段未被覆盖（merge 语义回归）。
- [ ] **Step 4: 剧本页换源**——ScriptView 摘除 `<StoryAnalysisCards>`/`<AssetTablesPanel>` 挂载与相关 import（组件文件保留，后端四卡路由保留一个版本周期）；原面板位置给「同步资产库」按钮（调 sync-assets，成功 toast added 数）。
- [ ] **Step 5: 构建 + 全绿 + 提交**——`git commit -m "feat: pipeline-to-asset-library bridge; script page single source"`

---

### Task 8: 改稿脑 + produce 接线 + 运行详情 + 端到端验收

**Files:**
- Create: `core/pipeline_editor.py`
- Modify: `server/app.py`（ChatRequest.mode 增 `"pipeline_editor"`；分支接 editor loop）
- Create: `server/pipeline_runs.py` 内追加 `GET /runs/{id}/stats`
- Modify: `web-vue/src/views/ScriptView.vue`（produce 按钮 + AiPanel mode）
- Modify: `web-vue/src/views/PipelineView.vue`（运行详情抽屉）
- Create: `tests/pipeline/test_pipeline_editor.py`、`tests/pipeline/test_phase2_acceptance.py`

**Interfaces:**
- Consumes: Task 2 loop 骨架；`ChatRequest.mode`（app.py 既有 Literal）；`invocation_store` 账本。
- Produces: `mode="pipeline_editor"`（带工具改稿，写入过审批）；`GET /api/pipeline/runs/{id}/stats → {"iterations": n, "gate_passes": n, "stages": {...}}`；验收测试全链。

- [ ] **Step 1: 改稿脑核心**——`core/pipeline_editor.py` 自带工具环（复用 `skill_executor` 的 loop 与 LLMPort）：

```python
EDITOR_TOOLS = ("read_artifact", "propose_edit", "finish")
# read_artifact(type) → 产物 JSON 文本
# propose_edit(type, summary, new_content) → 存入 pending_edits[project]（服务端内存 dict），
#   回 "已提案，等待用户审批"；**没有 apply 工具**——落稿只经审批端点。
```

审批落稿端点（`server/pipeline_runs.py`）：`POST /api/pipeline/projects/{canvas}/editor/apply {edit_id}` → 校验 pending 存在 → `pipeline_repository.save(source_run_id="editor")` → 若 type==script 则对该产物重跑 10 道门（`skill_executor.run_gate_only(artifact_type, content, upstreams)`——Task 2 的 `_run_gate` 提升为模块级公开函数）→ 返回门结果；门不过则回滚（重写旧 content）并返回 findings。pending_edits 带 10 分钟 TTL。

- [ ] **Step 2: ChatRequest 分支**——mode Literal 增 `"pipeline_editor"`；app.py 聊天分支仿 script_qa：不走 registry，改调 `pipeline_editor.chat(project_id, history)`（内部 loop 把工具结果续进 messages）；系统提示声明只读+提案边界。script_qa 既有字节锁定测试不动，新增 editor 分支测试：propose 产生 pending、未 apply 前产物不变、apply 后产物更新且门过、apply 门不过回滚。
- [ ] **Step 3: produce 按钮**——`ScriptView.vue:454` 的 `:disabled` 改为 `busy || producing || (!script?.script && !pipelineOutlineReady)`；点击 handler：有 canvas 剧本走原 produce；否则 `router.push` 到本项目制片台（分镜投产包已在库，制作入口从制片台进）。按钮文案 pipeline-only 时显示「去制片台」。
- [ ] **Step 4: 运行详情**——`GET /runs/{id}/stats`：从 invocation_store 按 `source_run_id`/run 标签聚合（读 `core/invocation_store.py` 选既有查询口径；若无聚合查询则按 run 标签过滤后内存聚合）：`{"iterations": ..., "gate_passes": ..., "stages": {stage: {"attempts": n, "ok": bool}}}`。PipelineView 加「运行详情」抽屉展示。
- [ ] **Step 5: 端到端验收测试**——`tests/pipeline/test_phase2_acceptance.py`：

```python
@pytest.mark.asyncio
async def test_full_run_novel_to_storyboard(fake_db, sample_novel):
    llm = ScriptedLLM(_script_all_stages())        # 六阶段工具轮次脚本（fixtures 裁剪）
    runner = build_runner(fake_db, llm=llm)
    run = await runner.create_and_start("acc-2", sample_novel)
    await runner.wait_idle()
    assert (await runner.store.get(run["run_id"]))["status"] == "gated"
    await runner.store.approve(run["run_id"]); await runner.resume_and_wait(run["run_id"])
    state = await runner.store.get(run["run_id"])
    assert state["status"] == "done"
    for t in ("outline", "script", "characters", "art", "staging", "storyboard"):
        assert (await pipeline_repository.get(fake_db, "acc-2", t))["content"]

@pytest.mark.asyncio
async def test_stage_failure_leaves_no_dirty_data(fake_db, sample_novel):
    llm = ScriptedLLM(_script_all_stages(break_stage="staging"))   # staging 五轮门全挂
    ...
    assert state["status"] == "failed" and state["stage"] == "staging"
    with pytest.raises(KeyError):
        await pipeline_repository.get(fake_db, "acc-2", "staging")   # 失败阶段无产物
    for t in ("outline", "script", "characters", "art"):
        assert (await pipeline_repository.get(fake_db, "acc-2", t))["content"]  # 上游完好
```

- [ ] **Step 6: 浏览器手工验收（记录进 spec 勾选括注）**——起服务（Postgres 后端），真实小说粘贴→大纲→确认→全链，四层面板点亮、改稿脑提案→审批→体检一轮、失败重试一次。
- [ ] **Step 7: 全绿 + 提交**——`git commit -m "feat: pipeline editor agent, produce wiring, run stats, phase-2 acceptance"`

---

## Self-Review 记录

1. **Spec 覆盖**：8.3 架构图各组件 ↔ Task 1-4；制片四层 ↔ Task 6；三套脑合并 ↔ Task 7；改稿脑/produce/评测 ↔ Task 8；8.4 八任务一一对应；8 条验收中前 5 条由 Task 3-6+8 交付，后 3 条分别由 Task 7、Task 8（stats+acceptance）、Task 8（editor）覆盖。无缺口。
2. **占位符扫描**：无 TBD/TODO；两处"实现时读既有文件校正"（STAGE_SPECS 的 validate 实参、invocation_store 聚合口径）是显式指定的事实来源与校正规则，非空白授权。
3. **类型一致性**：`StageResult`/`run_stage`/`LLMPort`/`RunStore`/`PipelineRunner.start/resume/retry`/`broker.publish/subscribe` 各任务间签名一致；STAGE_ORDER 与确认门位置（outline 后）全篇一致。
