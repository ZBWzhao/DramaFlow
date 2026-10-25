"""管线 → 资产库 同步桥测试（阶段 2 Task 7）。

模式照抄本目录 test_pipeline_api.py：真 app + 真 pipeline_artifacts 库
（dramaflow-postgres 容器的 pipeline_test 库，复用其 session 级夹具）+ file
画布仓库。资产库行直接种在画布上（资产库 = 画布 asset 节点，库语义由
core.asset_tables.merge_asset_tables 保证）。

核心回归点（brief Step 3）：merge 语义——只新增库里不存在的行；已存在行的
字段一律不覆盖（description/bio 非空时 fill_fields 不生效）；再同步幂等
（added=0，不重复建）。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.postgres import PostgresDatabase
from test_pipeline_repository import pipeline_test_database  # noqa: F401  session 级 DB 夹具
from conftest import make_project

ROOT_DIR = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT_DIR / "tests" / "fixtures" / "pipeline_run1"

NO_DB_MESSAGE = "pipeline artifacts 需要 Postgres（compose.postgres.yml）"


def _fixture(filename: str):
    return json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))


class _FakeLLM:
    """桥路由不触 LLM；registry 兜底用。"""

    model = "fake-model"

    async def simple(self, messages):
        return '{"worldview": "默认世界观"}'


def _build_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database):
    from fastapi.testclient import TestClient

    import server.app as app_module
    from core.canvas_repository import create_canvas_repository
    from core.session_repository import JsonlSessionRepository
    from server import pipeline as pipeline_module
    from server import studio as studio_module
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
    # 桥路由的写侧（studio_api._registry / _load / _apply_asset_sync_plan）走
    # studio 模块句柄，必须与本测试的临时环境重新装配。
    studio_module.configure(registry=registry, workspace=workspace, media_registry=None)
    return TestClient(app_module.app), registry


@pytest.fixture()
def bridge(pipeline_test_database, tmp_path, monkeypatch):
    from server import pipeline as pipeline_module

    database = PostgresDatabase(pipeline_test_database, min_size=1, max_size=2)
    client, registry = _build_client(monkeypatch, tmp_path, database)
    with client:
        assert client.portal.call(pipeline_module.ensure_started)
        client.portal.call(database.pool.execute, "TRUNCATE pipeline_artifacts")
        yield SimpleNamespace(client=client, registry=registry)
        if database.pool is not None:
            client.portal.call(database.close)


@pytest.fixture()
def bridge_no_db(tmp_path, monkeypatch):
    """file 后端形态：无 Postgres 句柄 → 桥读不到产物，503 {"message"}。"""
    client, _registry = _build_client(monkeypatch, tmp_path, None)
    with client:
        yield SimpleNamespace(client=client)


def _seed_asset(env, canvas: str, name: str, *, kind: str, description: str = "", bio: str = "", looks: list | None = None) -> str:
    """在画布资产库种一行既有资产，返回 node_id。"""

    async def _seed() -> str:
        graph = await env.registry.canvas_repository.load(canvas)
        data = {"kind": kind, "name": name, "description": description}
        if bio:
            data["bio"] = bio
        node = graph.add_node("asset", name, data=data)
        if looks:
            # add_node 对 data 是浅拷贝：回写落在返回的 node 上
            node.data["looks"] = list(looks)
        await env.registry.canvas_repository.save(graph)
        return node.id

    return env.client.portal.call(_seed)


def _asset_rows(env, canvas: str) -> list[dict]:
    """画布资产库投影（kind/name/description/bio/形象名列表）。"""

    async def _read() -> list[dict]:
        graph = await env.registry.canvas_repository.load(canvas)
        return [
            {
                "kind": str(node.data.get("kind") or ""),
                "name": str(node.data.get("name") or node.label or ""),
                "description": str(node.data.get("description") or ""),
                "bio": str(node.data.get("bio") or ""),
                "looks": [str(lk.get("name") or "") for lk in (node.data.get("looks") or [])],
            }
            for node in graph.nodes.values()
            if node.type == "asset"
        ]

    return env.client.portal.call(_read)


def _import(client, canvas: str, artifact_type: str, content) -> None:
    response = client.post(
        f"/api/pipeline/projects/{canvas}/artifacts/{artifact_type}",
        json={"content": content, "source_run_id": "run-brain"},
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# characters：只新增不存在的行；已存在行字段不被覆盖（merge 语义回归）
# ---------------------------------------------------------------------------


def test_characters_sync_adds_missing_never_overwrites(bridge):
    env = bridge
    client = env.client
    canvas = make_project(client, title="桥接角色")["canvas"]
    cast = _fixture("cast.json")
    cards = cast["characters"]
    existing = cards[0]
    seeded_id = _seed_asset(
        env, canvas, existing["name"], kind="character",
        description="种子身份——不许覆盖", bio="种子外形——不许覆盖",
        looks=[{"id": "look_seed_1", "name": "种子形象", "prompt": ""}],
    )

    _import(client, canvas, "characters", cast)
    response = client.post(f"/api/pipeline/projects/{canvas}/artifacts/characters/sync-assets")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"message", "added"}
    # cast 卡没有 looks 字段 → added = 新建资产数；种子行已存在 → 少一张
    assert body["added"] == len(cards) - 1
    assert "新增资产" in body["message"]

    rows = {row["name"]: row for row in _asset_rows(env, canvas)}
    assert len(rows) == len(cards)
    kept = rows[existing["name"]]
    assert kept["description"] == "种子身份——不许覆盖"  # 非空字段不被覆盖
    assert kept["bio"] == "种子外形——不许覆盖"
    assert kept["looks"] == ["种子形象"]  # 形象不增不改
    others = [name for name in rows if name != existing["name"]]
    assert len(others) == len(cards) - 1
    for name in others:
        row = rows[name]
        assert row["kind"] == "character"
        assert row["description"] or row["bio"]  # persona 取值路径生效，非全空行

    # 幂等：再同步一次不重复创建（只增不重复）
    again = client.post(f"/api/pipeline/projects/{canvas}/artifacts/characters/sync-assets")
    assert again.status_code == 200
    assert again.json()["added"] == 0
    assert "无需新增" in again.json()["message"]
    assert len(_asset_rows(env, canvas)) == len(cards)


# ---------------------------------------------------------------------------
# art：scenes/props 映射入库；同名词段不覆盖
# ---------------------------------------------------------------------------


def test_art_sync_maps_scenes_and_props(bridge):
    env = bridge
    client = env.client
    canvas = make_project(client, title="桥接美术")["canvas"]
    art = _fixture("art.json")
    first_scene = art["scenes"][0]
    _seed_asset(env, canvas, first_scene["name"], kind="scene", description="旧场景描述——保留")

    _import(client, canvas, "art", art)
    response = client.post(f"/api/pipeline/projects/{canvas}/artifacts/art/sync-assets")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"message", "added"}
    assert body["added"] == (len(art["scenes"]) - 1) + len(art.get("props", []))

    rows = {row["name"]: row for row in _asset_rows(env, canvas)}
    kept = rows[first_scene["name"]]
    assert kept["kind"] == "scene"
    assert kept["description"] == "旧场景描述——保留"  # 只增不改：已有描述一字不动
    # 其余场景入库，description = summary（设计意图）
    for scene in art["scenes"][1:]:
        row = rows[scene["name"]]
        assert row["kind"] == "scene"
        assert row["description"] == str(scene.get("summary") or "")
    # 道具入库，description = summary（戏剧功能）
    for prop in art.get("props", []):
        row = rows[prop["name"]]
        assert row["kind"] == "prop"
        assert row["description"] == str(prop.get("summary") or "")

    again = client.post(f"/api/pipeline/projects/{canvas}/artifacts/art/sync-assets")
    assert again.json()["added"] == 0


# ---------------------------------------------------------------------------
# 映射函数：取值路径按契约校正 + 截断与过滤（纯函数，不触 client）
# ---------------------------------------------------------------------------


def test_characters_to_rows_paths_clips_and_skips():
    from server.pipeline_assets import characters_to_rows

    content = {
        "characters": [
            {
                "name": "甲" * 70,  # 超 60 截断（CharacterRow 上限）
                "oneLiner": "兜底画像",
                "persona": {"identity": "夜班司机", "appearance": "高瘦" * 900},  # >1500
            },
            {"name": "乙", "oneLiner": "只有画像没有身份"},  # persona 缺 → 回落 oneLiner
            {"name": "   "},  # 无名卡跳过
            "不是对象",  # 脏形状跳过
        ],
    }
    rows = characters_to_rows(content)
    assert [row["name"] for row in rows] == ["甲" * 60, "乙"]
    assert rows[0]["description"] == "夜班司机"  # identity 优先于 oneLiner
    assert len(rows[0]["bio"]) == 1500  # 超长截断到 CharacterRow 上限
    assert rows[1]["description"] == "只有画像没有身份"  # persona 缺 → 回落 oneLiner
    assert rows[1]["bio"] == ""


def test_art_to_rows_maps_summary_only():
    from server.pipeline_assets import art_to_rows

    content = {
        "scenes": [
            {"name": "天台", "summary": "对峙高地"},
            {"name": "", "summary": "无名跳过"},
        ],
        "props": [{"name": "旧手机", "summary": "证据载体"}],
    }
    rows = art_to_rows(content)
    assert rows == {
        "scenes": [{"name": "天台", "description": "对峙高地"}],
        "props": [{"name": "旧手机", "description": "证据载体"}],
    }


# ---------------------------------------------------------------------------
# 错误形态：未知类型 / 缺产物 / 缺画布 → 404；file 后端 → 503；空行 → added 0
# ---------------------------------------------------------------------------


def test_sync_assets_error_shapes(bridge):
    env = bridge
    client = env.client
    canvas = make_project(client, title="桥接错误形态")["canvas"]

    # 可同步类型之外的注册类型也按未知类型 404（桥只开 characters|art 两个口）
    r = client.post(f"/api/pipeline/projects/{canvas}/artifacts/outline/sync-assets")
    assert r.status_code == 404
    assert r.json() == {"message": "未知产物类型：outline"}
    r = client.post(f"/api/pipeline/projects/{canvas}/artifacts/nonexistent/sync-assets")
    assert r.status_code == 404

    # 缺产物 → 404
    r = client.post(f"/api/pipeline/projects/{canvas}/artifacts/characters/sync-assets")
    assert r.status_code == 404
    assert r.json() == {"message": "产物不存在：characters"}

    # 画布不存在 → 404（沿用制片台 _load 形态）
    _import(client, "ghost-canvas", "characters", {"characters": [{"name": "某人"}]})
    r = client.post("/api/pipeline/projects/ghost-canvas/artifacts/characters/sync-assets")
    assert r.status_code == 404

    # 产物里没有可入库的行 → 200 + added 0（不产生脏数据）
    _import(client, canvas, "characters", {"source": "x", "lang": "zh", "characters": []})
    r = client.post(f"/api/pipeline/projects/{canvas}/artifacts/characters/sync-assets")
    assert r.status_code == 200
    assert r.json() == {"message": "「characters」产物里没有可入库的行", "added": 0}


def test_sync_assets_without_database_503(bridge_no_db):
    client = bridge_no_db.client
    response = client.post("/api/pipeline/projects/whatever/artifacts/characters/sync-assets")
    assert response.status_code == 503
    assert response.json() == {"message": NO_DB_MESSAGE}
