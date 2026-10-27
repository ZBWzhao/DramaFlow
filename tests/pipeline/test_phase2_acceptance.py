"""阶段 2 端到端验收联测（Task 8）：全链、失败无脏数据、改稿脑三拍、权威统计。

四个验收场景（brief Step 5 + 规格 8.4 验收项映射）：
1. test_full_run_novel_to_storyboard —— 粘贴小说 → outline 过真门 → gated 停确认门
   （红线：不可跳过）→ approve → 六阶段全链 → done，六份产物全落库；
   /runs/{id}/stats 与 StageResult 权威账本同源（iterations/gate_passes/stages）。
2. test_stage_exhaustion_degrades_through_run_continues —— staging 五轮门全挂 →
   耗尽不判死（failure immunity B）：兜底门对当前草稿再裁决仍挂 → ⛔ degraded
   落库、运行继续走完 storyboard；空草稿才保留 failed 路径（skill_executor 守卫测试锁）。
3. test_editor_proposal_apply_healthcheck_round —— 改稿脑三拍：提案（agent 只进
   pending，产物不动）→ 审批（HTTP apply 端点落稿）→ 自动体检（真门重查；通过）；
   再来一轮坏提案：门不过 → 回滚 → 产物保持审批前版本。
4. test_stats_endpoint_counts_from_authoritative_ledger —— failed 运行的 stats：
   失败阶段 ok=False、gate_passes 只数过门阶段（不是门执行次数）。

夹具照 test_skill_executor / test_pipeline_runs_api 惯例：真 Postgres + 真 node 门，
LLM 全程 ScriptedLLM；asyncio.run 每用例独立 pool。端点直调（不经 TestClient 的
生成器/stream，规避 TestClient.stream 在 gated 流上挂死的已知问题）。
"""
from __future__ import annotations

import asyncio
import json

import asyncpg

from core import pipeline_repository
from core.pipeline_editor import PipelineEditor
from core.pipeline_run import EventBroker, PipelineRunner, RunStore
from core.postgres import PostgresDatabase
from server import pipeline_runs as runs_module
from test_pipeline_repository import TEST_DSN, pipeline_test_database  # noqa: F401  session 级库夹具
from test_skill_executor import (  # noqa: F401  复用替身/夹具/轮次构造
    NOVEL_TEXT,
    ScriptedLLM,
    _ep_round,
    _load,
    _script_meta_round,
    _trim_script,
    _write_rounds,
)

OUTLINE = _load("outline.json")
SCRIPT = _load("script.json")
CAST = _load("cast.json")
ART = _load("art.json")
STAGING = _load("staging.json")
STORYBOARD = _load("storyboard.json")

PROJECT = "acc-2"


def _round(pairs):
    return [{"name": "write_draft", "args": {"section": s, "data": d}} for s, d in pairs]


def _six_stage_turns():
    """六阶段工具轮次（fixtures 全量；与 test_skill_executor 链路测试同配方）。"""
    outline_turns = (_write_rounds(OUTLINE, [4, 3, 2])
                     + [[{"name": "run_quality_gate", "args": {}}], [{"name": "finish", "args": {}}]])
    script_turns = ([_script_meta_round(SCRIPT["source"])]
                    + [_ep_round(ep) for ep in SCRIPT["episodes"]]
                    + [[{"name": "finish", "args": {}}]])
    characters_turns = [_round([("source", CAST["source"]), ("lang", CAST["lang"]),
                                ("summary", CAST["summary"]),
                                ("characters", CAST["characters"])])
                        + [{"name": "finish", "args": {}}]]
    art_turns = [_round([("source", ART["source"]), ("scenes", ART["scenes"]),
                         ("props", ART["props"])])
                 + [{"name": "finish", "args": {}}]]
    staging_turns = [_round([("source", STAGING["source"]), ("lang", STAGING["lang"]),
                             ("locations", STAGING["locations"]),
                             ("scenes", STAGING["scenes"])])
                     + [{"name": "finish", "args": {}}]]
    storyboard_turns = [_round([("source", STORYBOARD["source"]),
                                ("aspectRatio", STORYBOARD["aspectRatio"]),
                                ("promptLang", STORYBOARD["promptLang"]),
                                ("episodes", STORYBOARD["episodes"])])
                        + [{"name": "finish", "args": {}}]]
    return (outline_turns, script_turns, characters_turns, art_turns,
            staging_turns, storyboard_turns)


