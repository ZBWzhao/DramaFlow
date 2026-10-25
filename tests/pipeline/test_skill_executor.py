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


def test_runner_chains_all_six_stages_with_real_specs(pipeline_test_database):
    """Task 6（替代 Task 5 的两阶段链）：真 STAGE_SPECS + 真 run_stage + 真门，
    fake LLM 全程脚本化——outline 过门 → gated → approve → script → characters →
    art → staging → storyboard → done，六份产物全部落库。"""

    async def scenario(database) -> None:
        await database.pool.execute("TRUNCATE pipeline_runs")
        outline = _load("outline.json")
        script = _load("script.json")
        cast = _load("cast.json")
        art = _load("art.json")
        staging = _load("staging.json")
        storyboard = _load("storyboard.json")

        def _round(pairs):
            return [{"name": "write_draft", "args": {"section": s, "data": d}} for s, d in pairs]

        outline_turns = (_write_rounds(outline, [4, 3, 2])
                         + [[{"name": "run_quality_gate", "args": {}}], [{"name": "finish", "args": {}}]])
        script_turns = ([_script_meta_round(script["source"])]
                        + [_ep_round(ep) for ep in script["episodes"]]
                        + [[{"name": "finish", "args": {}}]])
        # 制片四层一轮写全 + finish（chain_repair 同轮 finish；agent 型此处门全过直接收）
        characters_turns = [_round([("source", cast["source"]), ("lang", cast["lang"]),
                                    ("summary", cast["summary"]),
                                    ("characters", cast["characters"])])
                            + [{"name": "finish", "args": {}}]]
        art_turns = [_round([("source", art["source"]), ("scenes", art["scenes"]),
                             ("props", art["props"])])
                     + [{"name": "finish", "args": {}}]]
        staging_turns = [_round([("source", staging["source"]), ("lang", staging["lang"]),
                                 ("locations", staging["locations"]),
                                 ("scenes", staging["scenes"])])
                         + [{"name": "finish", "args": {}}]]
        storyboard_turns = [_round([("source", storyboard["source"]),
                                    ("aspectRatio", storyboard["aspectRatio"]),
                                    ("promptLang", storyboard["promptLang"]),
                                    ("episodes", storyboard["episodes"])])
                            + [{"name": "finish", "args": {}}]]
        llm = ScriptedLLM(outline_turns + script_turns + characters_turns + art_turns
                          + staging_turns + storyboard_turns)
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
        # done = storyboard 过门（Task 6 起「没有剩余可运行阶段」与「六阶段全完成」重合）
        assert state["status"] == "done" and state["stage"] == "storyboard"
        assert [c["stage"] for c in await store.stage_history(run["run_id"])] == \
            ["outline", "script", "characters", "art", "staging", "storyboard"]
        for stage in ("script", "characters", "art", "staging", "storyboard"):
            assert state["stage_status"][stage]["status"] == "done"
        rows = {r["artifact_type"]: r for r in await pipeline_repository.list_all(database, "proj-chain")}
        assert set(rows) == {"outline", "script", "characters", "art", "staging", "storyboard"}
        assert rows["characters"]["source_run_id"] == "stage:characters"
        assert rows["storyboard"]["source_run_id"] == "stage:storyboard"
        # 制片四层产物逐字对账 fixtures（characters 经 save_merge 逐卡并入后仍与稿一致）
        saved_cast = await pipeline_repository.get(database, "proj-chain", "characters")
        assert saved_cast["content"]["characters"] == cast["characters"]
        assert saved_cast["content"]["summary"] == cast["summary"]
        saved_board = await pipeline_repository.get(database, "proj-chain", "storyboard")
        assert saved_board["content"]["episodes"] == storyboard["episodes"]
        # 上游注入按 gate_needs：characters 的对话里有已落库大纲（拆角色的事实来源），
        # storyboard 的对话里有已落库站位（切镜/走位的事实来源）
        cast_user = llm.calls[len(outline_turns) + len(script_turns)][1]["content"]
        assert outline["characters"][0]["name"] in cast_user
        board_user = llm.calls[-1][1]["content"]   # storyboard 只有一轮，最后一次 complete 即它
        assert staging["locations"][0]["name"] in board_user
        # SSE 中继：每个制片阶段都有 stage 起跑与 stage_done 收尾
        seq = [(e["type"], e.get("stage")) for e in
               [events.get_nowait() for _ in range(events.qsize())]]
        for stage in ("characters", "art", "staging", "storyboard"):
            assert ("stage", stage) in seq and ("stage_done", stage) in seq

    _run_scenario(scenario, pipeline_test_database)


