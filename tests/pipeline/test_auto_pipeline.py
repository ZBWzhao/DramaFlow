"""auto_asset_table：首次导入剧本后自动跑「剧本理解 + 资产解析」，停在表不生图。"""
import json
import time

from conftest import make_project

SCRIPT = "第1集 开场\n林生走进陈家客厅，拿起桌上的茶杯。\n第2集 争执\n林生与苏晚在陈家客厅争吵。\n"


def _card(card):
    if card == "episode_outline":
        return json.dumps({"episode_outline": [{"no": 1, "title": "开场", "hook": "茶杯", "beats": ["b"]}]}, ensure_ascii=False)
    if card == "audiovisual_style":
        return json.dumps({"audiovisual_style": {"visual": "写实", "camera": "固定", "pacing": "快"}}, ensure_ascii=False)
    return json.dumps({card: f"{card}内容"}, ensure_ascii=False)


def _parse_reply(names):
    chars = [{"name": n, "description": f"{n}的身份", "bio": "", "looks": [{"name": "宴会便装"}]} for n in names]
    return json.dumps(
        {"characters": chars, "scenes": [{"name": "陈家客厅", "description": "主场景"}], "props": []},
        ensure_ascii=False,
    )


def test_auto_pipeline_runs_after_first_import(pipeline):
    pipeline.llm.replies = [
        _card("worldview"), _card("synopsis"), _card("episode_outline"), _card("audiovisual_style"),
        _parse_reply(["林生"]), _parse_reply(["林生", "苏晚"]),
    ]
    body = make_project(pipeline.client, title="自动流水线", script=SCRIPT, auto=True)
    canvas = body["canvas"]

    deadline = time.time() + 8
    project = {}
    while time.time() < deadline:
        project = pipeline.client.get(f"/api/studio/project?canvas={canvas}").json()
        analysis = project.get("story_analysis") or {}
        tables = project.get("asset_tables") or {}
        if analysis.get("worldview") and tables.get("characters"):
            break
        time.sleep(0.1)
    assert project["story_analysis"]["worldview"] == "worldview内容"
    assert project["story_analysis"]["skill"] == {"name": "script-analysis", "version": "1.0.0"}
    assert [c["name"] for c in project["asset_tables"]["characters"]] == ["林生", "苏晚"]
    # 停在表：资产库没有任何节点被创建
    assets = pipeline.client.get(f"/api/studio/assets?canvas={canvas}").json()
    assert sum(len(assets[k]) for k in ("character", "scene", "prop")) == 0


def test_auto_pipeline_skipped_when_disabled(pipeline):
    pipeline.llm.replies = []
    body = make_project(pipeline.client, title="不自动", script=SCRIPT, auto=False)
    canvas = body["canvas"]
    time.sleep(0.5)
    assert pipeline.llm.calls == []  # 没勾选就完全不调模型
