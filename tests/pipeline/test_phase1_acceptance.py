"""阶段 1 端到端验收联测（Task 8）：一条龙锁定导入链路契约。

场景与顺序（Task 8 brief）：7 类 fixtures 依序导入同一画布 `phase1-acceptance`
→ 列表恰好 7 类 → outline/script/cast 内容与 fixtures 逐字段一致 →
PUT 集 2 正文（edited_text 覆盖语义）→ characters 单卡再导入不丢卡 →
非法 payload 拒绝（staging 顶层数组 422、未知类型 404）→ outline 导出往返 →
跨产物集数一致性（断言函数化，阶段 2 复用为导入告警）→ 全新画布重跑整个
导入序列，结果逐字段一致（场景级幂等，时间戳除外）。

夹具与客户端装配照抄 test_pipeline_api.py：不改业务代码，import server.app
前由目录级 conftest 设置环境变量；DB 复用 test_pipeline_repository 的
session 级 `pipeline_test_database` 夹具（dramaflow-postgres 的 pipeline_test 库），
每用例装配独立 PostgresDatabase 并 TRUNCATE，收池不留悬挂连接。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.pipeline_artifacts import ARTIFACTS
from core.pipeline_import import derive_episode_text
from core.postgres import PostgresDatabase
from test_pipeline_repository import pipeline_test_database  # noqa: F401  session 级 DB 夹具

ROOT_DIR = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT_DIR / "tests" / "fixtures" / "pipeline_run1"

# 文件名 → 产物类型：唯一事实来源是注册表（与 test_pipeline_api.py 同源）。
FILENAME_TO_TYPE = {art.filename: art.name for art in ARTIFACTS}
ALL_SEVEN = sorted(FILENAME_TO_TYPE)

CANVAS = "phase1-acceptance"
BASE = f"/api/pipeline/projects/{CANVAS}/artifacts"
SOURCE_RUN_ID = "run-acceptance"
ACCEPTANCE_TEXT = "验收替换正文"


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


# ---------------------------------------------------------------------------
# 跨产物一致性断言（Task 8 定案）：函数化，阶段 2 导入告警直接复用
# ---------------------------------------------------------------------------


def assert_cross_artifact_consistency(outline: dict, script: dict, storyboard: dict) -> None:
    """大纲/剧本/分镜三层的集数必须一致。

    字段路径（docs/pipeline/data-contract.md）：
    - outline `params.episodes`（总集数，集数参数的唯一事实源）；
    - script `episodes[].ep`（剧本正文，每集一条）；
    - storyboard `episodes[].ep`（分镜，每集一条，按 ep 去重计数）。
    断言：集数三处相等，且剧本/分镜覆盖的集号集合一致。
    """
    expected = outline["params"]["episodes"]
    script_eps = [e["ep"] for e in script["episodes"]]
    storyboard_eps = [e["ep"] for e in storyboard["episodes"]]
    assert expected == len(script_eps) == len(set(storyboard_eps)), (
        f"集数不一致：outline.params.episodes={expected}，"
        f"script={len(script_eps)} 集，storyboard 去重={len(set(storyboard_eps))} 集"
    )
    assert set(script_eps) == set(storyboard_eps), (
        f"剧本/分镜集号集合不一致：script={sorted(set(script_eps))}，"
        f"storyboard={sorted(set(storyboard_eps))}"
    )


# ---------------------------------------------------------------------------
# 场景级幂等：完整导入序列在全新画布上重跑，结果逐字段一致
# ---------------------------------------------------------------------------


def _run_import_sequence(client, canvas: str) -> dict:
    """在指定画布跑完整导入序列（7 类导入 + 集 2 编辑 + 单卡再导入），
    返回可比较结果：每步导入响应（不含 updated_at）+ 终态列表 + 7 类 content。"""
    base = f"/api/pipeline/projects/{canvas}/artifacts"
    steps = []
    for filename in ALL_SEVEN:
        art_type = FILENAME_TO_TYPE[filename]
        response = client.post(
            f"{base}/{art_type}",
            json={"content": _fixture(filename), "source_run_id": SOURCE_RUN_ID},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        steps.append((art_type, body["schema_version"], tuple(body["findings"])))
    assert client.put(f"{base}/script/episodes/2", json={"text": ACCEPTANCE_TEXT}).status_code == 200
    assert client.post(
        f"{base}/characters", json={"content": _fixture("cast.json")["characters"][0]}
    ).status_code == 200
    listed = client.get(base).json()["artifacts"]
    listing = sorted((row["artifact_type"], row["schema_version"]) for row in listed)
    contents = {
        art_type: client.get(f"{base}/{art_type}").json()["content"]
        for art_type in sorted(FILENAME_TO_TYPE.values())
    }
    return {"steps": steps, "listing": listing, "contents": contents}


# ---------------------------------------------------------------------------
# 一条龙验收主场景（Task 8 brief 顺序逐步落断言）
# ---------------------------------------------------------------------------


def test_phase1_acceptance_end_to_end(api_client):
    client = api_client.client
    fixtures = {art_type: _fixture(filename) for filename, art_type in FILENAME_TO_TYPE.items()}

    # 1. 依序导入 7 类 fixtures → 全 200，无告警
    for filename in ALL_SEVEN:
        art_type = FILENAME_TO_TYPE[filename]
        response = client.post(
            f"{BASE}/{art_type}",
            json={"content": _fixture(filename), "source_run_id": SOURCE_RUN_ID},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["artifact_type"] == art_type
        assert body["schema_version"]
        assert body["findings"] == []

    # 2. 列表 → 恰好 7 类
    listed = client.get(BASE)
    assert listed.status_code == 200
    rows = listed.json()["artifacts"]
    assert {row["artifact_type"] for row in rows} == set(FILENAME_TO_TYPE.values())
    assert len(rows) == 7

    # 3. outline / script / cast 内容与 fixtures 逐字段一致
    outline_row = client.get(f"{BASE}/outline")
    assert outline_row.status_code == 200
    assert outline_row.json()["content"] == fixtures["outline"]

    cast_row = client.get(f"{BASE}/characters")
    assert cast_row.status_code == 200
    assert cast_row.json()["content"] == fixtures["characters"]

    script_row = client.get(f"{BASE}/script")
    assert script_row.status_code == 200
    # 详情响应额外带 display-only 的逐集正文视图 episodes（不落库），
    # content 仍与落库形状保形（契约 script 节 + Task 5 定案）；
    # 0008 起响应还带分级门标记 findings（Task 3 定案，导入路径为空表）。
    assert set(script_row.json()) == {
        "artifact_type", "schema_version", "content", "source_run_id", "updated_at",
        "episodes", "findings",
    }
    assert script_row.json()["findings"] == []
    assert script_row.json()["content"] == fixtures["script"]
    assert [e["ep"] for e in script_row.json()["episodes"]] == [1, 2, 3]

    # 4. PUT 集 2 正文：edited_text 覆盖语义，他集仍是 fixture 派生正文
    put = client.put(f"{BASE}/script/episodes/2", json={"text": ACCEPTANCE_TEXT})
    assert put.status_code == 200, put.text
    detail = client.get(f"{BASE}/script").json()
    by_ep = {e["ep"]: e for e in detail["episodes"]}
    assert by_ep[2]["text"] == ACCEPTANCE_TEXT
    assert by_ep[2]["edited"] is True
    fixture_by_ep = {e["ep"]: e for e in fixtures["script"]["episodes"]}
    for ep in (1, 3):
        assert by_ep[ep]["edited"] is False
        assert by_ep[ep]["text"] == derive_episode_text(fixture_by_ep[ep])
    stored_by_ep = {e["ep"]: e for e in detail["content"]["episodes"]}
    assert stored_by_ep[2]["edited_text"] == ACCEPTANCE_TEXT
    assert "edited_text" not in stored_by_ep[1]

    # 5. characters 单卡再导入：前卡与他卡全部保留（逐角色合并不丢卡）
    first_card = fixtures["characters"]["characters"][0]
    single = client.post(f"{BASE}/characters", json={"content": first_card})
    assert single.status_code == 200, single.text
    cast_after = client.get(f"{BASE}/characters").json()["content"]
    assert cast_after == fixtures["characters"]
    names = [card["name"] for card in cast_after["characters"]]
    assert first_card["name"] in names
    assert len(names) == len(fixtures["characters"]["characters"])

    # 6. 非法 payload：staging 顶层数组 → 422 且 findings 非空；未知类型 → 404
    bad = client.post(f"{BASE}/staging", json={"content": [{"scene": "x"}]})
    assert bad.status_code == 422, bad.text
    assert bad.json()["findings"]
    assert client.post(f"{BASE}/nonexistent", json={"content": {}}).status_code == 404

    # 7. 导出往返：outline/export 解析后与 fixture 深等
    export = client.get(f"{BASE}/outline/export")
    assert export.status_code == 200, export.text
    assert export.headers["content-type"].startswith("application/json")
    assert json.loads(export.content) == fixtures["outline"]

    # 8. 跨产物一致性：经 API 取回的 outline/script/storyboard 集数一致
    outline = client.get(f"{BASE}/outline").json()["content"]
    script = client.get(f"{BASE}/script").json()["content"]
    storyboard = client.get(f"{BASE}/storyboard").json()["content"]
    assert outline["params"]["episodes"] == 3  # fixtures 为 3 集
    assert_cross_artifact_consistency(outline, script, storyboard)


def test_phase1_acceptance_import_sequence_deterministic(api_client):
    """场景级幂等：整个导入序列在全新画布重跑，结果逐字段一致（时间戳除外）。"""
    client = api_client.client
    first = _run_import_sequence(client, "phase1-acceptance-run1")
    second = _run_import_sequence(client, "phase1-acceptance-run2")
    assert second == first

    # 两个画布互不串扰：各自恰好 7 类
    for canvas in ("phase1-acceptance-run1", "phase1-acceptance-run2"):
        rows = client.get(f"/api/pipeline/projects/{canvas}/artifacts").json()["artifacts"]
        assert len(rows) == 7