# ---------------------------------------------------------------------------
# Task 6：制片四层（characters 链式修复 / staging agent 环）+ 上游落盘名对齐注册表
# ---------------------------------------------------------------------------

def test_characters_stage_chain_repair_writes_all_and_saves(pipeline_test_database):
    """填表型：一轮并行写全 + 同轮 finish（权威门过 → 落库）。轮次脚本只有一次
    产出、没有自检轮——chain_repair 的纪律就是门即自检。"""
    outline = _load("outline.json")
    cast = _load("cast.json")

    async def scenario(database) -> None:
        turns = [[{"name": "write_draft", "args": {"section": s, "data": cast[s]}}
                  for s in ("source", "lang", "summary", "characters")]
                 + [{"name": "finish", "args": {}}]]
        llm = ScriptedLLM(turns)
        events: list[dict] = []
        result = await run_stage(database, "proj-cast", "characters",
                                 {"novel_text": NOVEL_TEXT, "outline": outline},
                                 llm=llm, on_event=events.append, max_iterations=9)
        assert result.ok and result.artifact_type == "characters"
        assert result.iterations == 1                     # 单轮产全文 + finish
        assert any(e["type"] == "gate" and e["ok"] for e in events)
        assert events[-1] == {"type": "done", "ok": True}
        saved = await pipeline_repository.get(database, "proj-cast", "characters")
        assert saved["content"]["summary"] == cast["summary"]
        assert saved["content"]["characters"] == cast["characters"]
        rows = await pipeline_repository.list_all(database, "proj-cast")
        assert rows[0]["source_run_id"] == "stage:characters"
        # chain_repair 纪律进 system（唯一一轮修复机会）；门按 SKILL.md 拿到原文
        # book.txt 做逐字引文校验（cast 引文不是逐字时这条门会拦）
        system = llm.calls[0][0]["content"]
        assert "修复机会" in system and "run_quality_gate）" in system
        user = llm.calls[0][1]["content"]
        assert NOVEL_TEXT in user and outline["characters"][0]["name"] in user

    _run_scenario(scenario, pipeline_test_database)


def test_characters_chain_repair_gate_fail_repairs_exactly_once(pipeline_test_database):
    cast = _load("cast.json")

    async def scenario(database) -> None:
        def _turn(sections):
            return [{"name": "write_draft", "args": {"section": s, "data": cast[s]}}
                    for s in sections] + [{"name": "finish", "args": {}}]

        # 第一轮漏 summary（门必拦：报告顶部会空着）→ 恰好一轮修复 → 过门落库
        turns = [_turn(("source", "lang", "characters")), _turn(("summary",))]
        llm = ScriptedLLM(turns)
        events: list[dict] = []
        result = await run_stage(database, "proj-cast-fix", "characters",
                                 {"novel_text": NOVEL_TEXT, "outline": _load("outline.json")},
                                 llm=llm, on_event=events.append)
        assert result.ok and result.iterations == 2       # 产全文 + 一轮修复，没有第三轮
        assert [e["ok"] for e in events if e["type"] == "gate"] == [False, True]
        # 修复轮收到带门报告的 user 消息
        assert any(m["role"] == "user" and m["content"].startswith("质量门未过")
                   for m in llm.calls[1])
        saved = await pipeline_repository.get(database, "proj-cast-fix", "characters")
        assert saved["content"]["summary"] == cast["summary"]
        assert saved["content"]["characters"] == cast["characters"]

    _run_scenario(scenario, pipeline_test_database)