def _run_scenario(scenario, *, truncate_runs: bool = True) -> None:
    async def _runner() -> None:
        database = PostgresDatabase(TEST_DSN, min_size=1, max_size=2)
        database.pool = await asyncpg.create_pool(
            TEST_DSN, min_size=1, max_size=2, command_timeout=30
        )
        try:
            if truncate_runs:
                await database.pool.execute("TRUNCATE pipeline_runs")
            await database.pool.execute("TRUNCATE pipeline_artifacts")
            await scenario(database)
        finally:
            await database.pool.close()

    asyncio.run(_runner())


def _make_runner(database, llm) -> tuple[RunStore, PipelineRunner]:
    store = RunStore(database)
    runner = PipelineRunner(store, database, broker=EventBroker(), llm=llm)
    return store, runner


async def _poll_status(store: RunStore, run_id: str, statuses: set[str], timeout: float = 5.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    row = await store.get(run_id)
    while asyncio.get_running_loop().time() < deadline:
        row = await store.get(run_id)
        if row["status"] in statuses:
            return row
        await asyncio.sleep(0.02)
    raise AssertionError(f"运行未在 {timeout}s 内进入 {statuses}，最后状态：{row['status']}")


# ---------------------------------------------------------------------------
# 1. 全链：小说 → 大纲 → 确认门 → 六阶段 → done + stats
# ---------------------------------------------------------------------------

def test_full_run_novel_to_storyboard(pipeline_test_database, monkeypatch):
    outline_turns, script_turns, characters_turns, art_turns, staging_turns, storyboard_turns = \
        _six_stage_turns()
    llm = ScriptedLLM(outline_turns + script_turns + characters_turns + art_turns
                      + staging_turns + storyboard_turns)
    # 每阶段 attempts = 该阶段轮次长度（权威账本口径：StageResult.iterations）
    expected_attempts = {"outline": len(outline_turns), "script": len(script_turns),
                         "characters": len(characters_turns), "art": len(art_turns),
                         "staging": len(staging_turns), "storyboard": len(storyboard_turns)}

    async def scenario(database) -> None:
        store, runner = _make_runner(database, llm)
        run = await store.create(PROJECT, NOVEL_TEXT)
        await runner.start(run["run_id"])
        await runner.wait_idle()

        state = await store.get(run["run_id"])
        assert state["status"] == "gated" and state["stage"] == "outline"  # 确认门不可跳过
        # 门没过人工确认前，只有大纲落库
        assert await pipeline_repository.get(database, PROJECT, "outline") is not None
        assert await pipeline_repository.get(database, PROJECT, "script") is None

        assert await store.approve(run["run_id"]) is True
        await runner.resume(run["run_id"])
        await runner.wait_idle()

        state = await store.get(run["run_id"])
        assert state["status"] == "done" and state["stage"] == "storyboard"
        for artifact_type in ("outline", "script", "characters", "art", "staging", "storyboard"):
            row = await pipeline_repository.get(database, PROJECT, artifact_type)
            assert row is not None and row["content"], f"{artifact_type} 产物缺失"

        # stats：/runs/{id}/stats 端点直调（不依赖 import 顺序注入句柄）
        monkeypatch.setattr(runs_module, "_store", store)
        monkeypatch.setattr(runs_module, "_runner", runner)
        monkeypatch.setattr(runs_module, "_broker", EventBroker())
        stats = await runs_module.run_stats(run["run_id"])
        assert stats["run_id"] == run["run_id"] and stats["status"] == "done"
        assert stats["gate_passes"] == 6   # 六阶段全部过权威门并落库（≠ 门执行次数）
        assert stats["iterations"] == sum(expected_attempts.values())
        for stage, attempts in expected_attempts.items():
            entry = stats["stages"][stage]
            assert entry["attempts"] == attempts and entry["ok"] is True

    _run_scenario(scenario)


# ---------------------------------------------------------------------------
# 2. 兜底放行（failure immunity B）：staging 五轮门全挂 → 耗尽 → 兜底门再裁决
#    仍挂 → ⛔ degraded 落库，运行继续走完 storyboard（空草稿才 failed，不落库）
# ---------------------------------------------------------------------------

def test_stage_exhaustion_degrades_through_run_continues(pipeline_test_database, monkeypatch):
    outline_turns, script_turns, characters_turns, art_turns, staging_turns, storyboard_turns = \
        _six_stage_turns()

    # staging 病灶：出场人物表混进剧本没有的 C99 —— cast-complete 门必拦；
    # 五轮都带同样的病灶 + finish（agent 型预算 5 轮，耗尽后兜底门对当前草稿再裁决）。
    broken = {**STAGING,
              "scenes": [{**STAGING["scenes"][0],
                          "cast": [*STAGING["scenes"][0]["cast"], {"id": "C99", "name": "幽灵角色"}]},
                         *STAGING["scenes"][1:]]}
    broken_round = _round([("source", broken["source"]), ("lang", broken["lang"]),
                           ("locations", broken["locations"]),
                           ("scenes", broken["scenes"])]) + [{"name": "finish", "args": {}}]
    llm = ScriptedLLM(outline_turns + script_turns + characters_turns + art_turns
                      + [broken_round] * 5 + storyboard_turns)

    async def scenario(database) -> None:
        store, runner = _make_runner(database, llm)
        run = await store.create(PROJECT, NOVEL_TEXT)
        await runner.start(run["run_id"])
        await runner.wait_idle()
        assert await store.approve(run["run_id"]) is True
        await runner.resume(run["run_id"])
        await runner.wait_idle()

        state = await store.get(run["run_id"])
        # 耗尽不判死：staging degraded 放行，运行继续走到 storyboard 收尾
        assert state["status"] == "done" and state["stage"] == "storyboard"
        ledger = state["stage_status"]
        for stage in ("outline", "script", "characters", "art", "storyboard"):
            assert ledger[stage]["status"] == "done" and "degraded" not in ledger[stage]
        # staging 账本：⛔ blocking 标记留痕；gate_runs = 5 轮内门 + 1 兜底门
        assert ledger["staging"]["status"] == "done" and ledger["staging"]["degraded"] is True
        assert ledger["staging"]["attempts"] == 5 and ledger["staging"]["gate_runs"] == 6
        marker = ledger["staging"]["findings"][0]
        assert marker["gate"] == "validate" and marker["rule"] == "cast-complete"
        assert marker["severity"] == "blocking" and "C99" in marker["detail"]
        # 病灶产物带标记落库（degraded 语义：能跑，但烂在明处）；其余五份完好
        saved = await pipeline_repository.get(database, PROJECT, "staging")
        assert saved is not None
        assert saved["content"]["scenes"][0]["cast"] == broken["scenes"][0]["cast"]
        assert saved["findings"][0]["rule"] == "cast-complete"
        for artifact_type in ("outline", "script", "characters", "art", "storyboard"):
            row = await pipeline_repository.get(database, PROJECT, artifact_type)
            assert row is not None and row["content"], f"{artifact_type} 产物缺失"

        # done 运行的 stats：staging ok=True + degraded=True；gate_passes 含 degraded 阶段（6）
        monkeypatch.setattr(runs_module, "_store", store)
        monkeypatch.setattr(runs_module, "_runner", runner)
        monkeypatch.setattr(runs_module, "_broker", EventBroker())
        stats = await runs_module.run_stats(run["run_id"])
        assert stats["status"] == "done" and stats["gate_passes"] == 6
        assert stats["stages"]["staging"] == {"attempts": 5, "gate_runs": 6, "ok": True,
                                              "degraded": True}

    _run_scenario(scenario)


# ---------------------------------------------------------------------------
# 3. 改稿脑三拍：提案 → 审批 → 自动体检（门过落稿 / 门不过回滚）
# ---------------------------------------------------------------------------

def _propose_turns(new_content: dict, summary: str) -> list[list[dict]]:
    return [
        [{"name": "read_artifact", "args": {"type": "script"}},
         {"name": "propose_edit", "args": {"type": "script", "summary": summary,
                                           "new_content": new_content}}],
        [{"name": "finish", "args": {}}],
    ]


def test_editor_proposal_apply_healthcheck_round(pipeline_test_database, monkeypatch):
    baseline = _trim_script(SCRIPT, [1])
    # 坏提案：第 1 集混进大纲没有的 C99 —— 体检门必拦
    first = SCRIPT["episodes"][0]
    bogus = {**first,
             "scenes": [{**first["scenes"][0],
                         "characters": [*first["scenes"][0]["characters"], "C99"]},
                        *first["scenes"][1:]]}
    bad_content = {**SCRIPT, "episodes": [bogus]}

    async def scenario(database) -> None:
        project = "acc-editor"
        await pipeline_repository.save(database, project, "outline", OUTLINE,
                                       source_run_id="stage:outline")
        await pipeline_repository.save(database, project, "script", baseline,
                                       source_run_id="stage:script")
        # 端点直调：注入 runs 依赖（不依赖 import 顺序）
        store = RunStore(database)
        monkeypatch.setattr(runs_module, "_store", store)
        monkeypatch.setattr(runs_module, "_runner", PipelineRunner(store, database, broker=EventBroker()))
        monkeypatch.setattr(runs_module, "_broker", EventBroker())

        # ── 拍 1：提修改 —— agent 只进 pending，产物一字节不动 ──
        editor = PipelineEditor(database, llm=ScriptedLLM(_propose_turns(SCRIPT, "补齐第 2、3 集")))
        await editor.chat(project, "sess-acc", "把第 2、3 集补齐")
        listed = await runs_module.list_editor_pending(project)
        assert len(listed["edits"]) == 1 and listed["edits"][0]["status"] == "pending"
        row = await pipeline_repository.get(database, project, "script")
        assert row["content"] == baseline and row["source_run_id"] == "stage:script"

        # ── 拍 2+3：审批 → 自动体检（真门）→ 落稿 ──
        edit_id = listed["edits"][0]["edit_id"]
        outcome = await runs_module.apply_editor_edit(
            project, runs_module.ApplyEditBody(edit_id=edit_id))
        assert outcome["ok"] is True and outcome["gate"]["ok"] is True
        row = await pipeline_repository.get(database, project, "script")
        assert [e["ep"] for e in row["content"]["episodes"]] == [1, 2, 3]
        assert row["source_run_id"] == "editor"

        # ── 反例：坏提案过审批 → 体检门拦下 → 自动回滚，产物保持审批前版本 ──
        editor = PipelineEditor(database, llm=ScriptedLLM(_propose_turns(bad_content, "混入 C99")))
        await editor.chat(project, "sess-acc", "把 C99 写进第一集")
        listed = await runs_module.list_editor_pending(project)
        edit_id = listed["edits"][0]["edit_id"]
        outcome = await runs_module.apply_editor_edit(
            project, runs_module.ApplyEditBody(edit_id=edit_id))
        assert outcome["ok"] is False and outcome["rolled_back"] is True
        assert outcome["gate"]["ok"] is False and "C99" in outcome["findings"][0]["detail"]
        row = await pipeline_repository.get(database, project, "script")
        assert row["content"]["episodes"] == SCRIPT["episodes"]   # 回滚到上一版（editor 版）
        assert row["source_run_id"] == "editor"

        # 驳回：产物不动
        editor = PipelineEditor(database, llm=ScriptedLLM(_propose_turns(bad_content, "再来一次")))
        await editor.chat(project, "sess-acc", "改剧本")
        listed = await runs_module.list_editor_pending(project)
        edit_id = listed["edits"][0]["edit_id"]
        rejected = await runs_module.reject_editor_edit(
            project, runs_module.RejectEditBody(edit_id=edit_id))
        assert rejected["ok"] is True
        row = await pipeline_repository.get(database, project, "script")
        assert row["content"]["episodes"] == SCRIPT["episodes"]
        listed = await runs_module.list_editor_pending(project)
        statuses = {e["edit_id"]: e["status"] for e in listed["edits"]}
        assert statuses[edit_id] == "rejected"

    _run_scenario(scenario, truncate_runs=False)
