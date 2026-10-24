"""管线产物 API 路由测试（阶段 1 Task 3）：导入·查询·导出。

模式照抄本目录 conftest.py / tests/p0/conftest.py：不改业务代码，import server.app
前由目录级 conftest 设置环境变量（file 后端、内存任务存储），TestClient 走真实 app。

DB 复用 tests/pipeline/test_pipeline_repository.py 的 session 级 `pipeline_test_database`
夹具（dramaflow-postgres 容器的独立 pipeline_test 库）。asyncpg pool 绑定事件循环，
而 TestClient 每次进入 with 块都是新循环，所以每个用例装配独立 PostgresDatabase：
- 进入 with 后经 client.portal（app 自己的事件循环）惰性起池并 TRUNCATE；
- 退出前同路收池，不留悬挂连接。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.pipeline_artifacts import ARTIFACTS, get as get_artifact
from core.postgres import PostgresDatabase
from test_pipeline_repository import pipeline_test_database  # noqa: F401  Task 2 的 session 级 DB 夹具

ROOT_DIR = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT_DIR / "tests" / "fixtures" / "pipeline_run1"

# 文件名 → 产物类型：唯一事实来源是注册表（禁止在测试里散落映射）。
FILENAME_TO_TYPE = {art.filename: art.name for art in ARTIFACTS}
ALL_SEVEN = sorted(FILENAME_TO_TYPE)

BASE = "/api/pipeline/projects/proj-api/artifacts"
NO_DB_MESSAGE = "pipeline artifacts 需要 Postgres（compose.postgres.yml）"

# novel-outline SKILL.md frontmatter 的 version: 行（schema_version 推导锚点）。
OUTLINE_FRONTMATTER_VERSION = "2.0.0"


def _fixture(filename: str):
    return json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))


class _FakeLLM:
    """registry 只在 /api/health 里读 .model；会话链路本测试不触。"""

    model = "fake-model"

    async def simple(self, messages):
        return '{"worldview": "默认世界观"}'


def _build_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database):
    from fastapi.testclient import TestClient

    import server.app as app_module
    from core.canvas_repository import create_canvas_repository
    from core.session_repository import JsonlSessionRepository
    from server import pipeline as pipeline_module
    from server.registry import SessionRegistry

    monkeypatch.setattr(pipeline_module, "_database", database)

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    registry = SessionRegistry(
        llm=_FakeLLM(),
        workspace=str(workspace),
        canvas_repository=create_canvas_repository(workspace),
        session_repository=JsonlSessionRepository(tmp_path / "sessions"),
    )
    monkeypatch.setattr(app_module, "WORKSPACE", workspace)
    monkeypatch.setattr(app_module, "registry", registry)
    return TestClient(app_module.app)


@pytest.fixture()
def api_client(pipeline_test_database, tmp_path, monkeypatch):
    from server import pipeline as pipeline_module

    database = PostgresDatabase(pipeline_test_database, min_size=1, max_size=2)
    with _build_client(monkeypatch, tmp_path, database) as client:
        assert client.portal.call(pipeline_module.ensure_started)
        client.portal.call(database.pool.execute, "TRUNCATE pipeline_artifacts")
        yield SimpleNamespace(client=client)
        if database.pool is not None:  # 在 app 自己的循环里收池，不留悬挂连接
            client.portal.call(database.close)


@pytest.fixture()
def api_client_no_db(tmp_path, monkeypatch):
    """file 后端形态：无 Postgres 句柄 → 路由一律 503。"""
    with _build_client(monkeypatch, tmp_path, None) as client:
        yield SimpleNamespace(client=client)


# ---------------------------------------------------------------------------
# 快乐路径：7 类 fixture 导入 → 查询 → 导出往返保形
# ---------------------------------------------------------------------------


def test_import_all_seven_fixtures_then_query_and_export(api_client):
    client = api_client.client
    for filename in ALL_SEVEN:
        art_type = FILENAME_TO_TYPE[filename]
        payload = _fixture(filename)
        response = client.post(
            f"{BASE}/{art_type}", json={"content": payload, "source_run_id": "run-1"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["artifact_type"] == art_type
        assert body["schema_version"]  # frontmatter 推导，非空
        assert body["findings"] == []  # fixture 全部契合契约字段表，无告警
        assert isinstance(body["updated_at"], float) and body["updated_at"] > 0

    listed = client.get(BASE).json()["artifacts"]
    assert {row["artifact_type"] for row in listed} == set(FILENAME_TO_TYPE.values())
    assert len(listed) == 7
    for row in listed:
        assert row["schema_version"]
        assert isinstance(row["updated_at"], float)
        assert set(row) == {"artifact_type", "schema_version", "updated_at"}  # 列表不带 content

    for filename in ALL_SEVEN:
        art_type = FILENAME_TO_TYPE[filename]
        payload = _fixture(filename)
        # 契约"存储与导入约定"：character_refs 的 asset.json 是单角色文件，
        # 落库自动包装为 {"characters": [文件对象]}；其余类型原样落库。
        expected = {"characters": [payload]} if art_type == "character_refs" else payload
        got = client.get(f"{BASE}/{art_type}")
        assert got.status_code == 200, got.text
        row = got.json()
        assert set(row) == {
            "artifact_type", "schema_version", "content", "source_run_id", "updated_at"
        }
        assert row["content"] == expected  # 单条查询：content 与落库形状保形
        assert row["source_run_id"] == "run-1"

        export = client.get(f"{BASE}/{art_type}/export")
        assert export.status_code == 200, export.text
        assert export.headers["content-type"].startswith("application/json")
        assert export.headers["content-disposition"] == (
            f"attachment; filename={get_artifact(art_type).filename}"
        )
        assert json.loads(export.content) == expected  # 导出 body = content 原样


def test_list_artifacts_empty_project(api_client):
    client = api_client.client
    response = client.get(BASE)
    assert response.status_code == 200
    assert response.json() == {"artifacts": []}


# ---------------------------------------------------------------------------
# characters：单角色对象自动包装 → 全量导入合并不丢角色
# ---------------------------------------------------------------------------


def test_characters_single_card_wraps_then_full_fixture_merges(api_client):
    client = api_client.client
    cast = _fixture("cast.json")
    first = cast["characters"][0]

    single = client.post(f"{BASE}/characters", json={"content": first})
    assert single.status_code == 200, single.text
    got = client.get(f"{BASE}/characters").json()
    assert got["content"] == {"characters": [first]}  # 单卡自动包装为合并形状

    full = client.post(f"{BASE}/characters", json={"content": cast})
    assert full.status_code == 200, full.text
    got = client.get(f"{BASE}/characters").json()
    assert got["content"] == cast  # 全量导入后与 fixture 一致：先导卡片不丢、他卡保留


# ---------------------------------------------------------------------------
# script：按集合并——重导第 2 集，1/3 原样保留
# ---------------------------------------------------------------------------


def test_script_batch_reimport_updates_only_episode_2(api_client):
    client = api_client.client
    script = _fixture("script.json")
    assert [e["ep"] for e in script["episodes"]] == [1, 2, 3]

    first = client.post(f"{BASE}/script", json={"content": script})
    assert first.status_code == 200, first.text

    mutated = copy.deepcopy(script["episodes"][1])
    mutated["hook"] = "改写后的第 2 集钩子"
    reimport = client.post(f"{BASE}/script", json={"content": {"episodes": [mutated]}})
    assert reimport.status_code == 200, reimport.text

    content = client.get(f"{BASE}/script").json()["content"]
    assert [e["ep"] for e in content["episodes"]] == [1, 2, 3]  # 位置不变
    by_ep = {e["ep"]: e for e in content["episodes"]}
    assert by_ep[1] == script["episodes"][0]  # 未出现的集逐字节保留
    assert by_ep[3] == script["episodes"][2]
    assert by_ep[2] == mutated  # 同号集被新对象整替换


# ---------------------------------------------------------------------------
# 幂等 / schema_version
# ---------------------------------------------------------------------------


def test_idempotent_reimport_same_payload(api_client):
    client = api_client.client
    outline = _fixture("outline.json")
    first = client.post(f"{BASE}/outline", json={"content": outline})
    second = client.post(f"{BASE}/outline", json={"content": outline})
    assert first.status_code == second.status_code == 200
    assert second.json()["schema_version"] == first.json()["schema_version"]  # 版本稳定
    assert second.json()["updated_at"] >= first.json()["updated_at"]  # 时间戳不回退
    row = client.get(f"{BASE}/outline").json()
    assert row["content"] == outline  # 内容不变


def test_schema_version_frontmatter_anchor_and_explicit_override(api_client):
    client = api_client.client
    outline = _fixture("outline.json")

    derived = client.post(f"{BASE}/outline", json={"content": outline})
    assert derived.status_code == 200
    assert derived.json()["schema_version"] == OUTLINE_FRONTMATTER_VERSION  # 无版本 → frontmatter

    explicit = client.post(
        f"{BASE}/outline", json={"content": outline, "schema_version": "9.9.9-test"}
    )
    assert explicit.status_code == 200
    assert explicit.json()["schema_version"] == "9.9.9-test"  # 显式版本胜出
    assert client.get(f"{BASE}/outline").json()["schema_version"] == "9.9.9-test"

    blank = client.post(f"{BASE}/outline", json={"content": outline, "schema_version": ""})
    assert blank.status_code == 200
    assert blank.json()["schema_version"] == OUTLINE_FRONTMATTER_VERSION  # 空串视同未提供


# ---------------------------------------------------------------------------
# 非法输入
# ---------------------------------------------------------------------------


def test_staging_top_level_array_rejected_with_findings(api_client):
    client = api_client.client
    response = client.post(f"{BASE}/staging", json={"content": [{"scene": "x"}]})
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["message"]
    assert body["findings"]  # 422 的 findings 保证非空


def test_unknown_artifact_type_404(api_client):
    client = api_client.client
    assert client.post(f"{BASE}/nonexistent", json={"content": {}}).status_code == 404
    assert client.get(f"{BASE}/nonexistent").status_code == 404
    assert client.get(f"{BASE}/nonexistent/export").status_code == 404


def test_content_missing_is_422(api_client):
    client = api_client.client
    assert client.post(f"{BASE}/outline", json={}).status_code == 422
    assert client.post(f"{BASE}/outline", json={"source_run_id": "run-1"}).status_code == 422


def test_missing_artifact_404(api_client):
    client = api_client.client
    assert client.get(f"{BASE}/outline").status_code == 404
    assert client.get(f"{BASE}/outline/export").status_code == 404


# ---------------------------------------------------------------------------
# 无 Postgres（file 后端）→ 503
# ---------------------------------------------------------------------------


def test_no_database_returns_503(api_client_no_db):
    client = api_client_no_db.client
    post = client.post(f"{BASE}/outline", json={"content": {"source": "x"}})
    assert post.status_code == 503
    assert post.json() == {"message": NO_DB_MESSAGE}
    assert client.get(BASE).status_code == 503
    assert client.get(f"{BASE}/outline").status_code == 503
    assert client.get(f"{BASE}/outline/export").status_code == 503


# ---------------------------------------------------------------------------
# 回归护栏：真实 app 仍可装配、既有端点不受影响
# ---------------------------------------------------------------------------


def test_app_health_smoke(api_client):
    assert api_client.client.get("/api/health").status_code == 200
