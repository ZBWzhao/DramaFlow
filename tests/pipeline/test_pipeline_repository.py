"""pipeline_artifacts 表仓库层测试（Task 2 brief Step 4，真库）。

DB 夹具惯例：本分支 tests/ 此前没有 DB 夹具（tests/p0 走零依赖替身），
这里按阶段 1 约定连 dramaflow-postgres 容器的独立库 `pipeline_test`：

- session 级一次性引导：连 db dramaflow，CREATE DATABASE pipeline_test
  （DuplicateDatabaseError 视为已存在），再手工执行 migrations/0005_pipeline_artifacts.sql。
  不能走 PostgresDatabase.start() 的全量 migrate——0002/0003 会 ALTER 既有
  canvases/outbox_events 表，全新库上会失败，故 pool 直接装配（等价于 start() 的产物）。
- 每用例独立 asyncio.run + 独立 pool：asyncpg pool 绑定事件循环，必须与用例体
  同循环；用例前 TRUNCATE pipeline_artifacts 保证零污染。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

try:
    import asyncpg
except ImportError:  # pragma: no cover - 与 core/postgres.py 同样的部署错误
    asyncpg = None

from core.pipeline_artifacts import ARTIFACTS
from core.postgres import PostgresDatabase
from core.pipeline_repository import delete, get, list_all, save

ROOT_DIR = Path(__file__).resolve().parents[2]
MIGRATION_0005 = ROOT_DIR / "migrations" / "0005_pipeline_artifacts.sql"

TEST_DB_NAME = "pipeline_test"
TEST_DSN = "postgresql://postgres:postgres@127.0.0.1:5432/pipeline_test"


def _bootstrap_database() -> None:
    async def _run() -> None:
        admin = await asyncpg.connect(
            "postgresql://postgres:postgres@127.0.0.1:5432/dramaflow"
        )
        try:
            try:
                await admin.execute(f"CREATE DATABASE {TEST_DB_NAME}")
            except asyncpg.DuplicateDatabaseError:
                pass
        finally:
            await admin.close()
        connection = await asyncpg.connect(TEST_DSN)
        try:
            await connection.execute(MIGRATION_0005.read_text(encoding="utf-8"))
        finally:
            await connection.close()

    asyncio.run(_run())


@pytest.fixture(scope="session")
def pipeline_test_database() -> str:
    if asyncpg is None:
        pytest.fail("未安装 asyncpg，无法运行仓库层真库测试")
    _bootstrap_database()
    return TEST_DSN


def _run_scenario(scenario) -> None:
    """在同一个 asyncio.run 里装配 pool + TRUNCATE + 跑用例 + 关 pool。"""

    async def _runner() -> None:
        database = PostgresDatabase(TEST_DSN, min_size=1, max_size=2)
        database.pool = await asyncpg.create_pool(
            TEST_DSN, min_size=1, max_size=2, command_timeout=30
        )
        try:
            await database.pool.execute("TRUNCATE pipeline_artifacts")
            await scenario(database)
        finally:
            await database.pool.close()

    asyncio.run(_runner())


SAMPLE_OUTLINE = {
    "source": "测试书名",
    "params": {"episodes": 12, "genre": "古装"},
    "adaptation": {"core": "一句话", "keep": [{"what": "主线", "why": "保留"}], "cut": [], "risks": []},
    "characters": [{"id": "C01", "name": "阿禾", "role": "主角", "tier": "lead"}],
    "scenes": [{"id": "S01", "name": "茶山", "primary": True}],
    "beats": [{"id": "B01", "type": "打脸", "episode": 1, "setup": "起", "payoff": "收"}],
    "episodes": [{"ep": 1, "synopsis": "梗概", "hook": "钩子", "suspense": "悬念"}],
}


def test_save_get_roundtrip_preserves_jsonb(pipeline_test_database):
    async def scenario(database) -> None:
        saved = await save(database, "proj-1", "outline", SAMPLE_OUTLINE)
        assert saved["content"] == SAMPLE_OUTLINE  # 中文与嵌套结构保形
        assert saved["schema_version"] == ""  # schema_version 由 API 层负责，save 不设
        assert isinstance(saved["updated_at"], float) and saved["updated_at"] > 0

        row = await get(database, "proj-1", "outline")
        # Task 8 起 get() 带出 source_run_id（改稿脑回滚要按旧 provenance 原样写回）
        assert row == {
            "content": SAMPLE_OUTLINE,
            "schema_version": "",
            "source_run_id": "",
            "updated_at": saved["updated_at"],
        }

    _run_scenario(scenario)


def test_save_same_key_overwrites_latest_wins(pipeline_test_database):
    async def scenario(database) -> None:
        first = {"source": "旧稿", "episodes": [{"ep": 1}]}
        second = {"source": "新稿", "episodes": [{"ep": 1}, {"ep": 2}]}
        await save(database, "proj-1", "script", first, source_run_id="run-old")
        await save(database, "proj-1", "script", second, source_run_id="run-new")
        row = await get(database, "proj-1", "script")
        assert row["content"] == second
        rows = await list_all(database, "proj-1")
        assert len(rows) == 1  # upsert：同 (project_id, artifact_type) 只有一行
        assert rows[0]["source_run_id"] == "run-new"

    _run_scenario(scenario)


def test_get_missing_row_returns_none(pipeline_test_database):
    async def scenario(database) -> None:
        assert await get(database, "no-such-project", "outline") is None

    _run_scenario(scenario)


def test_list_all_returns_seven_types_ordered_by_type(pipeline_test_database):
    async def scenario(database) -> None:
        for art in ARTIFACTS:
            await save(database, "proj-1", art.name, {"_type": art.name})
        rows = await list_all(database, "proj-1")
        assert [row["artifact_type"] for row in rows] == sorted(a.name for a in ARTIFACTS)
        assert len(rows) == 7
        by_type = {row["artifact_type"]: row for row in rows}
        assert by_type["staging"]["content"] == {"_type": "staging"}
        assert isinstance(rows[0]["updated_at"], float)

    _run_scenario(scenario)


def test_list_all_isolated_per_project(pipeline_test_database):
    async def scenario(database) -> None:
        await save(database, "proj-1", "outline", SAMPLE_OUTLINE)
        await save(database, "proj-2", "art", {"source": "另一项目"})
        assert len(await list_all(database, "proj-1")) == 1
        assert len(await list_all(database, "proj-2")) == 1
        assert (await list_all(database, "proj-2"))[0]["artifact_type"] == "art"

    _run_scenario(scenario)


def test_delete_returns_true_then_false(pipeline_test_database):
    async def scenario(database) -> None:
        await save(database, "proj-1", "characters", {"characters": [{"name": "阿禾"}]})
        assert await delete(database, "proj-1", "characters") is True
        assert await get(database, "proj-1", "characters") is None
        assert await delete(database, "proj-1", "characters") is False

    _run_scenario(scenario)


def test_source_run_id_defaults_to_empty(pipeline_test_database):
    async def scenario(database) -> None:
        await save(database, "proj-1", "art", {"source": "剧名"})
        row = (await list_all(database, "proj-1"))[0]
        assert row["source_run_id"] == ""

    _run_scenario(scenario)
