# 失败免疫实施计划：A1 分级 v2 + A3 传输重试 + B 兜底落库 + A2 早停

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 内容类失败趋近零——叙事质量违规降 advisory（A1）、LLM 瞬断自动重试（A3）、blocking 轮耗尽落库带 ⛔ 标记继续跑（B）、连续无改善早停（A2）；环境性失败语义不变。

**Architecture:** 全部改动在执行器侧与传输层：STAGE_SPECS 白名单是纯数据；`llm.py` chat() 包重试循环；`run_stage` 把「迭代耗尽即失败」改为「最终权威门再裁决 → 过=正常通过 / 不过=落库带 blocking 标记返回 degraded 通过」，`_drive_inner` 与 stats 透传 degraded。零迁移、零新端点。

**Tech Stack:** 同主线（FastAPI/asyncpg/node 门脚本零触碰）。

**Spec:** `docs/superpowers/specs/2026-10-26-failure-immunity-design.md`（用户已批「A 全做 + B 批准」）

## Global Constraints

- `pipeline_skills/**` 零触碰（A1 只改执行器白名单，不碰 .mjs）；质量门阈值不改；确认门语义不动；agent 政策循环语义不动
- **环境性失败保持现状**：LLM 异常（重试后仍败）、执行器异常 → run failed、不落库、可重试（`_drive_inner` 的 infrastructure 分支原样）
- B 触发前置：`state["draft"]` 非空（无内容可兜 = 既有失败路径，落空产物是脏数据）；兜底必先跑一次最终权威门
- 事件契约只增不改：`stage_gate` 形状不变（兜底门多 `fallback: true`）；`stage_done` 在 degraded 时多 `degraded: true` + `blocking: N`；干净通过的 `{"type": "done", "ok": True}` 逐字节不变；stats `stages[stage]` 多 `degraded` 键
- 测试惯例：asyncio.run + 真 Postgres（`_run_scenario` 模式）、ScriptedLLM、monkeypatch `core.skill_executor._run_gate` 锁分级逻辑、真 node 门测试不碰。基线 **303 passed**
- Commits：lowercase conventional prefix，每任务一个提交

---

### Task 1: A1 —— advisory 白名单 v2（outline 11 条 / script 7 条）

**Files:**
- Modify: `core/skill_executor.py`（STAGE_SPECS outline `advisory_rules` ~:76、script ~:111 及各自注释）
- Modify: `tests/pipeline/test_gate_sentinel.py`（新增常量 + 断言）
- Modify: `tests/pipeline/test_skill_executor.py`（`test_stage_specs_advisory_whitelist_v1` ~:948 重命名 v2 并断言新集合）

**Interfaces:**
- Produces: `STAGE_SPECS["outline"]["advisory_rules"] == frozenset({"risk-flag","lead-cap","support-cap","functional-cap","prop-cap","scene-cap","beat-gap","ep1-hook","major-early","crowd-plan","no-dialogue"})`；`STAGE_SPECS["script"]["advisory_rules"] == frozenset({"line-length","speaker","beats-claimed","hook-cliff","hook-open","has-action","action-prose"})`。characters/art/staging/storyboard 不动
- 既有测试影响：monkeypatch 分级测试若以 v2-advisory id（scene-cap 等）当 blocking 夹具，改用 `ep-fields` / `refs`（v2 仍 blocking 的 outline id）

- [ ] **Step 1: 改锁测试（先红）**——test_gate_sentinel.py 在 `CHARACTERS_IDS` 旁新增：

