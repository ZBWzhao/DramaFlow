"""改稿脑测试（Task 8）：提修改 → 审批 → 自动体检，写入必过审批门。

三层"真"夹一层"假"（照 test_skill_executor 惯例）：
- DB 是真库（pipeline_test，session 级夹具 + 每用例独立 pool + TRUNCATE）；
- 体检是真门——apply 对 script 落稿后由 run_gate_only 起真 node 子进程全量重查，
  门不过真回滚；
- 只有 LLM 是假的：ScriptedLLM 按轮次吐工具调用（复用 test_skill_executor 的替身）。

红线断言：
- agent 工具环没有 apply —— EDITOR_TOOLS 三个工具，dispatch 对未知工具打回；
- 未 apply 前产物一字节不变；apply 后产物更新且 source_run_id="editor"；
- apply 门不过 → 回滚重写旧 content（provenance 保留），无脏数据；
- pending 10 分钟 TTL；驳回/过期后 apply 一律 KeyError。
"""
from __future__ import annotations

import asyncio
import functools
import json

import asyncpg

from core import pipeline_editor, pipeline_repository
from core.pipeline_editor import EDITOR_TOOLS, PipelineEditor
from core.postgres import PostgresDatabase
from test_pipeline_repository import TEST_DSN, pipeline_test_database  # noqa: F401  session 级库夹具
from test_skill_executor import ScriptedLLM, _load, _trim_script  # noqa: F401  复用替身与夹具

OUTLINE = _load("outline.json")
SCRIPT = _load("script.json")


class _EndpointLLM:
    """registry 只在个别端点读 .model；本文件的会话链路不触。"""

    model = "fake-model"

    async def simple(self, messages):
        return "{}"


def _run_scenario(scenario, dsn: str, *, truncate_runs: bool = False) -> None:
    """同一 asyncio.run 里装配 pool + TRUNCATE + 跑用例 + 关池（照抄仓库测试）。"""

    async def _runner() -> None:
        database = PostgresDatabase(dsn, min_size=1, max_size=2)
        database.pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=2, command_timeout=30
        )
        try:
            await database.pool.execute("TRUNCATE pipeline_artifacts")
            if truncate_runs:
                await database.pool.execute("TRUNCATE pipeline_runs")
            await scenario(database)
        finally:
            await database.pool.close()

    asyncio.run(_runner())


def _propose_turns(new_content: dict, summary: str = "补齐第 2、3 集") -> list[list[dict]]:
    return [
        [{"name": "read_artifact", "args": {"type": "script"}},
         {"name": "propose_edit", "args": {"type": "script", "summary": summary,
                                           "new_content": new_content}}],
        [{"name": "finish", "args": {}}],
    ]


# ---------------------------------------------------------------------------
# 红线：工具环没有 apply
# ---------------------------------------------------------------------------

def test_editor_tool_ring_has_no_apply():
    """工具清单逐字 = brief 的 EDITOR_TOOLS；spec 只含这三个；apply 不是工具。"""
    assert EDITOR_TOOLS == ("read_artifact", "propose_edit", "finish")
    spec_names = [entry["function"]["name"] for entry in pipeline_editor._tool_specs()]
    assert spec_names == list(EDITOR_TOOLS)

    async def scenario(database) -> None:
        editor = PipelineEditor(database, llm=ScriptedLLM([]))
        ok, out = await editor._dispatch("proj-x", "apply", {"edit_id": "edit_1"})
        assert not ok and "未知工具" in out
        ok, out = await editor._dispatch("proj-x", "write_draft", {"section": "source", "data": "x"})
        assert not ok and "未知工具" in out

    _run_scenario(scenario, TEST_DSN)


