"""P0-9 RED：画布读不到「新式」正文（`data.prompt` / `outputs.prompt`）。

现状（修复前）：
- `read_text` 只认 `content_path` 文件或 `outputs["text"]`，S4/S5 之后的正文写在
  `data.prompt`（渲染结果）或 `outputs["prompt"]`（人改写的片段正文）；
- 于是画布生成（`generation_jobs → text_inputs → read_text`）与导出（`render_all`）
  都拿到空串；
- 叠加：`validate` 仍硬要求 prompt 节点有 `content_path` 且正文 ≥500 字，
  新画布体检必然报「还没有正文」。

修复后：正文读取统一成单函数回退链；内容长度降级为「建议」，不再算体检问题。
"""

from __future__ import annotations

from pathlib import Path

from core.canvas import Canvas

BODY = "雨夜霓虹街道，女孩撑伞走过积水，镜头缓慢推进，电影感，35mm。"
FILE_BODY = "旧格式正文：存在 assets/<node>/content.md 里的片段文本。"


def _canvas(tmp_path: Path) -> Canvas:
    return Canvas("main", path=tmp_path / "main.canvas.json")


def _prompt(canvas: Canvas, **data):
    return canvas.add_node(
        "prompt",
        "片段 1",
        position={"x": 0.0, "y": 0.0},
        data={"segment_no": 1, **data},
    )


def test_p0_09_read_text_falls_back_to_data_prompt(tmp_path: Path) -> None:
    canvas = _canvas(tmp_path)
    node = _prompt(canvas, prompt=BODY)
    assert canvas.read_text(node.id) == BODY


def test_p0_09_read_text_reads_outputs_prompt(tmp_path: Path) -> None:
    canvas = _canvas(tmp_path)
    node = _prompt(canvas, prompt=BODY)
    node.outputs = {"prompt": {"data_type": "STRING", "value": "人改写的正文"}}
    assert canvas.read_text(node.id) == "人改写的正文"


def test_p0_09_read_text_still_reads_content_file(tmp_path: Path) -> None:
    canvas = _canvas(tmp_path)
    relative = "assets/prompt-1/content.md"
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FILE_BODY, encoding="utf-8")
    node = _prompt(canvas, content_path=relative)
    assert canvas.read_text(node.id) == FILE_BODY


def test_p0_09_validate_accepts_new_style_prompt(tmp_path: Path) -> None:
    """正文在 data.prompt、没有 content_path 的新画布不应该被体检判定为「没正文」。"""
    canvas = _canvas(tmp_path)
    node = _prompt(canvas, prompt=BODY)
    # 连一个资产，避免「提示词节点没有任何输入」这条无关问题掩盖本用例。
    asset = canvas.add_node(
        "asset",
        "女主",
        position={"x": 0.0, "y": 200.0},
        data={
            "kind": "character",
            "name": "女主",
            "spec": "短发、灰色风衣",
            "anchors": ["短发"],
            "consistency_tags": ["短发", "灰色风衣"],
        },
    )
    canvas.connect(asset.id, "ref", node.id, "ref")
    problems = canvas.validate()
    assert not any("还没有正文" in item for item in problems), problems
    assert not any("content_path" in item for item in problems), problems


def test_p0_09_validate_short_body_is_a_suggestion_not_a_problem(tmp_path: Path) -> None:
    canvas = _canvas(tmp_path)
    node = _prompt(canvas, prompt="下雨。")
    asset = canvas.add_node(
        "asset",
        "女主",
        position={"x": 0.0, "y": 200.0},
        data={
            "kind": "character",
            "name": "女主",
            "spec": "短发、灰色风衣",
            "anchors": ["短发"],
            "consistency_tags": ["短发", "灰色风衣"],
        },
    )
    canvas.connect(asset.id, "ref", node.id, "ref")
    problems = canvas.validate()
    assert not any("500" in item or "正文仅" in item for item in problems), problems


