"""P0-8：审批作废与任务入队不在同一事务里。

用户看到的症状（修复前）：

1. 点击生成后接口报错，但**画布上节点已经挂了一个"当前任务"标记**，
   指向一个根本不存在的任务 —— 之后 Worker 永远等不到它；
2. 同一份审批再也用不了：刷新页面重试也只能收到 409「执行参数与用户审批的参数不一致」，
   用户既没有生成，也无法重试，只能重新审批一次（钱没花，但流程断在半路）。

修复目标（本文件锁定的不变量）：

- 入队失败时**不留任何幽灵激活标记**（节点 ``data.active_generation_id`` 回到原值）；
- 失败时**审批退回可用状态**，用户刷新后可以用同一份审批重试成功；
- 重试成功只产生**一次**生成（一个 take），不会因为重试而多扣一次模型费用。
"""

from __future__ import annotations

from typing import Any

import pytest


def _run(p0: Any, approval_id: str, revision: int, node_ids: list[str] | None = None):
    return p0.client.post(
        "/api/canvas/run",
        json={
            "canvas": "main",
            "only": node_ids or [p0.video_id],
            "provider": "mock",
            "approved": True,
            "approval_id": approval_id,
            "duration": 5,
            "resolution": "720p",
            "ratio": "16:9",
            "generate_audio": False,
            "revision": revision,
        },
    )


def _run_tolerating_crash(p0: Any, approval_id: str, revision: int):
    """修复前入队异常会直接冲出 ASGI（TestClient 默认 re-raise），修复后应是 503 响应。"""
    try:
        return _run(p0, approval_id, revision)
    except RuntimeError:
        return None


def _break_enqueue(p0: Any, monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    jobs = p0.app_module.media_jobs

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(message)

    monkeypatch.setattr(jobs, "enqueue", boom)


def test_p0_08_failed_enqueue_does_not_leave_ghost_activation(
    p0: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_id = p0.approve([p0.video_id])
    revision = p0.revision()
    _break_enqueue(p0, monkeypatch, "任务存储不可用（模拟入队失败）")

    response = _run_tolerating_crash(p0, approval_id, revision)

    assert response is not None, "入队失败不应把未捕获异常抛给调用方，应返回可读错误"
    assert response.status_code == 503, response.text
    assert "重试" in response.text

    node = p0.canvas_node(p0.video_id)
    assert node["data"].get("active_generation_id") is None, (
        "入队失败却留下了幽灵激活标记：节点会永远等待一个不存在的任务"
    )
    assert node["status"] == "ready" or node["status"] == "draft", node["status"]


def test_p0_08_failed_enqueue_leaves_approval_reusable(
    p0: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_id = p0.approve([p0.video_id])
    jobs = p0.app_module.media_jobs
    real_enqueue = jobs.enqueue

    _break_enqueue(p0, monkeypatch, "任务存储不可用（模拟入队失败）")
    _run_tolerating_crash(p0, approval_id, p0.revision())

    # 恢复存储：用户刷新页面后拿最新 revision，用同一份审批重试。
    monkeypatch.setattr(jobs, "enqueue", real_enqueue)
    revision = p0.revision()
    response = _run(p0, approval_id, revision)

    assert response.status_code == 200, (
        f"审批在失败后被白白作废，重试拿到 {response.status_code}：{response.text}"
    )
    job = p0.wait_job(response.json()["job_id"])
    assert job["status"] == "succeeded", job

    node = p0.canvas_node(p0.video_id)
    assert node["status"] == "done", node
    takes = node["data"].get("takes") or []
    assert len(takes) == 1, f"重试产生了多条历史版本（可能重复计费）：{len(takes)}"


def test_p0_08_replayed_run_does_not_create_second_job(p0: Any) -> None:
    """成功之后重放同一个请求（例如 HTTP 响应丢了、用户又点了一次）：

    - 必须落到**同一个 job_id**（job_id 由"审批 + 计划"决定，不含不断前进的 revision）；
    - 不能再调一次模型（provider 只被调用一次）、不能多出历史版本。
    """
    approval_id = p0.approve([p0.video_id])
    first = _run(p0, approval_id, p0.revision())
    assert first.status_code == 200, first.text
    first_job_id = first.json()["job_id"]
    job = p0.wait_job(first_job_id)
    assert job["status"] == "succeeded", job
    assert len(p0.provider.calls) == 1

    replay = _run(p0, approval_id, p0.revision())
    assert replay.status_code == 200, replay.text
    assert replay.json()["job_id"] == first_job_id
    assert replay.json()["created"] is False, "重放不应新开一单"
    assert len(p0.provider.calls) == 1, "重放导致模型被第二次调用（重复计费）"
    node = p0.canvas_node(p0.video_id)
    assert len(node["data"].get("takes") or []) == 1