def test_propose_rejects_malformed_args(database=None):
    async def scenario(db) -> None:
        editor = PipelineEditor(db, llm=ScriptedLLM([]))
        # 未知产物类型
        ok, out = await editor._dispatch("proj-bad", "propose_edit",
                                         {"type": "novel", "summary": "s", "new_content": {}})
        assert not ok and "未知产物类型" in out
        # 空 summary
        ok, out = await editor._dispatch("proj-bad", "propose_edit",
                                         {"type": "script", "summary": " ", "new_content": {}})
        assert not ok and "summary" in out
        # new_content 双重编码成 JSON 字符串（真模型偶发，静默收下会让门报难懂的错）
        ok, out = await editor._dispatch("proj-bad", "propose_edit",
                                         {"type": "script", "summary": "s",
                                          "new_content": json.dumps(SCRIPT)})
        assert not ok and "不要把 JSON 再编码成字符串" in out
        assert pipeline_editor.list_edits("proj-bad") == []

    _run_scenario(scenario, TEST_DSN)


# ---------------------------------------------------------------------------
# 提案 → 审批：未 apply 前产物不变；apply 落稿 + 体检 + provenance
# ---------------------------------------------------------------------------

def test_propose_pends_without_touching_artifact_then_apply_passes_gate(pipeline_test_database):
    async def scenario(database) -> None:
        project = "proj-editor-apply"
        await pipeline_repository.save(database, project, "outline", OUTLINE,
                                       source_run_id="stage:outline")
        baseline = _trim_script(SCRIPT, [1])
        await pipeline_repository.save(database, project, "script", baseline,
                                       source_run_id="stage:script")

        editor = PipelineEditor(database, llm=ScriptedLLM(_propose_turns(SCRIPT)))
        result = await editor.chat(project, "sess-1", "把第 2、3 集补齐")
        # propose 的工具回执 = brief 逐字口径
        propose_ends = [e for e in result["events"] if e["type"] == "tool_end"
                        and e["name"] == "propose_edit"]
        assert propose_ends and "已提案，等待用户审批" in propose_ends[0]["result_tail"]
        assert "edit_id=" in propose_ends[0]["result_tail"]
        # 系统提示声明只读 + 提案边界
        first = editor.llm.calls[0]
        assert first[0]["role"] == "system" and "propose_edit" in first[0]["content"]
        assert "不能直接修改任何产物" in first[0]["content"]
        # 会话记忆留住本轮（下一轮对话有上下文）
        assert any(m.get("role") == "user" for m in editor._sessions["sess-1"])

        # pending 恰好一条；产物一字节未变
        edits = pipeline_editor.list_edits(project)
        assert len(edits) == 1 and edits[0]["status"] == "pending"
        assert edits[0]["artifact_type"] == "script"
        row = await pipeline_repository.get(database, project, "script")
        assert row["content"] == baseline
        assert row["source_run_id"] == "stage:script"

        # 审批落稿：自动体检（真门）通过 → content 更新、provenance=editor
        outcome = await pipeline_editor.apply_pending_edit(database, project, edits[0]["edit_id"])
        assert outcome["ok"] is True and outcome["rolled_back"] is False
        assert outcome["gate"] == {"ok": True, "report_tail": outcome["gate"]["report_tail"]}
        assert outcome["gate"]["report_tail"]
        saved = await pipeline_repository.get(database, project, "script")
        assert [e["ep"] for e in saved["content"]["episodes"]] == [1, 2, 3]
        assert saved["content"]["episodes"] == SCRIPT["episodes"]   # 合并后与全量稿一致
        assert saved["source_run_id"] == "editor"
        # 提案账本翻到 applied；再 apply 同一条 → KeyError（不可重复落稿）
        assert pipeline_editor.list_edits(project)[0]["status"] == "applied"
        try:
            await pipeline_editor.apply_pending_edit(database, project, edits[0]["edit_id"])
            raise AssertionError("重复 apply 必须 KeyError")
        except KeyError:
            pass

    _run_scenario(scenario, TEST_DSN)


