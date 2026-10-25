"""技能执行器测试（Task 2）：gated agent loop + outline 阶段端到端。

两层"真"夹一层"假"：
- DB 是真库——夹具装配照抄 test_pipeline_repository.py 的 pipeline_test 惯例
  （session 级 CREATE DATABASE + 手工跑 0005/0006 幂等迁移；每用例独立
  asyncio.run + 独立 pool + TRUNCATE，asyncpg pool 绑定事件循环，必须与用例
  同循环，所以不用 pytest.mark.asyncio——本仓库未装 pytest-asyncio）。
- 质量门是真 node 子进程（novel-outline validate --stage full），14 道门判定、
  draft 组装、"门不过不落库"的约束全部是真的。
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
from core.postgres import PostgresDatabase
from core.skill_executor import run_stage
from test_pipeline_repository import pipeline_test_database  # noqa: F401  session 级库装配

ROOT_DIR = Path(__file__).resolve().parents[2]
FIXTURE_OUTLINE = ROOT_DIR / "tests" / "fixtures" / "pipeline_run1" / "outline.json"
NOVEL_TEXT = (ROOT_DIR / "tests" / "fixtures" / "sample_novel_v1.txt").read_text(encoding="utf-8")

# write_draft 的 section 序列 = 大纲顶层键：元数据三件 + 六节（与 STAGE_SPECS 对齐）。
SECTION_ORDER = ("source", "lang", "params", "adaptation", "characters", "scenes", "props", "beats", "episodes")


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
