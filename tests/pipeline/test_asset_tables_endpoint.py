"""资产表端点：parse(NDJSON) → PUT 编辑 → sync 幂等 → export-prompt。"""
import json

from conftest import make_project

SCRIPT = "第1集 开场\n林生走进陈家客厅，拿起桌上的茶杯。\n第2集 争执\n林生与苏晚在陈家客厅争吵。\n"


def _parse_reply(names):
    chars = [{"name": n, "description": f"{n}的身份", "bio": "约175cm", "looks": [{"name": "宴会便装"}]} for n in names]
    return json.dumps(
        {"characters": chars, "scenes": [{"name": "陈家客厅", "description": "主场景"}], "props": [{"name": "茶杯", "description": "道具"}]},
        ensure_ascii=False,
    )


def _stream(client, path, body):
    with client.stream("POST", path, json=body) as r:
        assert r.status_code == 200
        return [json.loads(line) for line in r.iter_lines() if line.strip()]


def test_parse_put_sync_export_roundtrip(pipeline):
    from server import studio as studio_module

    pipeline.llm.replies = [_parse_reply(["林生"]), _parse_reply(["林生", "苏晚"])]
    body = make_project(pipeline.client, title="资产端点", script=SCRIPT)
    canvas = body["canvas"]

    # 1) parse：流式进度 + 落盘
    events = _stream(pipeline.client, "/api/studio/asset-tables/parse", {"canvas": canvas})
    assert [e["type"] for e in events] == ["parse_progress", "parse_progress", "parse_done"]
    tables = events[-1]["tables"]
    assert [c["name"] for c in tables["characters"]] == ["林生", "苏晚"]
    project = pipeline.client.get(f"/api/studio/project?canvas={canvas}").json()
    assert project["asset_tables"]["characters"][0]["name"] == "林生"

    # 2) PUT 编辑：改描述，服务端校验后覆写
    tables["characters"][0]["description"] = "手工改过的描述"
    tables["synced_at"] = 0
    r = pipeline.client.put("/api/studio/asset-tables", json={"canvas": canvas, "tables": tables})
    assert r.status_code == 200
    assert r.json()["tables"]["characters"][0]["description"] == "手工改过的描述"

    # 3) sync：首次创建资产 + 形象
    r = pipeline.client.post("/api/studio/asset-tables/sync", json={"canvas": canvas})
    assert r.status_code == 200
    report = r.json()
    assert report["created_assets"] == 4  # 林生/苏晚/陈家客厅/茶杯
    assert report["created_looks"] == 2
    # 幂等：再同步一次不重复创建
    r2 = pipeline.client.post("/api/studio/asset-tables/sync", json={"canvas": canvas})
    assert r2.json()["created_assets"] == 0 and r2.json()["created_looks"] == 0
    assets = pipeline.client.get(f"/api/studio/assets?canvas={canvas}").json()
    char_names = [a["name"] for a in assets["character"]]
    assert char_names == ["林生", "苏晚"]
    lin = assets["character"][0]
    assert lin["looks"][0]["name"] == "宴会便装"
    project = pipeline.client.get(f"/api/studio/project?canvas={canvas}").json()
    assert project["asset_tables"]["synced_at"] > 0

    # 4) export-prompt：先用产品路径给形象写提示词，再导出
    look_id = lin["looks"][0]["id"]
    r = pipeline.client.post(
        f"/api/studio/assets/{lin['node_id']}/looks?canvas={canvas}",
        json={"name": "宴会便装", "prompt": "林生，宴会便装，正面全身，平光纯灰底"},
    )
    assert r.status_code == 200
    ok = pipeline.client.get(f"/api/studio/looks/export-prompt?canvas={canvas}&asset={lin['node_id']}&look={look_id}")
    assert ok.status_code == 200
    assert "text/plain" in ok.headers["content-type"]
    assert "林生" in ok.text
    assert "attachment" in ok.headers["content-disposition"]
    # 不存在的形象 → 404
    empty = pipeline.client.get(f"/api/studio/looks/export-prompt?canvas={canvas}&asset={lin['node_id']}&look=look_none")
    assert empty.status_code == 404


def test_parse_conflicts_with_running_story_analysis(pipeline):
    import asyncio

    import server.app as app_module

    class HangingLLM:
        async def simple(self, messages):
            await asyncio.sleep(3600)

    old = pipeline.registry.llm
    pipeline.registry.llm = HangingLLM()
    try:
        body = make_project(pipeline.client, title="解析冲突", script=SCRIPT)
        canvas = body["canvas"]
        import threading
        import time

        def bg():
            pipeline.client.post("/api/studio/story-analysis/run", json={"canvas": canvas})

        t = threading.Thread(target=bg)
        t.start()
        from server import studio as studio_module

        for _ in range(100):
            run = studio_module._story_runs.get(canvas)
            if run and not run["task"].done():
                break
            time.sleep(0.05)
        r = pipeline.client.post("/api/studio/asset-tables/parse", json={"canvas": canvas})
        assert r.status_code == 409
        # 用 HTTP cancel 端点打断：task.cancel 从测试线程直接调不是线程安全的
        r3 = pipeline.client.post("/api/studio/story-analysis/cancel", json={"canvas": canvas})
        assert r3.status_code == 200 and r3.json()["ok"] is True
        t.join(timeout=10)
        assert not t.is_alive(), "取消后流没有终止"
    finally:
        pipeline.registry.llm = old