def test_apply_gate_failure_rolls_back_to_baseline(pipeline_test_database):
    """自动体检：门不过 → 重写旧 content（保留旧 provenance），返回 findings，无脏数据。"""
    async def scenario(database) -> None:
        project = "proj-editor-rollback"
        await pipeline_repository.save(database, project, "outline", OUTLINE,
                                       source_run_id="stage:outline")
        baseline = _trim_script(SCRIPT, [1])
        await pipeline_repository.save(database, project, "script", baseline,
                                       source_run_id="stage:script")

        # 病灶：第 1 集出场人物混进大纲没有的 C99 —— refs-characters 门必拦
        first = SCRIPT["episodes"][0]
        bogus = {**first,
                 "scenes": [{**first["scenes"][0], "characters": [*first["scenes"][0]["characters"], "C99"]},
                            *first["scenes"][1:]]}
        bad_content = {**SCRIPT, "episodes": [bogus]}

        editor = PipelineEditor(database, llm=ScriptedLLM(_propose_turns(bad_content, "混入 C99")))
        await editor.chat(project, "sess-1", "把 C99 加进第一集")
        edit_id = pipeline_editor.list_edits(project)[0]["edit_id"]

        outcome = await pipeline_editor.apply_pending_edit(database, project, edit_id)
        assert outcome["ok"] is False and outcome["rolled_back"] is True
        assert outcome["gate"]["ok"] is False
        assert outcome["findings"] and "C99" in outcome["findings"][0]["detail"]
        # 库里是回滚后的基线（逐字段 + provenance 原样），没有半成品
        row = await pipeline_repository.get(database, project, "script")
        assert row["content"] == baseline
        assert row["source_run_id"] == "stage:script"
        assert pipeline_editor.list_edits(project)[0]["status"] == "failed"

    _run_scenario(scenario, TEST_DSN)


def test_apply_malformed_script_payload_is_rejected_without_writing(pipeline_test_database):
    async def scenario(database) -> None:
        project = "proj-editor-422"
        baseline = _trim_script(SCRIPT, [1])
        await pipeline_repository.save(database, project, "script", baseline,
                                       source_run_id="stage:script")
        editor = PipelineEditor(database, llm=ScriptedLLM(
            _propose_turns({"source": SCRIPT["source"]}, "忘了集数组")))
        await editor.chat(project, "sess-1", "改一下剧本")
        edit_id = pipeline_editor.list_edits(project)[0]["edit_id"]
        outcome = await pipeline_editor.apply_pending_edit(database, project, edit_id)
        assert outcome["ok"] is False and outcome["rolled_back"] is False
        assert outcome["findings"]
        row = await pipeline_repository.get(database, project, "script")
        assert row["content"] == baseline
        assert pipeline_editor.list_edits(project)[0]["status"] == "failed"

    _run_scenario(scenario, TEST_DSN)


def test_apply_without_upstream_outline_fails_and_rolls_back_new_row(pipeline_test_database):
    """基线为空 + 门不过（缺 outline.json）→ 删行回滚：库里连半行都不留。"""
    async def scenario(database) -> None:
        project = "proj-editor-nobaseline"
        editor = PipelineEditor(database, llm=ScriptedLLM(_propose_turns(SCRIPT)))
        await editor.chat(project, "sess-1", "全量写剧本")
        edit_id = pipeline_editor.list_edits(project)[0]["edit_id"]
        outcome = await pipeline_editor.apply_pending_edit(database, project, edit_id)
        assert outcome["ok"] is False and outcome["rolled_back"] is True
        assert await pipeline_repository.get(database, project, "script") is None

    _run_scenario(scenario, TEST_DSN)


# ---------------------------------------------------------------------------
# 提案账本：TTL / 驳回
# ---------------------------------------------------------------------------

