"""P0-14：提交点的异常边界与重试退避。

背景：``/api/canvas/run`` 与 ``/api/canvas/compose`` 的提交点用 ``except Exception``
把**所有**异常都包装成 503 + "任务存储或画布暂时不可用，请重试"：

- 编程错误（签名不匹配、字段拼错、序列化失败）也会变成"重试就好"，用户反复点、日志里
  没有 traceback，真 bug 被掩盖；
- 真正的临时故障（Redis/Postgres 连接失败）和编程错误的处置不同：前者该 503 + 退回审批，
  后者该 500 + 留下 traceback，不该诱导用户重试。

修复前钉住的现状（RED）：
- 注入 ``TypeError``（非临时错误）→ 现在返回 **503**（应为 500，且文案不能是"暂时不可用"）；
- 注入 ``ConnectionError``（临时错误）→ 503 + 审批退回（这条现在就对，作为对照组）。
"""

from __future__ import annotations

from core.canvas_repository import CanvasConflictError

RUN_PAYLOAD = {
    "canvas": "main",
    "provider": "mock",
    "approved": True,
    "duration": 5,
    "resolution": "720p",
    "ratio": "16:9",
    "generate_audio": False,
}


def _post_run(p0, approval_id: str):
    return p0.client.post(
        "/api/canvas/run",
        json={
            **RUN_PAYLOAD,
            "only": [p0.video_id],
            "approval_id": approval_id,
            "revision": p0.revision(),
        },
    )


def test_programming_error_returns_500_not_503(p0, monkeypatch):
    """非预期异常：500 + 明确"内部错误"，不能伪装成"暂时不可用，请重试"。"""
    async def broken_enqueue(job):  # noqa: ANN001
        raise TypeError("enqueue() got an unexpected keyword argument 'payload'")

    monkeypatch.setattr(p0.app_module.media_jobs, "enqueue", broken_enqueue)
    approval = p0.approve([p0.video_id])
    response = _post_run(p0, approval)

    assert response.status_code == 500, response.text
    detail = str(response.json().get("detail") or "")
    assert "暂时不可用" not in detail, detail
    assert "内部错误" in detail, detail

    # 激活标记必须被回滚：不能留下指向"不存在任务"的幽灵激活。
    node = p0.canvas_node(p0.video_id)
    assert not node["data"].get("active_generation_id"), node["data"]


def test_transient_storage_error_returns_503_and_releases_approval(p0, monkeypatch):
    """临时故障：503 + 审批退回；恢复后同一审批可以重试成功。"""
    real_enqueue = p0.app_module.media_jobs.enqueue
    calls = {"n": 0}

    async def flaky_enqueue(job):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("redis: connection refused")
        return await real_enqueue(job)

    monkeypatch.setattr(p0.app_module.media_jobs, "enqueue", flaky_enqueue)
    approval = p0.approve([p0.video_id])

    first = _post_run(p0, approval)
    assert first.status_code == 503, first.text
    node = p0.canvas_node(p0.video_id)
    assert not node["data"].get("active_generation_id"), node["data"]

    second = _post_run(p0, approval)
    assert second.status_code == 200, second.text
    job = p0.wait_job(second.json()["job_id"])
    assert job["status"] == "succeeded", job


def test_conflict_retry_survives_two_consecutive_conflicts(p0, monkeypatch):
    """CAS 冲突重试仍然有效（加退避后不能把重试逻辑改坏）。"""
    repo = p0.app_module.registry.canvas_repository
    real_save = repo.save
    state = {"conflicts": 0}

    async def save_with_conflicts(canvas):  # noqa: ANN001
        if state["conflicts"] < 2:
            state["conflicts"] += 1
            raise CanvasConflictError(canvas.name, canvas.revision, canvas.revision + 1)
        return await real_save(canvas)

    monkeypatch.setattr(repo, "save", save_with_conflicts)
    approval = p0.approve([p0.video_id])
    response = _post_run(p0, approval)

    assert state["conflicts"] == 2
    assert response.status_code == 200, response.text
    job = p0.wait_job(response.json()["job_id"])
    assert job["status"] == "succeeded", job
    assert p0.canvas_node(p0.video_id)["status"] == "done"
