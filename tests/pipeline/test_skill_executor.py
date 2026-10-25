"""技能执行器测试（Task 2）：gated agent loop + outline 阶段端到端。

两层"真"夹一层"假"：
- DB 是真库——夹具装配照抄 test_pipeline_repository.py 的 pipeline_test 惯例
  （session 级 CREATE DATABASE + 手工跑 0005/0006 幂等迁移；每用例独立
  asyncio.run + 独立 pool + TRUNCATE，asyncpg pool 绑定事件循环，必须与用例
  同循环，所以不用 pytest.mark.asyncio——本仓库未装 pytest-asyncio）。
- 质量门是真 node 子进程（novel-outline validate --stage full、novel-script
  validate --outline），逐道门判定、draft 组装、"门不过不落库"的约束全部是真的。
- 只有 LLM 是假的：ScriptedLLM 按预设轮次吐工具调用（一轮可多个，对应真实
  模型的并行工具调用），不碰网络。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest

from core import pipeline_repository
from core.pipeline_run import EventBroker, PipelineRunner, RunStore
from core.postgres import PostgresDatabase
from core.skill_executor import run_stage
from test_pipeline_repository import pipeline_test_database  # noqa: F401  session 级库装配

ROOT_DIR = Path(__file__).resolve().parents[2]
FIXTURE_OUTLINE = ROOT_DIR / "tests" / "fixtures" / "pipeline_run1" / "outline.json"
FIXTURE_SCRIPT = ROOT_DIR / "tests" / "fixtures" / "pipeline_run1" / "script.json"
NOVEL_TEXT = (ROOT_DIR / "tests" / "fixtures" / "sample_novel_v1.txt").read_text(encoding="utf-8")

# write_draft 的 section 序列 = 大纲顶层键：元数据三件 + 六节（与 STAGE_SPECS 对齐）。
SECTION_ORDER = ("source", "lang", "params", "adaptation", "characters", "scenes", "props", "beats", "episodes")


def _load(name: str) -> dict:
    return json.loads((ROOT_DIR / "tests" / "fixtures" / "pipeline_run1" / name).read_text(encoding="utf-8"))


def _trim_outline(outline: dict, eps: list[int]) -> dict:
    """把大纲裁到指定集：分集与爽点同步裁、params.episodes 跟随，保持大纲自洽
    （script 门按它对账角色/爽点/落集）。"""
    return {**outline,
            "params": {**outline["params"], "episodes": len(eps)},
            "episodes": [e for e in outline["episodes"] if e["ep"] in eps],
            "beats": [b for b in outline["beats"] if b["episode"] in eps]}


def _trim_script(script: dict, eps: list[int]) -> dict:
    return {**script, "episodes": [e for e in script["episodes"] if e["ep"] in eps]}


def _script_meta_round(source: str) -> list[dict]:
    """script 的元数据一轮：source/lang 并行写（params 刻意不在可写键里，见 spec）。"""
    return [{"name": "write_draft", "args": {"section": "source", "data": source}},
            {"name": "write_draft", "args": {"section": "lang", "data": "zh"}}]


def _ep_round(ep: dict) -> list[dict]:
    """逐集写一轮：data 只带这一集，靠 merge_sections 按 ep 并入已写集。"""
    return [{"name": "write_draft", "args": {"section": "episodes", "data": [ep]}}]


class ScriptedLLM:
    """complete() 按预设轮次返回；每轮是若干工具调用；记录 messages 供断言。"""

    def __init__(self, turns: list[list[dict]]):
        self.turns = list(turns)
        self.calls: list[list[dict]] = []

    async def complete(self, messages, tools):
        self.calls.append(list(messages))
        return SimpleNamespace(text="", tool_calls=list(self.turns.pop(0)))


def _write_rounds(outline: dict, group_sizes: list[int]) -> list[list[dict]]:
    """把大纲顶层键按组切成 write_draft 轮（每组一轮，轮内并行多调用）。"""
    sections = [key for key in SECTION_ORDER if key in outline]
    turns, i = [], 0
    for size in group_sizes:
        turns.append([
            {"name": "write_draft", "args": {"section": key, "data": outline[key]}}
            for key in sections[i:i + size]
        ])
        i += size
    assert i == len(sections), f"分组没盖全顶层键：{i}/{len(sections)}"
    return turns


def _run_scenario(scenario, dsn: str) -> None:
    """同一 asyncio.run 里装配 pool + TRUNCATE + 跑用例 + 关池（照抄仓库测试）。"""

    async def _runner() -> None:
        database = PostgresDatabase(dsn, min_size=1, max_size=2)
        database.pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=2, command_timeout=30
        )
        try:
            await database.pool.execute("TRUNCATE pipeline_artifacts")
            await scenario(database)
        finally:
            await database.pool.close()

    asyncio.run(_runner())


def test_outline_stage_agent_loop_saves_artifact(pipeline_test_database):
    good_outline = json.loads(FIXTURE_OUTLINE.read_text(encoding="utf-8"))

    async def scenario(database) -> None:
        # 5 轮 = 默认迭代上限内完成：4+3+2 并行写九节 → 自检门 → finish（权威门过 → 落库）
        turns = (_write_rounds(good_outline, [4, 3, 2])
                 + [[{"name": "run_quality_gate", "args": {}}], [{"name": "finish", "args": {}}]])
        llm = ScriptedLLM(turns)
        events: list[dict] = []
        result = await run_stage(database, "proj-x", "outline", {"novel_text": NOVEL_TEXT},
                                 llm=llm, on_event=events.append)
        assert result.ok and result.artifact_type == "outline"
        assert result.iterations == 5
        assert any(e["type"] == "gate" and e["ok"] for e in events)
        assert events[-1] == {"type": "done", "ok": True}
        assert len(llm.calls) == 5  # 每轮恰好一次 complete()
        saved = await pipeline_repository.get(database, "proj-x", "outline")
        assert saved["content"]["source"] == good_outline["source"]
        assert saved["content"]["params"] == good_outline["params"]
        assert saved["content"]["episodes"] == good_outline["episodes"]
        assert saved["content"]["characters"] == good_outline["characters"]
        # 产物来源标记 = stage 前缀（Task 3 运行记录按此溯源）
        rows = await pipeline_repository.list_all(database, "proj-x")
        assert rows[0]["source_run_id"] == "stage:outline"

    _run_scenario(scenario, pipeline_test_database)


def test_gate_failure_exhausts_iterations_without_saving(pipeline_test_database):
    async def scenario(database) -> None:
        # 五轮全在自检门上打转，finish 永远排不到——迭代耗尽必须无产物、带 findings
        llm = ScriptedLLM([[{"name": "run_quality_gate", "args": {}}] for _ in range(5)]
                          + [[{"name": "finish", "args": {}}]])
        result = await run_stage(database, "proj-x", "outline", {"novel_text": NOVEL_TEXT}, llm=llm)
        assert not result.ok and result.findings
        assert result.iterations == 5
        assert len(result.gate_log) == 5  # 每轮自检都是真门，输出进 gate_log
        assert result.findings[0]["gate"] == "validate" and result.findings[0]["detail"]
        assert await pipeline_repository.get(database, "proj-x", "outline") is None

    _run_scenario(scenario, pipeline_test_database)


def test_gate_failure_then_repair_passes_and_saves(pipeline_test_database):
    good_outline = json.loads(FIXTURE_OUTLINE.read_text(encoding="utf-8"))
    bad_outline = json.loads(FIXTURE_OUTLINE.read_text(encoding="utf-8"))
    bad_outline["params"] = {**bad_outline["params"], "episodes": 5}  # ≠ episodes 长度(3)，门必拦

    async def scenario(database) -> None:
        turns = (
            _write_rounds(bad_outline, [9])
            + [[{"name": "finish", "args": {}}]]
            + [[{"name": "write_draft", "args": {"section": "params", "data": good_outline["params"]}}]]
            + [[{"name": "finish", "args": {}}]]
        )
        llm = ScriptedLLM(turns)
        events: list[dict] = []
        result = await run_stage(database, "proj-fix", "outline", {"novel_text": NOVEL_TEXT},
                                 llm=llm, on_event=events.append)
        assert result.ok and result.iterations == 4
        assert any(e["type"] == "gate" and not e["ok"] for e in events)
        assert any(e["type"] == "gate" and e["ok"] for e in events)
        # 修复轮收到带门报告的 user 消息（失败要说清病灶，模型才修得动）
        assert any(m["role"] == "user" and m["content"].startswith("质量门未过")
                   for m in llm.calls[2])
        saved = await pipeline_repository.get(database, "proj-fix", "outline")
        assert saved["content"]["params"] == good_outline["params"]

    _run_scenario(scenario, pipeline_test_database)


def test_exhaustion_without_finish_still_reports_findings(pipeline_test_database):
    async def scenario(database) -> None:
        llm = ScriptedLLM([[] for _ in range(3)])  # 三轮空转，一个工具都不调
        result = await run_stage(database, "proj-idle", "outline", {"novel_text": NOVEL_TEXT},
                                 llm=llm, max_iterations=3)
        assert not result.ok and result.findings
        assert await pipeline_repository.get(database, "proj-idle", "outline") is None

    _run_scenario(scenario, pipeline_test_database)


def test_system_prompt_is_vendored_playbook_verbatim(pipeline_test_database):
    """提示词口径：SKILL.md 与 schema.md 运行时全文进 system，禁止手抄副本漂移。"""

    async def scenario(database) -> None:
        skill_md = (ROOT_DIR / "pipeline_skills" / "novel-outline" / "SKILL.md").read_text(encoding="utf-8")
        schema_md = (ROOT_DIR / "pipeline_skills" / "novel-outline" / "references" / "schema.md").read_text(encoding="utf-8")
        llm = ScriptedLLM([[]])
        await run_stage(database, "proj-prompt", "outline", {"novel_text": NOVEL_TEXT},
                        llm=llm, max_iterations=1)
        system, user = llm.calls[0][0]["content"], llm.calls[0][1]["content"]
        assert skill_md in system and schema_md in system  # 全文原样，非摘要
        assert "write_draft" in system and "finish" in system
        assert NOVEL_TEXT in user  # 小说原文在 user 消息里

    _run_scenario(scenario, pipeline_test_database)


# ---------------------------------------------------------------------------
# Task 5：script 阶段（节拍流 + 时长门 + outline→script 上游注入/分批合并）
# ---------------------------------------------------------------------------

def test_script_stage_writes_episodes_per_ep_and_saves(pipeline_test_database):
    outline = _trim_outline(_load("outline.json"), [1, 2])
    script = _trim_script(_load("script.json"), [1, 2])

    async def scenario(database) -> None:
        # 5 轮 = 元数据一轮 + 逐集各一轮（跨轮按 ep 并入）+ 自检门 + finish（权威门过 → 落库）
        turns = ([_script_meta_round(script["source"])]
                 + [_ep_round(ep) for ep in script["episodes"]]
                 + [[{"name": "run_quality_gate", "args": {}}], [{"name": "finish", "args": {}}]])
        llm = ScriptedLLM(turns)
        events: list[dict] = []
        result = await run_stage(database, "proj-script", "script",
                                 {"novel_text": NOVEL_TEXT, "outline": outline},
                                 llm=llm, on_event=events.append)
        assert result.ok and result.artifact_type == "script"
        assert result.iterations == 5
        assert any(e["type"] == "gate" and e["ok"] for e in events)
        assert events[-1] == {"type": "done", "ok": True}
        saved = await pipeline_repository.get(database, "proj-script", "script")
        # 逐集写的两集都在：write_draft 按 ep 并入，跨轮不丢集；集号与 inputs outline 一致
        assert [e["ep"] for e in saved["content"]["episodes"]] == [1, 2]
        assert saved["content"]["episodes"] == script["episodes"]
        assert saved["content"]["source"] == outline["source"]
        # 上游注入约定：outline 随 user 消息进对话（模型没有 Read 工具，要看内容写戏）
        user = llm.calls[0][1]["content"]
        assert outline["episodes"][0]["synopsis"][:30] in user
        rows = await pipeline_repository.list_all(database, "proj-script")
        assert rows[0]["source_run_id"] == "stage:script"

    _run_scenario(scenario, pipeline_test_database)


def test_script_duration_gate_failure_then_repair_saves(pipeline_test_database):
    outline = _trim_outline(_load("outline.json"), [1, 2])
    script = _trim_script(_load("script.json"), [1, 2])
    # 时长门靶子：第 1 集每场只留前 2 拍——估算秒数远低于 90s 的 -15% 下限，门必拦；
    # 其余门（hookBeat/has-action/speaker/beatsClaimed）不受截断影响，只欠时长。
    short_ep1 = {**script["episodes"][0],
                 "scenes": [{**sc, "flow": sc["flow"][:2]} for sc in script["episodes"][0]["scenes"]]}

    async def scenario(database) -> None:
        turns = ([_script_meta_round(script["source"])]
                 + [[{"name": "write_draft", "args": {"section": "episodes",
                                                      "data": [short_ep1, script["episodes"][1]]}}]]
                 + [[{"name": "finish", "args": {}}]]        # 权威门：第 1 集欠时长
                 + [_ep_round(script["episodes"][0])]        # 单集修复（按 ep 并入，不重发第 2 集）
                 + [[{"name": "finish", "args": {}}]])
        llm = ScriptedLLM(turns)
        events: list[dict] = []
        result = await run_stage(database, "proj-duration", "script",
                                 {"novel_text": NOVEL_TEXT, "outline": outline},
                                 llm=llm, on_event=events.append)
        assert result.ok and result.iterations == 5
        gate_events = [e for e in events if e["type"] == "gate"]
        assert [e["ok"] for e in gate_events] == [False, True]  # 时长门先拦后放
        # 失败必须把报告喂回对话：报告点名「欠」多少秒，模型才修得动
        feedback = llm.calls[3][-1]["content"]
        assert feedback.startswith("质量门未过") and "欠" in feedback
        saved = await pipeline_repository.get(database, "proj-duration", "script")
        # 单集修复按 ep 整对象替换、位置不变；第 2 集原样保留
        assert [e["ep"] for e in saved["content"]["episodes"]] == [1, 2]
        assert saved["content"]["episodes"][0] == script["episodes"][0]
        assert saved["content"]["episodes"][1] == script["episodes"][1]

    _run_scenario(scenario, pipeline_test_database)


def test_script_save_merges_by_ep_preserving_other_episodes(pipeline_test_database):
    """data-contract「script 分批合并」：本批只写 eps 1-2，库内既有的第 3 集必须
    原样保留（script 按集合并落库；outline 才是 latest-wins 整行替换）。"""
    outline = _trim_outline(_load("outline.json"), [1, 2])
    full_script = _load("script.json")

    async def scenario(database) -> None:
        old_ep3 = full_script["episodes"][2]
        await pipeline_repository.save(database, "proj-merge", "script",
                                       {"source": full_script["source"], "episodes": [old_ep3]},
                                       source_run_id="import:batch0")
        script = _trim_script(full_script, [1, 2])
        turns = ([_script_meta_round(script["source"])]
                 + [[{"name": "write_draft", "args": {"section": "episodes",
                                                      "data": script["episodes"]}}]]
                 + [[{"name": "finish", "args": {}}]])
        result = await run_stage(database, "proj-merge", "script",
                                 {"novel_text": NOVEL_TEXT, "outline": outline},
                                 llm=ScriptedLLM(turns))
        assert result.ok
        saved = await pipeline_repository.get(database, "proj-merge", "script")
        # 既有集原位保留、新集追加在后（与 pipeline_import._merge_by_key 同语义）
        assert [e["ep"] for e in saved["content"]["episodes"]] == [3, 1, 2]
        assert saved["content"]["episodes"][0] == old_ep3
        assert saved["content"]["episodes"][1:] == script["episodes"]

    _run_scenario(scenario, pipeline_test_database)


def test_script_gate_audits_seeded_outline(pipeline_test_database):
    """gate_needs 落盘是真门输入：剧本引用大纲没有的角色，refs-characters 门必须
    点名——outline.json 没被写进工作目录时，这条对账只会 ENOENT 崩溃或整体跳过。"""
    outline = _trim_outline(_load("outline.json"), [1, 2])
    script = _trim_script(_load("script.json"), [1, 2])
    first = script["episodes"][0]
    bogus_ep1 = {**first,
                 "scenes": [{**first["scenes"][0], "characters": [*first["scenes"][0]["characters"], "C99"]},
                            *first["scenes"][1:]]}

    async def scenario(database) -> None:
        turns = ([_script_meta_round(script["source"])]
                 + [[{"name": "write_draft", "args": {"section": "episodes",
                                                      "data": [bogus_ep1, script["episodes"][1]]}}]]
                 + [[{"name": "finish", "args": {}}]])
        result = await run_stage(database, "proj-audit", "script",
                                 {"novel_text": NOVEL_TEXT, "outline": outline},
                                 llm=ScriptedLLM(turns), max_iterations=3)
        assert not result.ok
        assert "C99" in result.findings[0]["detail"]  # 对账真的发生在大纲上
        assert await pipeline_repository.get(database, "proj-audit", "script") is None

    _run_scenario(scenario, pipeline_test_database)


def test_runner_chains_outline_then_script_with_real_specs(pipeline_test_database):
    """Task 5 Step 3：真 STAGE_SPECS + 真 run_stage + 真门，fake LLM 全程脚本化——
    outline 过门 → gated → approve → script（inputs.outline = 已落库大纲）→ done。"""

    async def scenario(database) -> None:
        await database.pool.execute("TRUNCATE pipeline_runs")
        outline = _load("outline.json")
        script = _load("script.json")
        outline_turns = (_write_rounds(outline, [4, 3, 2])
                         + [[{"name": "run_quality_gate", "args": {}}], [{"name": "finish", "args": {}}]])
        script_turns = ([_script_meta_round(script["source"])]
                        + [_ep_round(ep) for ep in script["episodes"]]
                        + [[{"name": "finish", "args": {}}]])
        llm = ScriptedLLM(outline_turns + script_turns)
        store, broker = RunStore(database), EventBroker()
        runner = PipelineRunner(store, database, broker=broker, llm=llm)  # executor 缺省 = 真 run_stage
        run = await store.create("proj-chain", NOVEL_TEXT)
        events = broker.subscribe(run["run_id"])

        await runner.start(run["run_id"])
        await runner.wait_idle()
        state = await store.get(run["run_id"])
        assert state["status"] == "gated" and state["stage"] == "outline"  # 确认门不可跳过

        assert await store.approve(run["run_id"]) is True
        await runner.resume(run["run_id"])
        await runner.wait_idle()
        state = await store.get(run["run_id"])
        # script 过门后继续推进：characters 起没有规格（Task 6 注册）→ 无剩余可运行
        # 阶段 → done。done = 「没有可运行阶段」的 Task 3 口径，不是六阶段全完成。
        assert state["status"] == "done"
        assert [c["stage"] for c in await store.stage_history(run["run_id"])] == ["outline", "script"]
        assert state["stage_status"]["script"]["status"] == "done"
        saved = await pipeline_repository.get(database, "proj-chain", "script")
        assert [e["ep"] for e in saved["content"]["episodes"]] == [1, 2, 3]
        rows = {r["artifact_type"]: r for r in await pipeline_repository.list_all(database, "proj-chain")}
        assert rows["script"]["source_run_id"] == "stage:script"
        # runner._upstream 真把已落库大纲塞进了 script 阶段的对话（拼在 user 消息里）
        script_user = llm.calls[len(outline_turns)][1]["content"]
        assert outline["episodes"][0]["synopsis"][:30] in script_user
        # SSE 中继：script 的权威门以 stage_gate/stage_done 透传（run 级 gate 仍留给确认门）
        seq = [(e["type"], e.get("stage")) for e in
               [events.get_nowait() for _ in range(events.qsize())]]
        assert ("stage", "script") in seq and ("stage_done", "script") in seq

    _run_scenario(scenario, pipeline_test_database)