```python
OUTLINE_ADVISORY_IDS = frozenset({
    "risk-flag", "lead-cap", "support-cap", "functional-cap", "prop-cap",
    "scene-cap", "beat-gap", "ep1-hook", "major-early", "crowd-plan", "no-dialogue",
})
SCRIPT_ADVISORY_IDS = frozenset({
    "line-length", "speaker", "beats-claimed",
    "hook-cliff", "hook-open", "has-action", "action-prose",
})

def test_advisory_whitelist_v2_subsets():
    """v2 白名单必须是全量 id 集合的子集（防拼写漂移），余集 = 机械结构类。"""
    assert OUTLINE_ADVISORY_IDS <= OUTLINE_IDS
    assert SCRIPT_ADVISORY_IDS <= SCRIPT_IDS
    assert OUTLINE_IDS - OUTLINE_ADVISORY_IDS == {"ep-fields", "once-scene", "refs"}
    assert SCRIPT_IDS - SCRIPT_ADVISORY_IDS == {"duration", "refs-characters", "refs-scenes"}
```

  test_skill_executor.py 的 `test_stage_specs_advisory_whitelist_v1` 改名 `_v2`：断言 `STAGE_SPECS["outline"]["advisory_rules"] == test_gate_sentinel.OUTLINE_ADVISORY_IDS`、script 同理；characters 断言保留
- [ ] **Step 2: 跑失败**——`python -m pytest tests/pipeline/test_gate_sentinel.py tests/pipeline/test_skill_executor.py -k advisory -q` FAIL（STAGE_SPECS 还是 v1）
- [ ] **Step 3: 实现**——STAGE_SPECS 两条 `advisory_rules` 换成上式，注释从「v1」改「v2（failure immunity A1）：叙事质量/数量体量类不拦，机械结构类（ep-fields/once-scene/refs；duration/refs-*）照旧 blocking，耗尽由 B 兜底」
- [ ] **Step 4: 全套测试**——`python -m pytest tests/ -q` 全绿；若 monkeypatch 分级测试夹具受 v2 影响，改用 blocking id 后重跑
- [ ] **Step 5: 提交**

```bash
git add core/skill_executor.py tests/pipeline/test_gate_sentinel.py tests/pipeline/test_skill_executor.py
git commit -m "feat: advisory whitelist v2 — narrative-quality outline/script rules no longer block"
```

---

### Task 2: A3 —— LLM 传输层退避重试

**Files:**
- Modify: `core/llm.py`（`chat()` ~:106-181；顶部 import 与模块级常量）
- Test: `tests/pipeline/test_llm_retry.py`（新建，纯单元、无 DB、无真 client）

**Interfaces:**
- Produces: 模块级 `TRANSIENT_ERRORS = (APIConnectionError, RateLimitError, InternalServerError)`、`_RETRY_ATTEMPTS = 3`、`_RETRY_BASE_DELAY = 2.0`；`chat()` 签名与返回类型零变化；语义 = 整次请求（create+消费）至多 3 次尝试，退避 `2.0 * 2**(attempt-1) + random.uniform(0, 0.5)` 秒；**已经向外吐过任何增量（on_text/on_reasoning 已回调或已攒到 text/tool 片段）则不重试直接抛**；非瞬态（AuthenticationError 等 4xx）首发即抛

- [ ] **Step 1: 失败测试**——`tests/pipeline/test_llm_retry.py` 全文：

