"""批量出片任务化：一次请求排队生成 N 个片段，有进度、可取消、单项失败不拖垮整批。

原来的前端 batchGenerate 是串行 for 循环：关页面就断、没有进度、不能取消，
几十段就是几十次盲等。现在对齐 produce 的 job 模式：POST 建 job →
轮询 progress（done/ok/failed）→ 终态带 per-item 结果。
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.canvas import Canvas  # noqa: E402

BATCH_URL = "/api/studio/segments/batch-generate"


class FakeMedia:
    """替身 provider：真写小文件；可按正文标记失败、可放慢节奏配合取消测试。"""

    active = "fake"

    def __init__(self, fail_on: str = "", delay: float = 0.0):
        self.fail_on = fail_on
        self.delay = delay
        self.calls: list[dict] = []

    def list_models(self):
        return [{"id": "fake-video", "kind": "video", "provider": "fake"}]

    def generate(self, kind, request, workspace):
        self.calls.append(dict(request))
        time.sleep(self.delay)
        if self.fail_on and self.fail_on in str(request.get("prompt") or ""):
            raise RuntimeError("模拟生成失败")
        out = Path(workspace) / "clips" / f"take_{len(self.calls)}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00")
        return SimpleNamespace(path=str(out), mime="video/mp4", provider="fake", mock=False)


def _shot(no: int, text: str = "") -> dict:
    shot = {
        "no": no,
        "duration_s": 5.0,
        "shot_size": "中景",
        "camera": {"height": "齐腰", "angle": "正面", "movement": "固定"},
        "action": "他推门进来。",
        "subjects": [{"name": "林生"}],
    }
    if text:
        shot["dialogue"] = {"text": text}
    return shot


def _seed(env, name: str, segments: list[tuple[int, str]]) -> list[str]:
    """segments: [(segment_no, prompt_text)]。同步写画布文件，绕开 asyncio 仓库的事件循环绑定。"""
    canvas = Canvas(name)
    canvas.meta.update({"story_title": name, "aspect_ratio": "9:16", "quality": "480P"})
    node_ids: list[str] = []
    for seg_no, text in segments:
        sb = canvas.add_node(
            "storyboard",
            f"分镜{seg_no:02d}",
            data={"episode_no": 1, "segment_no": seg_no, "shots": [_shot(1, "嗯。")]},
        )
        prompt = canvas.add_node(
            "prompt",
            f"分镜{seg_no:02d}·片段",
            # rendered_by=human：跳过服务端正文渲染，测试文本原样进生成请求
            data={"episode_no": 1, "segment_no": seg_no, "prompt": text, "rendered_by": "human"},
        )
        gen = canvas.add_node("video_gen", f"生成槽{seg_no:02d}")
        canvas.connect(sb.id, "shots", prompt.id, "ctx")
        canvas.connect(prompt.id, "prompt", gen.id, "prompt")
        node_ids.append(prompt.id)
    canvas.path = Path(env.workspace) / f"{name}.canvas.json"
    canvas.save()
    return node_ids


def _post_batch(client, canvas_name: str, node_ids: list[str]):
    return client.post(f"{BATCH_URL}?canvas={canvas_name}", json={"node_ids": node_ids})


def _get_job(client, job_id: str) -> dict:
    res = client.get(f"/api/studio/jobs/{job_id}")
    assert res.status_code == 200, res.text
    return res.json()


def _wait_terminal(client, job_id: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    job: dict = {}
    while time.time() < deadline:
        job = _get_job(client, job_id)
        if job.get("status") not in ("queued", "running"):
            return job
        time.sleep(0.1)
    return job


def _wait_inner_running(client, batch_id: str, timeout: float = 10.0) -> dict:
    """等到第一段的单段任务真正 running 再取消——批量标记 running 到内部 job
    建档之间有窗口，在这个窗口里取消是合法路径（item 停在 cancelled、没有子
    job），但那不是本用例要验证的语义。"""
    deadline = time.time() + timeout
    job: dict = {}
    while time.time() < deadline:
        job = _get_job(client, batch_id)
        first = (job.get("items") or [{}])[0]
        if first.get("job_id") and _get_job(client, first["job_id"]).get("status") == "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"10s 内第一段的单段任务没有进入 running：{job}")


def test_batch_generates_all_and_reports_progress(pipeline, monkeypatch):
    from server import studio as studio_module

    media = FakeMedia()
    monkeypatch.setattr(studio_module, "_media", media)
    ids = _seed(pipeline, "batch-ok", [(1, "第一段"), (2, "第二段")])

    res = _post_batch(pipeline.client, "batch-ok", ids)
    assert res.status_code == 200, res.text
    job = res.json()["job"]
    assert job["kind"] == "batch_generate"
    assert [item["segment_no"] for item in job["items"]] == [1, 2]

    job = _wait_terminal(pipeline.client, job["job_id"])
    assert job["status"] == "succeeded"
    assert job["progress"]["total"] == 2 and job["progress"]["ok"] == 2 and job["progress"]["failed"] == 0
    assert all(item["status"] == "succeeded" for item in job["items"])
    assert len(media.calls) == 2, "两项都要真的发起生成"
    # 子任务挂在批量 job 名下，任务中心按 parent 归并，不逐条刷屏
    inner = _get_job(pipeline.client, job["items"][0]["job_id"])
    assert inner.get("parent") == job["job_id"]
    # 生成结果真的落到画布：两段都有片可播
    segs = pipeline.client.get("/api/studio/segments?canvas=batch-ok").json()["segments"]
    assert [s["has_video"] for s in segs] == [True, True]


def test_batch_continues_past_failed_item(pipeline, monkeypatch):
    from server import studio as studio_module

    media = FakeMedia(fail_on="必失败")
    monkeypatch.setattr(studio_module, "_media", media)
    ids = _seed(pipeline, "batch-mix", [(1, "这段必失败"), (2, "这段没事")])

    res = _post_batch(pipeline.client, "batch-mix", ids)
    assert res.status_code == 200
    job = _wait_terminal(pipeline.client, res.json()["job"]["job_id"])

    assert job["status"] == "succeeded", "一项失败不该把整批判死"
    assert job["progress"]["ok"] == 1 and job["progress"]["failed"] == 1
    assert job["items"][0]["status"] == "failed"
    assert "模拟生成失败" in job["items"][0]["error"], "失败原因要能直达制作者"
    assert job["items"][1]["status"] == "succeeded", "失败后必须继续跑完剩余项"
    assert len(media.calls) == 2


def test_batch_cancel_skips_remaining_and_stops_current(pipeline, monkeypatch):
    from server import studio as studio_module

    media = FakeMedia(delay=0.4)
    monkeypatch.setattr(studio_module, "_media", media)
    ids = _seed(pipeline, "batch-cancel", [(1, "第一段"), (2, "第二段")])

    res = _post_batch(pipeline.client, "batch-cancel", ids)
    assert res.status_code == 200
    batch_id = res.json()["job"]["job_id"]
    _wait_inner_running(pipeline.client, batch_id)

    stop = pipeline.client.post(f"/api/studio/jobs/{batch_id}/cancel")
    assert stop.status_code == 200

    job = _wait_terminal(pipeline.client, batch_id)
    assert job["status"] == "cancelled"
    assert job["items"][1]["status"] == "skipped", "没开跑的直接标跳过，不再发起"
    assert job["items"][0]["status"] == "cancelled"
    # 正在跑的那一单也要被收尾成 cancelled，而不是永远停在 running
    inner = _get_job(pipeline.client, job["items"][0]["job_id"])
    assert inner["status"] == "cancelled"
    assert len(media.calls) <= 2
