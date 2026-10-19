"""P0-10 RED：画布生成不读项目画幅与分镜时长。

现状（修复前）：
- 前端 `runCanvas` 写死 `duration:5 / resolution:"720p" / ratio:"16:9"`；
- 后端 Worker 把 size 硬编码成图片 `1024x576`、视频 `1280x720`；
- 9:16 的短剧项目从画布出片会得到 16:9，分镜时长被丢弃。

修复后定义参数解析链「节点 params → 分镜镜头时长 → 项目 defaults（ratio/quality）→ 请求值」，
provider 侧只做能力 clamp；合成阶段沿用同一来源。
"""

from __future__ import annotations

from pathlib import Path

from core.canvas import Canvas

BODY = "雨夜霓虹街道，女孩撑伞走过积水。"
PROJECT = {"aspect_ratio": "9:16", "quality": "720P", "video_model": "fake-video-v1"}

SHOTS = [
    {"no": 1, "segment_no": 1, "duration_s": 3.0, "shot_size": "远景"},
    {"no": 2, "segment_no": 2, "duration_s": 6.0, "shot_size": "近景"},
]


def _canvas(tmp_path: Path) -> Canvas:
    return Canvas("main", path=tmp_path / "main.canvas.json")


def _params(canvas: Canvas, node_id: str) -> dict:
    jobs = {job["node_id"]: job for job in canvas.generation_jobs()}
    assert node_id in jobs, jobs
    return jobs[node_id]["params"]


def test_p0_10_params_come_from_project_and_storyboard(tmp_path: Path) -> None:
    canvas = _canvas(tmp_path)
    canvas.meta["project"] = dict(PROJECT)
    canvas.add_node("storyboard", "分镜", data={"shots": SHOTS})
    gen = canvas.add_node("video_gen", "镜头 2", data={"prompt": BODY, "segment_no": 2})
    params = _params(canvas, gen.id)
    assert params["duration"] == 6
    assert params["ratio"] == "9:16"
    assert params["resolution"] == "720p"
    assert params["size"] == "720x1280"


def test_p0_10_node_params_override_project(tmp_path: Path) -> None:
    canvas = _canvas(tmp_path)
    canvas.meta["project"] = dict(PROJECT)
    gen = canvas.add_node(
        "video_gen",
        "镜头 1",
        data={"prompt": BODY, "duration": 4, "ratio": "1:1", "quality": "1080P"},
    )
    params = _params(canvas, gen.id)
    assert params["duration"] == 4
    assert params["ratio"] == "1:1"
    assert params["resolution"] == "1080p"
    assert params["size"] == "1920x1920"


def test_p0_10_params_are_none_without_sources(tmp_path: Path) -> None:
    canvas = _canvas(tmp_path)
    gen = canvas.add_node("video_gen", "镜头 1", data={"prompt": BODY})
    params = _params(canvas, gen.id)
    assert params["duration"] is None
    assert params["ratio"] is None
    assert params["resolution"] is None
    assert params["size"] is None


def _apply_project(p0, project: dict) -> None:
    """直接改画布文件设置项目参数（走领域 API，模拟前端项目设置面板的结果）。"""
    path = p0.workspace / "main.canvas.json"
    canvas = Canvas.load(path, "main")
    canvas.meta["project"] = dict(project)
    canvas.save()


def _add_storyboard(p0) -> None:
    path = p0.workspace / "main.canvas.json"
    canvas = Canvas.load(path, "main")
    canvas.add_node("storyboard", "分镜", data={"shots": SHOTS})
    canvas.save()


def test_p0_10_video_request_uses_shot_duration_and_project_ratio(p0) -> None:
    _apply_project(p0, PROJECT)
    _add_storyboard(p0)
    created = p0.client.post(
        "/api/canvas/nodes",
        json={
            "canvas": "main",
            "type": "video_gen",
            "label": "镜头 2",
            "x": 960.0,
            "y": 0.0,
            "data": {"prompt": BODY, "segment_no": 2},
            "revision": p0.revision(),
        },
    )
    assert created.status_code == 200, created.text
    node_id = created.json()["node_id"]

    approval_id = p0.approve([node_id])
    job_id = p0.run([node_id], approval_id)
    job = p0.wait_job(job_id)
    assert job["status"] == "succeeded", job
    request = p0.provider.calls[-1]["request"]
    assert request["duration"] == 6, request
    assert request["ratio"] == "9:16", request
    assert request["resolution"] == "720p", request


def test_p0_10_image_request_uses_portrait_size(p0) -> None:
    _apply_project(p0, PROJECT)
    created = p0.client.post(
        "/api/canvas/nodes",
        json={
            "canvas": "main",
            "type": "image_gen",
            "label": "竖屏关键帧",
            "x": 0.0,
            "y": 900.0,
            "data": {"prompt": BODY},
            "revision": p0.revision(),
        },
    )
    assert created.status_code == 200, created.text
    node_id = created.json()["node_id"]

    approval_id = p0.approve([node_id])
    job_id = p0.run([node_id], approval_id)
    job = p0.wait_job(job_id)
    assert job["status"] == "succeeded", job
    request = p0.provider.calls[-1]["request"]
    assert request["size"] == "720x1280", request


def test_p0_10_request_defaults_still_apply_without_canvas_settings(p0) -> None:
    """画布没设置项目参数时，退回请求里的参数（当前行为不变）。"""
    created = p0.client.post(
        "/api/canvas/nodes",
        json={
            "canvas": "main",
            "type": "video_gen",
            "label": "默认参数镜头",
            "x": 960.0,
            "y": 300.0,
            "data": {"prompt": BODY},
            "revision": p0.revision(),
        },
    )
    assert created.status_code == 200, created.text
    node_id = created.json()["node_id"]

    approval_id = p0.approve([node_id])
    job_id = p0.run([node_id], approval_id)
    job = p0.wait_job(job_id)
    assert job["status"] == "succeeded", job
    request = p0.provider.calls[-1]["request"]
    assert request["duration"] == 5, request
    assert request["ratio"] == "16:9", request
    assert request["resolution"] == "720p", request
