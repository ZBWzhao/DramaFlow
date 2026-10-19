"""P0-7 RED：生成节点「无连线时按自身提示词生成」不成立。

用户问题三的核心分支：
- 视频节点**没有连线**时，应只按卡片自身提示词生成（单卡文生视频）；
- 有连线时，按连线传入的资产作为生成依据（图生视频），文本输入优先于卡片自身提示词。

现状（修复前）：`generation_jobs` 只从上游文本入边取提示词，完全不读 `data.prompt`：
单卡永远 `ready=False`；有连线时又丢掉卡片里人写的要求。

修复后定义「提示词来源优先级」：显式 prompt 节点正文 > 卡片自身 `data.prompt` > 其他文本输入，
缺失时 `ready=False` 并给出 `ready_reason`。
"""

from __future__ import annotations

from pathlib import Path

from core.canvas import Canvas

BODY = "雨夜霓虹街道，女孩撑伞走过积水，镜头缓慢推进。"
UPSTREAM_BODY = "上游提示词节点：女主特写，雨滴在伞面炸开，慢动作。"


def _canvas(tmp_path: Path) -> Canvas:
    return Canvas("main", path=tmp_path / "main.canvas.json")


def _job(canvas: Canvas, node_id: str) -> dict:
    jobs = {job["node_id"]: job for job in canvas.generation_jobs()}
    assert node_id in jobs, jobs
    return jobs[node_id]


def test_p0_07_single_card_uses_own_prompt(tmp_path: Path) -> None:
    canvas = _canvas(tmp_path)
    gen = canvas.add_node("video_gen", "镜头 1", data={"prompt": BODY})
    job = _job(canvas, gen.id)
    assert job["ready"] is True
    assert job["prompt"] == BODY
    assert job["prompt_source"] == "node"


def test_p0_07_prompt_node_wins_over_card_prompt(tmp_path: Path) -> None:
    canvas = _canvas(tmp_path)
    relative = "assets/prompt-1/content.md"
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(UPSTREAM_BODY, encoding="utf-8")
    prompt = canvas.add_node("prompt", "片段 1", data={"content_path": relative, "segment_no": 1})
    gen = canvas.add_node("video_gen", "镜头 1", data={"prompt": BODY})
    canvas.connect(prompt.id, "prompt", gen.id, "prompt")
    job = _job(canvas, gen.id)
    assert job["prompt"] == UPSTREAM_BODY
    assert job["prompt_source"] == "prompt_node"
    assert job["prompt_overridden"] is True


def test_p0_07_text_input_is_fallback(tmp_path: Path) -> None:
    canvas = _canvas(tmp_path)
    text = canvas.add_node("text", "场记", data={"prompt": "第三场：雨夜追车。"})
    gen = canvas.add_node("video_gen", "镜头 1")
    canvas.connect(text.id, "text", gen.id, "prompt")
    job = _job(canvas, gen.id)
    assert job["prompt"] == "第三场：雨夜追车。"
    assert job["prompt_source"] == "text_input"


def test_p0_07_missing_prompt_is_not_ready_with_reason(tmp_path: Path) -> None:
    canvas = _canvas(tmp_path)
    gen = canvas.add_node("video_gen", "镜头 1")
    job = _job(canvas, gen.id)
    assert job["ready"] is False
    assert job["ready_reason"], job


def test_p0_07_validate_allows_single_card_with_own_prompt(tmp_path: Path) -> None:
    canvas = _canvas(tmp_path)
    canvas.add_node("video_gen", "镜头 1", data={"prompt": BODY})
    problems = canvas.validate()
    assert not any("没有连接 prompt 输入，无法执行" in item for item in problems), problems


def test_p0_07_end_to_end_single_card_generation(p0) -> None:
    """最常用路径：新建一张视频卡、只填提示词、不连线，点生成就应该出片。"""
    revision = p0.revision()
    created = p0.client.post(
        "/api/canvas/nodes",
        json={
            "canvas": "main",
            "type": "video_gen",
            "label": "单卡镜头",
            "x": 960.0,
            "y": 0.0,
            "data": {"prompt": BODY},
            "revision": revision,
        },
    )
    assert created.status_code == 200, created.text
    node_id = created.json()["node_id"]

    approval_id = p0.approve([node_id])
    job_id = p0.run([node_id], approval_id)
    job = p0.wait_job(job_id)
    assert job["status"] == "succeeded", job
    node = p0.canvas_node(node_id)
    assert node["status"] == "done", node
    assert node["outputs"]["video"]["value"]["path"], node["outputs"]
    assert len(node["data"]["takes"]) == 1, node["data"]
    sent = p0.provider.calls[-1]
    assert sent["kind"] == "video"
    assert sent["request"]["prompt"] == BODY, sent["request"]