```python
"""A3：传输层瞬态错误退避重试。纯单元：LLM.__new__ 绕过 __init__，注假 client。"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from openai import (APIConnectionError, AuthenticationError, InternalServerError,
                    RateLimitError)

from core.llm import LLM


def _http_err(exc_cls, status: int):
    return exc_cls(f"http-{status}",
                   response=httpx.Response(status, request=httpx.Request("POST", "http://t")),
                   body=None)


def _chunk(*, content=None):
    """chunk 形状对齐 openai 流式增量；content=None 的空 chunk 会被消费体跳过。"""
    if content is None:
        return SimpleNamespace(usage=None, choices=[])
    delta = SimpleNamespace(content=content, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(usage=None,
                           choices=[SimpleNamespace(finish_reason=None, delta=delta)])


class FlakyClient:
    """create 前 n_fail 次抛 exc，之后按 payload 逐个吐 chunk。"""

    def __init__(self, exc, n_fail, payload=None):
        self.calls = 0
        self.exc = exc
        self.n_fail = n_fail
        self.payload = payload or [_chunk()]

    @property
    def chat(self):
        return SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls += 1
        if self.calls <= self.n_fail:
            raise self.exc

        async def _stream():
            for item in self.payload:
                yield item
        return _stream()


def _llm(client) -> LLM:
    llm = LLM.__new__(LLM)
    llm.client = client
    llm.model = "test-model"
    llm.settings = SimpleNamespace(max_output_tokens=0)
    return llm


def _zero_sleep(_delay):
    return asyncio.sleep(0)


@pytest.mark.parametrize("exc", [
    APIConnectionError("conn"),
    _http_err(RateLimitError, 429),
    _http_err(InternalServerError, 500),
])
def test_transient_errors_retry_then_succeed(exc, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _zero_sleep)
    client = FlakyClient(exc, 2)
    msg = asyncio.run(_llm(client).chat([{"role": "user", "content": "hi"}]))
    assert client.calls == 3


def test_auth_error_no_retry(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _zero_sleep)
    client = FlakyClient(_http_err(AuthenticationError, 401), 5)
    with pytest.raises(AuthenticationError):
        asyncio.run(_llm(client).chat([{"role": "user", "content": "hi"}]))
    assert client.calls == 1


def test_no_retry_after_partial_stream(monkeypatch):
    """已经 on_text 吐过字：断流不重试（重试会向用户重复输出）。"""
    monkeypatch.setattr(asyncio, "sleep", _zero_sleep)

    class MidStreamDrop(FlakyClient):
        def __init__(self):
            super().__init__(APIConnectionError("x"), 0)

        async def _create(self, **kwargs):
            self.calls += 1

            async def _stream():
                yield _chunk(content="你")
                raise APIConnectionError("mid-stream")
            return _stream()

    client = MidStreamDrop()
    seen: list[str] = []
    with pytest.raises(APIConnectionError):
        asyncio.run(_llm(client).chat([{"role": "user", "content": "hi"}], on_text=seen.append))
    assert seen == ["你"] and client.calls == 1
```

- [ ] **Step 2: 跑失败**——`python -m pytest tests/pipeline/test_llm_retry.py -q` FAIL（chat 无重试，首次即抛）
- [ ] **Step 3: 实现**——llm.py 顶部 `import asyncio, random`、`from openai import APIConnectionError, InternalServerError, RateLimitError`；模块级三常量；`chat()` 里把「create + async for 消费 + return AssistantMessage」整段包进：

```python
        last_exc: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_RETRY_BASE_DELAY * 2 ** (attempt - 1) + random.uniform(0, 0.5))
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            calls: dict[int, dict[str, str]] = {}
            usage = Usage()
            finish_reason: str | None = None
            emitted = False
            try:
                stream = await self.client.chat.completions.create(**kwargs)
                async for chunk in stream:
                    ...  # 既有消费体逐字搬入；首个 text/reasoning/tool_call 片段处置 emitted = True
                return AssistantMessage(
                    content="".join(text_parts),
                    reasoning="".join(reasoning_parts),
                    tool_calls=[ToolCall(id=v["id"], name=v["name"], arguments=v["args"])
                                for _, v in sorted(calls.items())],
                    usage=usage,
                    finish_reason=finish_reason,
                )
            except TRANSIENT_ERRORS as exc:
                if emitted:
                    raise          # 半途断流不重试：用户已看到一半，重试会重复输出
                last_exc = exc     # 尚无任何对外输出：整次重来
        raise last_exc  # type: ignore[misc]
```

  4xx 非瞬态异常不被 `except TRANSIENT_ERRORS` 捕获，自然首发即抛；`simple()` 走 `chat()` 自动获得重试
- [ ] **Step 4: 全套测试**——`python -m pytest tests/ -q` 全绿
- [ ] **Step 5: 提交**