def test_characters_chain_repair_budget_locked_at_two_rounds(pipeline_test_database):
    """chain_repair 的预算锁 2：调用方给 max_iterations=9 也不放宽——两轮门都没过
    就带 findings 退出、绝不落库（调用方预算只对 agent 型生效）。"""
    cast = _load("cast.json")

    async def scenario(database) -> None:
        def _turn():
            return [{"name": "write_draft", "args": {"section": "source", "data": cast["source"]}},
                    {"name": "write_draft", "args": {"section": "lang", "data": cast["lang"]}},
                    {"name": "write_draft", "args": {"section": "summary", "data": cast["summary"]}},
                    {"name": "write_draft", "args": {"section": "characters", "data": []}},  # 空 cast 门必拦
                    {"name": "finish", "args": {}}]

        llm = ScriptedLLM([_turn(), _turn()])             # 修复轮也不修：门再拦一次
        result = await run_stage(database, "proj-cast-cap", "characters",
                                 {"novel_text": NOVEL_TEXT, "outline": _load("outline.json")},
                                 llm=llm, max_iterations=9)
        assert not result.ok and result.findings
        assert result.iterations == 2
        assert len(llm.calls) == 2                        # 第 3 轮不存在
        assert await pipeline_repository.get(database, "proj-cast-cap", "characters") is None

    _run_scenario(scenario, pipeline_test_database)


def test_staging_stage_agent_gate_fail_then_repair_saves(pipeline_test_database):
    """创作型（policy=agent）：写全 → finish 门拦（地点认图不足 4 件）→ 单键修复
    → 过门落库；上游 script/art 按 gate_needs 落盘成门的 --script/--art 实参。"""
    script = _load("script.json")
    art = _load("art.json")
    staging = _load("staging.json")
    thin = {**staging,
            "locations": [{**staging["locations"][0], "items": staging["locations"][0]["items"][:2]},
                          *staging["locations"][1:]]}

    async def scenario(database) -> None:
        def _round(pairs):
            return [{"name": "write_draft", "args": {"section": s, "data": d}} for s, d in pairs]

        turns = ([_round([("source", thin["source"]), ("lang", thin["lang"]),
                          ("locations", thin["locations"]), ("scenes", thin["scenes"])])
                  + [{"name": "finish", "args": {}}]]
                 + [_round([("locations", staging["locations"])])
                    + [{"name": "finish", "args": {}}]])
        llm = ScriptedLLM(turns)
        events: list[dict] = []
        result = await run_stage(database, "proj-stage", "staging",
                                 {"novel_text": NOVEL_TEXT, "script": script, "art": art},
                                 llm=llm, on_event=events.append)
        assert result.ok and result.artifact_type == "staging"
        assert result.iterations == 2                     # 写全+finish 一轮、修复+finish 一轮
        gate_events = [e for e in events if e["type"] == "gate"]
        assert [e["ok"] for e in gate_events] == [False, True]
        # 剧本对账真的发生在 inputs 上：staging 的 user 消息带 script/art 全文
        user = llm.calls[0][1]["content"]
        assert script["source"] in user and art["scenes"][0]["name"] in user
        saved = await pipeline_repository.get(database, "proj-stage", "staging")
        assert saved["content"]["locations"] == staging["locations"]
        assert saved["content"]["scenes"] == staging["scenes"]
        rows = await pipeline_repository.list_all(database, "proj-stage")
        assert rows[0]["source_run_id"] == "stage:staging"

    _run_scenario(scenario, pipeline_test_database)