def test_pending_ttl_expiry_and_reject(pipeline_test_database):
    async def scenario_ttl(database) -> None:
        project = "proj-editor-ttl"
        await pipeline_repository.save(database, project, "script", _trim_script(SCRIPT, [1]),
                                       source_run_id="stage:script")
        editor = PipelineEditor(database, llm=ScriptedLLM(_propose_turns(SCRIPT)))
        await editor.chat(project, "sess-1", "改剧本")
        book = pipeline_editor._PENDING[project]
        edit_id = next(iter(book))
        # 10 分钟 TTL：把 proposed_at 拨回 TTL+1s 前 → 列表里是 expired，apply 404 语义
        book[edit_id]["proposed_at"] -= pipeline_editor.EDIT_TTL_SECONDS + 1
        assert pipeline_editor.list_edits(project)[0]["status"] == "expired"
        try:
            await pipeline_editor.apply_pending_edit(database, project, edit_id)
            raise AssertionError("过期提案必须 KeyError")
        except KeyError:
            pass
        # 过期不落稿：产物原样
        row = await pipeline_repository.get(database, project, "script")
        assert row["content"] == _trim_script(SCRIPT, [1])

    async def scenario_reject(database) -> None:
        project = "proj-editor-reject"
        baseline = _trim_script(SCRIPT, [1])
        await pipeline_repository.save(database, project, "script", baseline,
                                       source_run_id="stage:script")
        editor = PipelineEditor(database, llm=ScriptedLLM(_propose_turns(SCRIPT)))
        await editor.chat(project, "sess-1", "改剧本")
        edit_id = pipeline_editor.list_edits(project)[0]["edit_id"]
        rejected = pipeline_editor.reject(project, edit_id)
        assert rejected["status"] == "rejected"
        try:
            await pipeline_editor.apply_pending_edit(database, project, edit_id)
            raise AssertionError("已驳回提案必须 KeyError")
        except KeyError:
            pass
        row = await pipeline_repository.get(database, project, "script")
        assert row["content"] == baseline

    _run_scenario(scenario_ttl, pipeline_test_database)
    _run_scenario(scenario_reject, pipeline_test_database)


# ---------------------------------------------------------------------------
# 对话环：纯文本回答 / 预算耗尽
# ---------------------------------------------------------------------------

def test_chat_plain_answer_and_history_carryover(pipeline_test_database):
    async def scenario(database) -> None:
        project = "proj-editor-chat"
        await pipeline_repository.save(database, project, "outline", OUTLINE,
                                       source_run_id="stage:outline")
        llm = ScriptedLLM([
            [],                                    # 第一轮：纯文本回答（无工具调用）
            [{"name": "read_artifact", "args": {"type": "outline"}}],
            [{"name": "finish", "args": {}}],
        ])
        editor = PipelineEditor(database, llm=llm)
        first = await editor.chat(project, "sess-h", "第三集的反转是什么？")
        assert first["text"] == ""            # 替身 text 为空；纯文本收尾
        assert first["events"] == [{"type": "assistant_text", "text": ""}]
        second = await editor.chat(project, "sess-h", "把大纲给我看看")
        # 第二轮的 messages 带上一轮的 user/assistant（会话记忆生效）
        roles = [m["role"] for m in llm.calls[2]]
        assert roles.count("user") >= 2 and "assistant" in roles
        read_ends = [e for e in second["events"] if e["type"] == "tool_end" and e["name"] == "read_artifact"]
        assert read_ends and read_ends[0]["ok"]
        # 模型收到的工具结果是全文不截断的产物 JSON（会话历史里的 tool 消息逐字节可解析）
        tool_msgs = [m for m in editor._sessions["sess-h"]
                     if m.get("role") == "tool" and m.get("name") == "read_artifact"]
        assert tool_msgs and json.loads(tool_msgs[-1]["content"]) == OUTLINE

    _run_scenario(scenario, TEST_DSN)


def test_chat_budget_exhausts_with_visible_note(pipeline_test_database):
    async def scenario(database) -> None:
        project = "proj-editor-budget"
        llm = ScriptedLLM([[{"name": "read_artifact", "args": {"type": "script"}}]
                           for _ in range(6)])
        editor = PipelineEditor(database, llm=llm, max_iterations=2)
        result = await editor.chat(project, "sess-b", "一直读下去")
        assert "预算已用完" in result["text"]
        assert len(llm.calls) == 2

    _run_scenario(scenario, TEST_DSN)