```bash
git add core/llm.py tests/pipeline/test_llm_retry.py
git commit -m "feat: llm transport retry with backoff for transient errors"
```

---

### Task 3: B —— 兜底落库（耗尽不再判死）

**Files:**
- Modify: `core/skill_executor.py`（StageResult ~:228、run_stage 循环尾 ~:734-811）
- Modify: `core/pipeline_run.py`（`_drive_inner` 成功分支 ~:474-479）
- Modify: `server/pipeline_runs.py`（`aggregate_run_stats` ~:292-311）
- Test: `tests/pipeline/test_skill_executor.py`（新增 2 个）

**Interfaces:**
- Produces: `StageResult` 新末位字段 `degraded: bool = False`（既有构造点不传 = False）；模块级 `async def _pass_and_save(spec, state, db, project_id, gate_findings) -> tuple[dict, list[dict]]`（= 循环内通过路径的 `_finalize_draft`→save_merge→`pipeline_repository.save(findings=...)` 整段提取，返回 (content, 全量 findings)；循环内与兜底共用）；兜底 `stage_done` 事件 `{"type": "done", "ok": True, "degraded": True, "blocking": N}`；`stage_status[stage]` 新键 `degraded: true`；stats `stages[stage].degraded`
- 兜底语义：循环耗尽到达循环尾时——`state["draft"]` 为空 → 既有失败路径**逐字保留**；非空 → `box.run_gate()` 最 终权威门再裁决：过 = 正常通过（零标记、零 degraded）；不过 = 落库，blocking 标记 `{"gate": "validate", "rule": <id>, "severity": "blocking", "detail": ...}`，rules=None 时单条 `rule: "gate-unavailable"`（detail = 门输出尾部）；StageResult(ok=True, degraded=True, draft=None)

- [ ] **Step 1: 失败测试**——test_skill_executor.py 新增（monkeypatch 模式照抄既有 advisory 分级测试）：

```python
def _gate_stub(sequence):
    """monkeypatch core.skill_executor._run_gate：按调用次序弹出 (ok, output, rules)，
    弹尽后停在最后一项（兜底门与轮内门共用同一桩）。"""
    calls = {"n": 0}
    async def _fake(spec, workdir):
        item = sequence[min(calls["n"], len(sequence) - 1)]
        calls["n"] += 1
        return item
    return _fake, calls


def test_blocking_exhaustion_saves_degraded(pipeline_test_database, monkeypatch):
    good_outline = json.loads(FIXTURE_OUTLINE.read_text(encoding="utf-8"))
    rules = [{"id": "refs", "ok": False, "detail": "引用了不存在的场景"},
             {"id": "ep-fields", "ok": False, "detail": "第 2 集缺 goal"}]
    stub, calls = _gate_stub([(False, "✗ 2 项未过", rules)])
    monkeypatch.setattr(skill_executor, "_run_gate", stub)

    async def scenario(database) -> None:
        # 写一轮全量草稿后每轮 finish → 权威门每轮挂 → 耗尽（或后续任务的早停）→
        # 兜底门对当前草稿再裁一次仍挂 → degraded 落库。不锁轮数/门数：
        # 本测试钉的是「耗尽结局 = degraded 落库」，轮数预算由 Task 4 的测试专管。
        turns = [item for _ in range(6) for item in (_write_rounds(good_outline, [9])[0],
                                                     [{"name": "finish", "args": {}}])]
        llm = ScriptedLLM(turns)
        events: list[dict] = []
        result = await run_stage(database, "proj-deg", "outline", {"novel_text": NOVEL_TEXT},
                                 llm=llm, on_event=events.append)
        assert result.ok and result.degraded and result.draft is None
        assert [f["rule"] for f in result.findings if f.get("severity") == "blocking"] \
            == ["refs", "ep-fields"]
        done = [e for e in events if e["type"] == "done"][-1]
        assert done == {"type": "done", "ok": True, "degraded": True, "blocking": 2}
        saved = await pipeline_repository.get(database, "proj-deg", "outline")
        assert saved["content"]["episodes"] == good_outline["episodes"]
        assert {f["rule"] for f in saved["findings"] if f["severity"] == "blocking"} \
            == {"refs", "ep-fields"}
        assert saved["source_run_id"] == "stage:outline"

    _run_scenario(scenario, pipeline_test_database)


def test_gate_unavailable_falls_back_with_marker(pipeline_test_database, monkeypatch):
    good_outline = json.loads(FIXTURE_OUTLINE.read_text(encoding="utf-8"))
    stub, _ = _gate_stub([(False, "质量门超时（>120s），进程已终止", None)])
    monkeypatch.setattr(skill_executor, "_run_gate", stub)

    async def scenario(database) -> None:
        turns = [item for _ in range(6) for item in (_write_rounds(good_outline, [9])[0],
                                                     [{"name": "finish", "args": {}}])]
        llm = ScriptedLLM(turns)
        result = await run_stage(database, "proj-gu", "outline", {"novel_text": NOVEL_TEXT}, llm=llm)
        assert result.ok and result.degraded
        assert result.findings[0]["rule"] == "gate-unavailable"
        assert result.findings[0]["severity"] == "blocking" and result.findings[0]["detail"]

    _run_scenario(scenario, pipeline_test_database)
```

  **守卫回归（不改断言、只跑）**：既有真门测试 `test_gate_failure_exhausts_iterations_without_saving`（只自检不写稿→草稿空）与 `test_exhaustion_without_finish_still_reports_findings`（三轮空转）必须在本任务实现前后都 PASS——它们就是「空草稿不兜底」守卫的回归证明
