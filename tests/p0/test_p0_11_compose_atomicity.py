"""P0-8 的同类问题：合成（compose）路径的「审批 + 激活 + 入队」不是原子提交。

生成路径（``/api/canvas/run``）在 P0-8 已经改成单一提交点，但合成路径当时没跟着改：

1. 审批 consume 之后才做 ``media_jobs.get(job_id)`` 与激活写入；
2. 激活写入用的还是审批时冻结的 ``canvas_revision``（``_require_revision``）——
   审批之后只要有人拖了一下节点，revision 前进，整个合成请求就 409，而审批已经作废；
3. 入队失败时没有回滚：审批永久作废，画布上还挂着指向不存在任务的
   ``active_generation_id``（幽灵激活）；
4. ``job_id`` 由包含 ``canvas_revision`` 的执行计划算出来：失败后重试（revision 已变）
   会生成新的 ``job_id``，同一份审批可能被重复计费。

这组测试锁定四条语义（前三条是 RED，控制用例应始终为 GREEN）：

- 审批之后发生无关编辑，合成仍应成功（不能因为 revision 前进而失效）；
- 入队失败应返回 503、回滚幽灵激活、退回审批（同一审批可重试成功）；
- 同一审批的重试必须复用同一个 ``job_id``（不能重复计费）；
- 控制用例：没有并发/故障时合成正常受理。
"""

from __future__ import annotations

from typing import Any

import pytest

COMPOSE_APPROVALS = "/api/canvas/composition-approvals"
COMPOSE = "/api/canvas/compose"


def _patch_environment(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """把外部依赖替换成确定性替身：ffmpeg 可用、媒体探测返回假元数据。"""
    app_module = env.app_module
    monkeypatch.setattr(
        app_module,
        "ffmpeg_capabilities",
        lambda: {"available": True, "ffmpeg": "test", "ffprobe": "test", "reason": ""},
    )
    service = app_module.composition_service
    monkeypatch.setattr(
        service,
        "inspect",
        lambda path, kind: {
            "path": str(path),
            "kind": kind,
            "duration": 4.0,
            "width": 1280,
            "height": 720,
        },
    )


def _fake_enqueue(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """不真正排队：只回显任务，避免后台 Worker 去跑真实 ffmpeg。"""

    async def fake_enqueue(job: Any) -> tuple[Any, bool]:
        return job, True

    monkeypatch.setattr(env.app_module.media_jobs, "enqueue", fake_enqueue)


def _approve(env: Any) -> str:
    response = env.client.post(COMPOSE_APPROVALS, json={"canvas": "main", "revision": env.revision()})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("approval_id"), body
    return str(body["approval_id"])


def _compose(env: Any, approval_id: str) -> Any:
    return env.client.post(
        COMPOSE,
        json={
            "canvas": "main",
            "approved": True,
            "approval_id": approval_id,
            "revision": env.revision(),
        },
    )


def _move_node(env: Any, node_id: str, x: float, y: float) -> None:
    response = env.client.post(
        f"/api/canvas/nodes/{node_id}/position",
        json={"canvas": "main", "x": x, "y": y, "revision": env.revision()},
    )
    assert response.status_code == 200, response.text


def test_compose_control_happy_path(p0_compose: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """控制用例：无并发、无故障时合成应被正常受理。"""
    _patch_environment(p0_compose, monkeypatch)
    _fake_enqueue(p0_compose, monkeypatch)

    approval_id = _approve(p0_compose)
    response = _compose(p0_compose, approval_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is True
    assert body["job_id"].startswith("media_")
    assert body["canvas"] == "main"


def test_compose_survives_unrelated_edit_after_approval(p0_compose: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """审批之后用户挪了一下提示词卡：无关写入不能让已审批的合成作废。"""
    _patch_environment(p0_compose, monkeypatch)
    _fake_enqueue(p0_compose, monkeypatch)

    approval_id = _approve(p0_compose)
    revision_at_approval = p0_compose.revision()
    _move_node(p0_compose, p0_compose.prompt_id, 17.0, 23.0)
    assert p0_compose.revision() > revision_at_approval, "无关写入应让 revision 前进"

    response = _compose(p0_compose, approval_id)

    assert response.status_code == 200, response.text
    assert response.json()["job_id"].startswith("media_")
    # 审批不能被这次失败吃掉：画布状态与审批状态都应可继续使用。
    node = p0_compose.canvas_node(p0_compose.timeline_id)
    assert (node.get("data") or {}).get("active_generation_id"), "受理成功后时间线应指向本任务"


def test_compose_activation_survives_competing_edit(p0_compose: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """激活标记落盘瞬间被另一位写者抢先提交：必须重试成功，而不是 409 吃掉审批。"""
    _patch_environment(p0_compose, monkeypatch)
    _fake_enqueue(p0_compose, monkeypatch)

    approval_id = _approve(p0_compose)
    repo = p0_compose.app_module.registry.canvas_repository
    real_save = repo.save
    state = {"fired": False}

    async def save_with_competing_edit(canvas: Any) -> Any:
        timeline = canvas.nodes.get(p0_compose.timeline_id)
        if not state["fired"] and timeline is not None and timeline.data.get("active_generation_id"):
            state["fired"] = True
            other = await repo.load("main")
            other.update_node(p0_compose.prompt_id, position={"x": 111.0, "y": 222.0})
            await real_save(other)  # 另一位写者先提交，revision 前进
        return await real_save(canvas)

    monkeypatch.setattr(repo, "save", save_with_competing_edit)

    response = _compose(p0_compose, approval_id)

    assert state["fired"] is True, "测试注入的并发写没有触发"
    assert response.status_code == 200, response.text


def test_compose_enqueue_failure_releases_approval(p0_compose: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """入队失败：返回 503、回滚幽灵激活、把审批退回 pending 以便重试。"""
    _patch_environment(p0_compose, monkeypatch)

    async def broken_enqueue(job: Any) -> tuple[Any, bool]:
        raise RuntimeError("任务存储暂时不可用")

    monkeypatch.setattr(p0_compose.app_module.media_jobs, "enqueue", broken_enqueue)

    approval_id = _approve(p0_compose)
    response = _compose(p0_compose, approval_id)

    assert response.status_code == 503, response.text
    assert "审批已退回" in response.text

    timeline = p0_compose.canvas_node(p0_compose.timeline_id)
    assert not (timeline.get("data") or {}).get("active_generation_id"), "失败的提交不能留下幽灵激活"

    # 存储恢复后，同一份审批必须还能用（说明 release 生效了）。
    _fake_enqueue(p0_compose, monkeypatch)
    retry = _compose(p0_compose, approval_id)
    assert retry.status_code == 200, retry.text


def test_compose_retry_after_edit_reuses_same_job_id(p0_compose: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一审批的重复提交必须落到同一个 job_id：否则重试会新开一单、重复计费。"""
    _patch_environment(p0_compose, monkeypatch)
    _fake_enqueue(p0_compose, monkeypatch)

    approval_id = _approve(p0_compose)
    first = _compose(p0_compose, approval_id)
    assert first.status_code == 200, first.text
    first_job_id = first.json()["job_id"]

    _move_node(p0_compose, p0_compose.prompt_id, 42.0, 84.0)

    second = _compose(p0_compose, approval_id)
    assert second.status_code == 200, second.text
    assert second.json()["job_id"] == first_job_id, "同一审批重试必须复用同一任务（避免重复扣费）"
