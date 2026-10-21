"""P0-00 冒烟：确认夹具本身可用（不验证缺陷）。"""

from __future__ import annotations


def test_app_boots_with_minimal_canvas(p0) -> None:
    doc = p0.canvas_doc()
    assert doc["exists"] is True
    assert doc["revision"] >= 1
    assert {node["type"] for node in doc["nodes"]} == {"prompt", "image_gen", "video_gen"}
    # 服务端把端口定义下发给前端，前端不该自己再实现一份
    assert doc["node_spec"]["video_gen"]["in"]["ref"] == "IMAGE"


def test_fake_provider_is_active(p0) -> None:
    response = p0.client.get("/api/media/providers")
    assert response.status_code == 200
    body = response.json()
    assert body["active"] == "mock"
    assert any(item["id"] == "fake-video-v1" for item in body["providers"])


def test_canvas_write_requires_no_missing_fields(p0) -> None:
    """P0-2 修复后：写回正文没带 revision 必须被拒绝（4xx），带了才生效。"""
    missing = p0.client.post(
        "/api/canvas/nodes/{}/text".format(p0.prompt_id),
        json={"canvas": "main", "text": "冒烟：改一下提示词正文"},
    )
    assert missing.status_code in {400, 409, 422, 428}, missing.text

    ok = p0.client.post(
        "/api/canvas/nodes/{}/text".format(p0.prompt_id),
        json={"canvas": "main", "text": "冒烟：改一下提示词正文", "revision": p0.revision()},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["revision"] > p0.revision() - 1
