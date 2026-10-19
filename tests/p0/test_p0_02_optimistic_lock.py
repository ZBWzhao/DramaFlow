"""P0-2：画布写接口必须强制乐观锁（revision）。

现状（RED）：
- ``POST /api/canvas/nodes/{id}/text``：``revision: int | None = None``，
  ``_require_revision(canvas, None)`` 直接放行 → 一次迟到的自动保存/writeback
  可以安静覆盖别人刚写的正文（典型场景：前端 debounce 保存 + 多人/多端编辑）。
- ``POST /api/canvas/nodes/{id}/take``：请求模型里根本没有 revision 字段，
  同理无条件覆盖 ``current_take_id`` 与 outputs。

修复后（GREEN）：这两个"改写画布内容"的接口都要求 revision，缺失即 4xx，
过期即 409（canvas_revision_conflict），与 /position、/connect、/nodes/{id} 一致。
"""

from __future__ import annotations

MISSING_REVISION_STATUSES = {400, 409, 422, 428}


def test_p0_02_text_write_without_revision_is_rejected(p0) -> None:
    response = p0.client.post(
        f"/api/canvas/nodes/{p0.prompt_id}/text",
        json={"canvas": "main", "text": "迟到的自动保存，不应覆盖画布"},
    )
    assert response.status_code in MISSING_REVISION_STATUSES, (
        f"不带宽版本号的文字写回被接受了：{response.status_code} {response.text[:200]}"
    )


def test_p0_02_stale_revision_is_rejected(p0) -> None:
    """带过期 revision 的写回必须 409（这一条现状可能已经正确，作为对照组）。"""
    stale = p0.revision()
    # 先让 revision 前进一次
    moved = p0.client.post(
        f"/api/canvas/nodes/{p0.prompt_id}/position",
        json={"canvas": "main", "x": 10, "y": 10, "revision": stale},
    )
    assert moved.status_code == 200, moved.text
    response = p0.client.post(
        f"/api/canvas/nodes/{p0.prompt_id}/text",
        json={"canvas": "main", "text": "用过期版本写的正文", "revision": stale},
    )
    assert response.status_code == 409, response.text
    assert response.json().get("code") == "canvas_revision_conflict"


def test_p0_02_delete_canvas_requires_revision(p0) -> None:
    """删除画布也是写：不能只凭"我知道这个名字"就把别人刚改的画布删掉。"""
    from core.canvas import Canvas

    side = Canvas("side", path=p0.workspace / "side.canvas.json")
    side.add_node("prompt", "旁支项目", position={"x": 0.0, "y": 0.0}, data={})
    side.save()

    missing = p0.client.delete("/api/canvases/side")
    assert missing.status_code in MISSING_REVISION_STATUSES, missing.text
    assert (p0.workspace / "side.canvas.json").exists(), "缺 revision 的删除不应生效"

    stale = p0.client.delete("/api/canvases/side", params={"revision": 999})
    assert stale.status_code == 409, stale.text

    ok = p0.client.delete("/api/canvases/side", params={"revision": 1})
    assert ok.status_code == 200, ok.text
    assert not (p0.workspace / "side.canvas.json").exists()


def test_p0_02_take_switch_without_revision_is_rejected(p0) -> None:
    take = {
        "take_id": "take_p0",
        "path": "assets/generated/fake-old.mp4",
        "mime": "video/mp4",
        "provider": "mock",
        "mock": True,
        "model": "fake-video-v1",
        "created_at": 0,
    }
    seeded = p0.client.post(
        f"/api/canvas/nodes/{p0.video_id}",
        json={"canvas": "main", "data": {"takes": [take]}, "revision": p0.revision()},
    )
    assert seeded.status_code == 200, seeded.text

    response = p0.client.post(
        f"/api/canvas/nodes/{p0.video_id}/take",
        json={"canvas": "main", "take_id": "take_p0"},
    )
    assert response.status_code in MISSING_REVISION_STATUSES, (
        f"不带宽版本号的切版被接受了：{response.status_code} {response.text[:200]}"
    )
