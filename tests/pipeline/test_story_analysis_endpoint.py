"""剧本理解流式端点：NDJSON 事件序、逐卡落盘、单飞 409、可打断。"""
import json

from conftest import FakeLLM, make_project

SCRIPT = "第1集 开场\n林生推开陈家客厅的门，发现桌上有一只陌生的茶杯。\n"


def _card(card):
    if card == "episode_outline":
        return json.dumps({"episode_outline": [{"no": 1, "title": "开场", "hook": "陌生茶杯", "beats": ["b"]}]}, ensure_ascii=False)
    if card == "audiovisual_style":
        return json.dumps({"audiovisual_style": {"visual": "写实", "camera": "固定机位", "pacing": "快"}}, ensure_ascii=False)
    return json.dumps({card: f"{card}内容"}, ensure_ascii=False)


def _run_analysis(client, canvas):
    with client.stream("POST", "/api/studio/story-analysis/run", json={"canvas": canvas}) as r:
        assert r.status_code == 200
        events = [json.loads(line) for line in r.iter_lines() if line.strip()]
    return events


def test_run_emits_card_events_and_persists(pipeline):
    pipeline.llm.replies = [_card(c) for c in ("worldview", "synopsis", "episode_outline", "audiovisual_style")]
    body = make_project(pipeline.client, title="流式分析", script=SCRIPT)
    canvas = body["canvas"]
    events = _run_analysis(pipeline.client, canvas)
    assert [e["type"] for e in events] == [
        "card_start", "card_done", "card_start", "card_done", "card_start", "card_done", "card_start", "card_done", "analysis_done",
    ]
    assert events[1]["key"] == "worldview"
    assert events[-1]["analysis"]["skill"] == {"name": "script-analysis", "version": "1.0.0"}
    project = pipeline.client.get(f"/api/studio/project?canvas={canvas}").json()
    assert project["story_analysis"]["worldview"] == "worldview内容"
    assert project["story_analysis"]["episode_outline"][0]["hook"] == "陌生茶杯"


def test_run_without_script_rejected(pipeline):
    body = make_project(pipeline.client, title="空剧本")
    r = pipeline.client.post("/api/studio/story-analysis/run", json={"canvas": body["canvas"]})
    assert r.status_code == 400


def test_second_run_while_active_conflicts_409(pipeline):
    # 假 LLM 挂起第一卡：模拟一个还没跑完的分析
    class HangingLLM(FakeLLM):
        async def simple(self, messages):
            import asyncio

            await asyncio.sleep(3600)

    import server.app as app_module
    from server import studio as studio_module

    hanging = HangingLLM()
    old_llm = pipeline.registry.llm
    pipeline.registry.llm = hanging
    try:
        body = make_project(pipeline.client, title="并发409", script=SCRIPT)
        canvas = body["canvas"]
        import threading

        results = []

        def bg():
            results.append(pipeline.client.post("/api/studio/story-analysis/run", json={"canvas": canvas}))

        t = threading.Thread(target=bg)
        t.start()
        # 第一个请求已进入执行（等 _story_runs 出现）
        import time

        for _ in range(100):
            run = studio_module._story_runs.get(canvas)
            if run and not run["task"].done():
                break
            time.sleep(0.05)
        r2 = pipeline.client.post("/api/studio/story-analysis/run", json={"canvas": canvas})
        assert r2.status_code == 409
        r3 = pipeline.client.post("/api/studio/story-analysis/cancel", json={"canvas": canvas})
        assert r3.status_code == 200 and r3.json()["ok"] is True
        t.join(timeout=10)
        assert not t.is_alive(), "取消后流没有终止"
        assert results and results[0].status_code == 200
    finally:
        pipeline.registry.llm = old_llm


def test_cancel_keeps_finished_cards(pipeline):
    class CancelAfterWorldview(FakeLLM):
        async def simple(self, messages):
            from server import studio as studio_module

            text = await super().simple(messages)
            run = studio_module._story_runs.get(self.canvas)
            if run is not None and len(self.calls) >= 1:
                run["cancel"].set()  # 第一卡完成后置位：后续卡跳过
            return text

    llm = CancelAfterWorldview([_card(c) for c in ("worldview", "synopsis", "episode_outline", "audiovisual_style")])
    old_llm = pipeline.registry.llm
    pipeline.registry.llm = llm
    try:
        body = make_project(pipeline.client, title="打断保留", script=SCRIPT)
        canvas = body["canvas"]
        llm.canvas = canvas
        events = _run_analysis(pipeline.client, canvas)
        dones = [e for e in events if e["type"] == "card_done"]
        assert [e["key"] for e in dones] == ["worldview"]
        assert events[-1]["type"] == "analysis_done" and events[-1]["cancelled"] is True
        project = pipeline.client.get(f"/api/studio/project?canvas={canvas}").json()
        assert project["story_analysis"]["worldview"] == "worldview内容"
        assert project["story_analysis"]["synopsis"] == ""  # 被跳过的卡为空
    finally:
        pipeline.registry.llm = old_llm