def test_p0_09_validate_still_reports_missing_body(tmp_path: Path) -> None:
    canvas = _canvas(tmp_path)
    node = _prompt(canvas)
    asset = canvas.add_node(
        "asset",
        "女主",
        position={"x": 0.0, "y": 200.0},
        data={
            "kind": "character",
            "name": "女主",
            "spec": "短发、灰色风衣",
            "anchors": ["短发"],
            "consistency_tags": ["短发", "灰色风衣"],
        },
    )
    canvas.connect(asset.id, "ref", node.id, "ref")
    problems = canvas.validate()
    assert any("还没有正文" in item for item in problems), problems


def test_p0_09_export_includes_new_style_body(tmp_path: Path) -> None:
    canvas = _canvas(tmp_path)
    _prompt(canvas, prompt=BODY)
    markdown = canvas.render_all()
    assert BODY in markdown


def test_p0_09_api_exposes_content_for_data_prompt_node(p0) -> None:
    """卡片详情/编辑框需要正文：GET /api/canvas 应返回 data.prompt 的 content。"""
    revision = p0.revision()
    created = p0.client.post(
        "/api/canvas/nodes",
        json={
            "canvas": "main",
            "type": "prompt",
            "label": "新式片段",
            "x": 0.0,
            "y": 600.0,
            "data": {"prompt": BODY, "segment_no": 9},
            "revision": revision,
        },
    )
    assert created.status_code == 200, created.text
    node_id = created.json()["node_id"]

    document = p0.canvas_doc()
    node = next(item for item in document["nodes"] if item["id"] == node_id)
    assert node["content"] == BODY
    assert not any("还没有正文" in item for item in document["problems"]), document["problems"]


def test_p0_09_alternating_writers_keep_latest_body(p0) -> None:
    """同一 prompt 节点被画布 /text 与制片台 /studio/.../prompt 交替写时，
    读侧必须看到最后一次写入的正文。

    历史缺口的成因：画布走 outputs.text、制片台走 outputs.prompt，而 read_text 的
    顺序是 outputs.prompt → outputs.text → data.prompt；两个槽位都留着旧副本时，
    旧值会永远盖住新值 —— 用户的编辑被静默回退成上一次的内容。
    """
    revision = p0.revision()
    created = p0.client.post(
        "/api/canvas/nodes",
        json={
            "canvas": "main",
            "type": "prompt",
            "label": "交替写入",
            "x": 900.0,
            "y": 0.0,
            "data": {"prompt": "最初正文", "segment_no": 11},
            "revision": revision,
        },
    )
    assert created.status_code == 200, created.text
    node_id = created.json()["node_id"]

    studio = p0.client.post(
        f"/api/studio/segments/{node_id}/prompt",
        params={"canvas": "main"},
        json={"prompt": "制片台写入"},
    )
    assert studio.status_code == 200, studio.text

    canvas_write = p0.client.post(
        f"/api/canvas/nodes/{node_id}/text",
        json={"canvas": "main", "text": "画布最后写入", "revision": p0.revision()},
    )
    assert canvas_write.status_code == 200, canvas_write.text

    document = p0.canvas_doc()
    node = next(item for item in document["nodes"] if item["id"] == node_id)
    assert node["content"] == "画布最后写入", node["outputs"]
    assert set(node["outputs"]) == {"prompt"}, node["outputs"]


def test_p0_09_studio_segment_view_reads_same_body(p0) -> None:
    """制片台读正文必须与画布同一来源（Canvas.read_text）。

    制片台曾自己维护一份读取顺序（outputs.text → outputs.prompt，且不认
    data.prompt），导致同一片段在画布能读到正文、在制片台详情里是空的。
    """
    revision = p0.revision()
    created = p0.client.post(
        "/api/canvas/nodes",
        json={
            "canvas": "main",
            "type": "prompt",
            "label": "制片台读取",
            "x": 900.0,
            "y": 200.0,
            "data": {"prompt": BODY, "segment_no": 12},
            "revision": revision,
        },
    )
    assert created.status_code == 200, created.text
    node_id = created.json()["node_id"]

    detail = p0.client.get(f"/api/studio/segments/{node_id}", params={"canvas": "main"})
    assert detail.status_code == 200, detail.text
    assert detail.json()["prompt"] == BODY