- [ ] **Step 2: 跑失败**——两个新测试 FAIL（现在耗尽返回 ok=False）；两个既有耗尽测试 PASS
- [ ] **Step 3: 实现**——skill_executor.py：
  1. `StageResult` 加末位字段 `degraded: bool = False`
  2. 循环内通过路径（:765-799 的 `_finalize_draft`→save_merge→save→返回段）提取为模块级：

```python
async def _pass_and_save(spec, state, db, project_id, stage, gate_findings):
    """组装 + save_merge 整形 + 落库。返回 (content, 全量 findings)。
    循环内干净通过 / advisory 过门与循环尾兜底共用；事件与 StageResult 由调用方发。"""
    content = _finalize_draft(spec, state)
    import_findings: list[dict] = []
    if spec.get("save_merge"):
        existing = await pipeline_repository.get(db, project_id, spec["artifact"])
        content, warnings = pipeline_import.prepare(
            pipeline_artifacts.get(spec["artifact"]),
            content, existing["content"] if existing else None,
        )
        import_findings = [{"gate": "import", "detail": w} for w in warnings]
    await pipeline_repository.save(db, project_id, spec["artifact"], content,
                                   source_run_id=f"stage:{stage}",
                                   findings=gate_findings + import_findings)
    return content, gate_findings + import_findings
```

  （循环内原调用点同步改为 `_pass_and_save(spec, state, db, project_id, stage, gate_findings)`，`source_run_id` 口径 `f"stage:{stage}"` 不变）
  3. 循环尾（:801-811）改为：

