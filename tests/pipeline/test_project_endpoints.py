"""Skill 清单 / 项目 Skill 选择 / get_project 扩展 端点测试。"""
from conftest import make_project


def test_skills_endpoint_lists_three_roles(pipeline):
    r = pipeline.client.get("/api/skills")
    assert r.status_code == 200
    skills = r.json()["skills"]
    roles = {s["role"] for s in skills}
    assert {"script-analysis", "director", "asset"} <= roles
    for s in skills:
        assert s["name"] and s["version"] and s["description"]


def test_create_project_stores_skills_and_auto_flag(pipeline):
    body = make_project(
        pipeline.client,
        title="选择技能",
        skills={"script_analysis": "script-analysis@1.0.0", "director": "director@2.0.0", "asset": "asset@2.0.0"},
        auto=True,
    )
    assert body["ok"] is True
    canvas = body["canvas"]
    r = pipeline.client.get(f"/api/studio/project?canvas={canvas}")
    assert r.status_code == 200
    data = r.json()
    assert data["skills"]["script_analysis"] == "script-analysis@1.0.0"
    assert data["project"]["auto_asset_table"] is True
    assert data["story_analysis"] == {}
    assert data["asset_tables"] == {}


def test_project_skills_endpoint_rejects_unknown_skill(pipeline):
    body = make_project(pipeline.client, title="技能校验")
    canvas = body["canvas"]
    r = pipeline.client.post(
        "/api/studio/project/skills",
        json={"canvas": canvas, "skills": {"script_analysis": "not-exist@9.9.9"}},
    )
    assert r.status_code == 400
    # 合法选择能保存
    ok = pipeline.client.post(
        "/api/studio/project/skills",
        json={"canvas": canvas, "skills": {"script_analysis": "script-analysis@1.0.0"}},
    )
    assert ok.status_code == 200
    assert ok.json()["skills"]["script_analysis"] == "script-analysis@1.0.0"