def test_history_stored_exactly_once_across_turns(pipeline_test_database):
    """回归（review Important）：会话历史必须整段赋值回写——extend 会把取出来的旧历史
    再 extend 回去，逐轮整段复制（第 3 轮后 q1/a1 出现 4 次）。断言：多轮之后每条
    user/assistant 消息在存储里恰好出现一次；本轮 user 消息必须已入历史（下一轮上下文）。"""
    async def scenario(database) -> None:
        from collections import Counter

        project = "proj-editor-nodup"
        await pipeline_repository.save(database, project, "outline", OUTLINE,
                                       source_run_id="stage:outline")
        llm = ScriptedLLM([
            [],                                                    # 轮 1：纯文本回答
            [{"name": "read_artifact", "args": {"type": "outline"}},
             {"name": "propose_edit", "args": {"type": "outline", "summary": "s",
                                               "new_content": OUTLINE}},
             {"name": "finish", "args": {}}],                      # 轮 2：带工具轮（同轮 finish）
            [],                                                    # 轮 3：纯文本回答
            [],                                                    # 轮 4：纯文本回答（验证第四问上下文）
        ])
        editor = PipelineEditor(database, llm=llm)
        questions = ["第一问", "第二问", "第三问"]
        for q in questions:
            await editor.chat(project, "sess-dup", q)

        history = editor._sessions["sess-dup"]
        user_texts = [m["content"] for m in history if m.get("role") == "user"]
        assert user_texts == questions                      # 三问各一条、按序、无复制
        # 逐轮恰好存 2 条（纯文本轮）/ 5 条（带工具轮：assistant+3 tool）：3 轮共 9 条。
        # 旧 extend 写法第 2 轮就会把第 1 轮历史整段复制（history 变 10 条、逐轮膨胀）。
        assert len(history) == 9
        assert sum(1 for m in history if m.get("role") == "tool") == 3
        # 有内容的消息逐条唯一（空 assistant 占位两条同形属正常，不参与身份判定）
        nonempty = Counter(json.dumps(m, ensure_ascii=False, sort_keys=True)
                           for m in history if m.get("content"))
        assert all(c == 1 for c in nonempty.values()), "历史里出现了重复消息"

        # 下一轮递给模型的 messages：system + 历史 + 新 user，旧历史只出现一次
        await editor.chat(project, "sess-dup", "第四问")
        fourth = llm.calls[3]
        assert fourth[0]["role"] == "system"
        fourth_users = [m["content"] for m in fourth if m.get("role") == "user"]
        assert fourth_users == [*questions, "第四问"]

    _run_scenario(scenario, TEST_DSN)


def test_history_cap_trims_from_the_front(pipeline_test_database):
    async def scenario(database) -> None:
        project = "proj-editor-cap"
        llm = ScriptedLLM([[] for _ in range(4)])
        editor = PipelineEditor(database, llm=llm, history_cap=5)
        for i in range(4):
            await editor.chat(project, "sess-cap", f"问{i}")
        history = editor._sessions["sess-cap"]
        assert len(history) == 5                     # 4 轮 × 2 条 = 8，裁到最近 5 条
        assert [m["content"] for m in history if m.get("role") == "user"] == ["问2", "问3"]

    _run_scenario(scenario, TEST_DSN)


# ---------------------------------------------------------------------------
# app.py 分支：mode="pipeline_editor" 的 NDJSON 聊天流
# ---------------------------------------------------------------------------