```python
    # 兜底（failure immunity B）：轮耗尽不判死——最终权威门对**当前草稿**再裁决。
    if not state["draft"]:
        # 既有失败路径逐字保留：无内容可兜，落空产物 = 脏数据
        if not findings:
            last = state["last_gate"]
            findings = [{"gate": "validate",
                         "detail": last["output"][-FINDING_TAIL:] if last
                         else f"{rnd} 轮内未 finish，无质量门结果"}]
        on_event and on_event({"type": "done", "ok": False})
        return StageResult(False, spec["artifact"], rnd, gate_log, findings,
                           draft=state["draft"])
    ok_f, output_f, rules_f = await box.run_gate()
    gate_log.append(output_f[-GATE_TAIL:])
    blocking_f, advisory_f = _grade_gate(rules_f, spec)
    on_event and on_event({"type": "gate", "ok": ok_f, "round": rnd,
                           "advisory": 0 if ok_f else len(advisory_f), "fallback": True})
    if ok_f:
        _content, all_findings = await _pass_and_save(spec, state, db, project_id, stage, [])
        on_event and on_event({"type": "done", "ok": True})
        return StageResult(True, spec["artifact"], rnd, gate_log, all_findings, draft=None)
    if rules_f:
        gate_findings = [{"gate": "validate", "rule": r["id"], "severity": "blocking",
                          "detail": r["detail"]} for r in rules_f if not r["ok"]]
        blocking_n = len(gate_findings)
    else:
        gate_findings = [{"gate": "validate", "rule": "gate-unavailable", "severity": "blocking",
                          "detail": output_f[-FINDING_TAIL:]}]
        blocking_n = 1
    _content, all_findings = await _pass_and_save(spec, state, db, project_id, stage, gate_findings)
    on_event and on_event({"type": "done", "ok": True, "degraded": True, "blocking": blocking_n})
    return StageResult(True, spec["artifact"], rnd, gate_log, all_findings,
                       draft=None, degraded=True)
```

  4. pipeline_run.py `_drive_inner` 成功分支：

```python
            stage_patch = {
                "status": "done" if result.ok else "failed",
                "attempts": result.iterations,
                "gate_runs": len(result.gate_log),
                "findings": list(result.findings),
            }
            if getattr(result, "degraded", False):
                stage_patch["degraded"] = True
            await self.store.update_stage(run_id, stage, stage_patch)
```

  5. server/pipeline_runs.py `aggregate_run_stats` 的 `stages[stage]` 增 `"degraded": bool(entry.get("degraded"))`
- [ ] **Step 4: 全套测试**——`python -m pytest tests/ -q` 全绿（≥ 305）；既有干净路径断言 `events[-1] == {"type": "done", "ok": True}` 必须原样通过
- [ ] **Step 5: 提交**

```bash
git add core/skill_executor.py core/pipeline_run.py server/pipeline_runs.py tests/pipeline/
git commit -m "feat: best-effort fallback — exhausted blocking stages save with markers, run continues"
```

---

### Task 4: A2 —— 早停（连续无缩小转兜底）

**Files:**
- Modify: `core/skill_executor.py`（run_stage 失败分支 ~:745-763；模块级常量 `STALL_LIMIT = 2`）
- Test: `tests/pipeline/test_skill_executor.py`（新增 1 个）

**Interfaces:**
- Consumes: Task 3 的兜底循环尾（早停 = `break` 落进同一兜底，事件带 `fallback: true`）
- Produces: run_stage 内局部 `prev_blocking: frozenset[str] | None = None`、`stall = 0`；语义 = blocking failed id 集 `ids >= prev_blocking`（超集或相等 = 无缩小）连续 `STALL_LIMIT` 次转移 → `break`；集合缩小或不可比 → 计数清零；rules=None 的全 blocking 退化失败不计入（无 id 可比，计数冻结）

- [ ] **Step 1: 失败测试**

```python
def test_stall_early_stop_routes_to_fallback(pipeline_test_database, monkeypatch):
    good_outline = json.loads(FIXTURE_OUTLINE.read_text(encoding="utf-8"))
    rules = [{"id": "refs", "ok": False, "detail": "引用了不存在的场景"}]
    stub, calls = _gate_stub([(False, "✗ 1 项未过", rules)])
    monkeypatch.setattr(skill_executor, "_run_gate", stub)

    async def scenario(database) -> None:
        # r1 写全量草稿（草稿非空是兜底前置）；r2/r3/r4 finish → 门挂，同一集合
        # 连续 3 轮无缩小（stall 达 STALL_LIMIT=2）→ 早停，5 轮预算只烧 4 轮；
        # 兜底门 = 第 4 次门调用，裁决仍挂 → degraded 落库。
        llm = ScriptedLLM(_write_rounds(good_outline, [9])
                          + [[{"name": "finish", "args": {}}]] * 4)
        events: list[dict] = []
        result = await run_stage(database, "proj-stall", "outline", {"novel_text": NOVEL_TEXT},
                                 llm=llm, on_event=events.append)
        assert len(llm.calls) == 4                       # 第 5 轮没跑
        assert calls["n"] == 4                           # 3 次轮内门 + 1 次兜底门
        assert result.ok and result.degraded
        gate_events = [e for e in events if e["type"] == "gate"]
        assert len(gate_events) == 4 and gate_events[-1]["fallback"] is True

    _run_scenario(scenario, pipeline_test_database)
```

- [ ] **Step 2: 跑失败**——`len(llm.calls) == 5`（无早停，烧满预算后走兜底）
- [ ] **Step 3: 实现**——run_stage 循环前初始化 `prev_blocking`/`stall`；失败分支头部插入：

```python
        if not ok and (blocking_failed or not advisory_failed):
            if blocking_failed:
                ids = frozenset(r["id"] for r in blocking_failed)
                stall = stall + 1 if (prev_blocking is not None and ids >= prev_blocking) else 0
                prev_blocking = ids
                if stall >= STALL_LIMIT:
                    break   # 早停（failure immunity A2）：连续无缩小，剩余预算交给兜底门
            ...  # 既有 findings / 修复消息逻辑原样
            continue
```

- [ ] **Step 4: 全套测试**——`python -m pytest tests/ -q` 全绿（≥ 306）；Task 3 两个测试无轮数断言、自动适应早停（核实仍绿即可）
- [ ] **Step 5: 提交**

```bash
git add core/skill_executor.py tests/pipeline/test_skill_executor.py
git commit -m "feat: stall early-stop — three rounds without shrinking blocking set routes to fallback"
```

---

### Task 5: 文档 + 收尾

**Files:**
- Modify: `docs/pipeline/phase2-acceptance.md`（队列新增「失败免疫」小节）

- [ ] **Step 1:** 记录：四件套落点与提交号、事件/Stats 契约增补（stage_done degraded/blocking、gate fallback、stats degraded）、红线修订引用（spec §4 第二次修订）、环境性失败边界、早停阈值 STALL_LIMIT=2、Deferred（标记向下游提示词透传；editor run_gate_only 全 blocking 旧语义不变）
- [ ] **Step 2: 提交**

```bash
git add docs/pipeline/phase2-acceptance.md
git commit -m "docs: failure immunity — v2 tiers, transport retry, degraded fallback, stall early-stop"
```

- [ ] **Step 3（controller 执行）:** 全套 pytest ≥ 306；Desktop 合并后重启后端（核验 mtime < 进程启动）；真机冒烟一次性项目确认干净路径零回归（大纲确认门照常停、事件流无 degraded 键）；degraded 路径真机验收随对话流计划（`2026-10-26-pipeline-chat-feed.md`）的 CDP 关卡一并做

---

## 验收清单

- [ ] outline/script advisory 白名单 = v2 集合，三方锁测试绿；characters/art/staging/storyboard 白名单零变化
- [ ] 瞬态 LLM 错误 3 次尝试退避重试；非瞬态首发即抛；半途断流不重试
- [ ] blocking 耗尽 → 最终权威门裁决 → degraded 落库带 ⛔ 标记、run 继续；空草稿 → 既有失败原样（两个真门耗尽测试保绿）；门不可用 → gate-unavailable 标记
- [ ] 干净通过路径零变化（既有全部测试原样绿，`{"type": "done", "ok": True}` 逐字节不变）
- [ ] 环境性失败（infrastructure 分支）原样