def test_agent_endpoint_pipeline_editor_mode(pipeline_test_database, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import server.app as app_module
    from core.canvas_repository import create_canvas_repository
    from core.postgres import PostgresDatabase
    from core.session_repository import JsonlSessionRepository
    from server.registry import SessionRegistry

    database = PostgresDatabase(pipeline_test_database, min_size=1, max_size=2)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    registry = SessionRegistry(
        llm=_EndpointLLM(),
        workspace=str(workspace),
        canvas_repository=create_canvas_repository(workspace),
        session_repository=JsonlSessionRepository(tmp_path / "sessions"),
    )
    monkeypatch.setattr(app_module, "WORKSPACE", workspace)
    monkeypatch.setattr(app_module, "registry", registry)
    from server import studio as studio_module

    studio_module.configure(registry=registry, workspace=workspace, media_registry=None)

    # 审批端点依赖：照 test_pipeline_runs_api 的注入口径（不依赖 import 顺序）
    from core.pipeline_run import EventBroker, PipelineRunner, RunStore
    from server import pipeline_runs as runs_module

    runs_store = RunStore(database)
    monkeypatch.setattr(runs_module, "_store", runs_store)
    monkeypatch.setattr(runs_module, "_runner",
                        PipelineRunner(runs_store, database, broker=EventBroker()))
    monkeypatch.setattr(runs_module, "_broker", EventBroker())

    monkeypatch.setattr(
        app_module, "_pipeline_editor",
        PipelineEditor(database, llm=ScriptedLLM(_propose_turns(SCRIPT))),
    )

    with TestClient(app_module.app) as client:
        pool = client.portal.call(functools.partial(
            asyncpg.create_pool, pipeline_test_database, min_size=1, max_size=2, command_timeout=30
        ))
        database.pool = pool
        try:
            client.portal.call(pool.execute, "TRUNCATE pipeline_artifacts")
            client.portal.call(functools.partial(
                pipeline_repository.save, database, "proj-api", "outline", OUTLINE, "stage:outline"))
            client.portal.call(functools.partial(
                pipeline_repository.save, database, "proj-api", "script",
                _trim_script(SCRIPT, [1]), "stage:script"))

            response = client.post("/api/agent", json={
                "prompt": "把第 2、3 集补齐", "canvas": "proj-api",
                "session_id": "sess-editor-api", "media_policy": "plan_only",
                "mode": "pipeline_editor",
            })
            assert response.status_code == 200, response.text
            assert response.headers["content-type"].startswith("application/x-ndjson")
            events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
            kinds = [e["type"] for e in events]
            assert kinds[0] == "session" and kinds[-1] == "done"
            assert "tool_execution_start" in kinds and "tool_execution_end" in kinds
            assert "message_update" in kinds
            propose_end = next(e for e in events if e["type"] == "tool_execution_end"
                               and e.get("result", "").startswith("已提案"))
            assert propose_end["isError"] is False

            # 提案落进 pending；HTTP 端点能看到
            listed = client.get("/api/pipeline/projects/proj-api/editor/pending").json()
            assert len(listed["edits"]) == 1 and listed["edits"][0]["status"] == "pending"

            # 未 apply：产物未变
            async def _row():
                return await pipeline_repository.get(database, "proj-api", "script")

            row = client.portal.call(_row)
            assert row["content"] == _trim_script(SCRIPT, [1])

            # 审批端点落稿（script 自动体检）
            edit_id = listed["edits"][0]["edit_id"]

            async def _apply():
                return await runs_module.apply_editor_edit(
                    "proj-api", runs_module.ApplyEditBody(edit_id=edit_id))

            outcome = client.portal.call(_apply)
            assert outcome["ok"] is True and outcome["gate"]["ok"] is True
            row = client.portal.call(_row)
            assert [e["ep"] for e in row["content"]["episodes"]] == [1, 2, 3]
            assert row["source_run_id"] == "editor"

            # 再 apply 同一条：404（提案已消费）
            gone = client.post("/api/pipeline/projects/proj-api/editor/apply",
                               json={"edit_id": edit_id})
            assert gone.status_code == 404
        finally:
            client.portal.call(pool.close)


def test_agent_endpoint_pipeline_editor_requires_db(tmp_path, monkeypatch):
    """file 后端（未装配编辑器）：mode=pipeline_editor → 503，其余模式不受影响。"""
    from fastapi.testclient import TestClient

    import server.app as app_module
    from core.canvas_repository import create_canvas_repository
    from core.session_repository import JsonlSessionRepository
    from server.registry import SessionRegistry

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    registry = SessionRegistry(
        llm=_EndpointLLM(),
        workspace=str(workspace),
        canvas_repository=create_canvas_repository(workspace),
        session_repository=JsonlSessionRepository(tmp_path / "sessions"),
    )
    monkeypatch.setattr(app_module, "WORKSPACE", workspace)
    monkeypatch.setattr(app_module, "registry", registry)
    from server import studio as studio_module

    studio_module.configure(registry=registry, workspace=workspace, media_registry=None)
    monkeypatch.setattr(app_module, "_pipeline_editor", None)

    with TestClient(app_module.app) as client:
        response = client.post("/api/agent", json={
            "prompt": "hi", "canvas": "c", "media_policy": "plan_only",
            "mode": "pipeline_editor"})
        assert response.status_code == 503
        # 其余 mode 不走分支（script_qa 照旧进 registry 会话链路）
        ok = client.post("/api/agent", json={
            "prompt": "hi", "canvas": "c", "media_policy": "plan_only", "mode": "script_qa"})
        assert ok.status_code == 200
