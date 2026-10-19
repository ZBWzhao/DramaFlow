"""P0-13：媒体任务的终态语义。

背景：``MediaJobWorker._run`` 只要 ``execute`` 没抛异常就 ``complete(failed=False)``，
完全不看返回值里的 ``ok``。于是两种"没成功"都被记成 ``succeeded``：

1. **stale_discarded**：Provider 已经出片、费用已经发生，但节点在写回前被新一代任务
   取代，产物整份丢弃。它不是成功（画布没拿到任何东西），也不该算失败
   （用户不该重试同一个计划），需要独立终态 ``discarded``。
2. **批次里部分失败**：一个画布任务里有 N 个节点，其中一个 Provider 报错，其余成功。
   前端看到 ``succeeded`` 会以为整批都好了，实际有节点停在 ``error``。

修复前钉住的现状（RED）：
- stale 场景 ``job.status == "succeeded"``；
- 部分失败 ``job.status == "succeeded"``。
修复后：``discarded`` / ``failed`` / ``succeeded`` 三态分明。
"""

from __future__ import annotations


def test_stale_task_is_marked_discarded(p0):
    """产物被丢弃的任务：记 ``discarded``，不冒充成功。"""
    race = p0.supersede_generation_before_writeback(p0.video_id)
    approval = p0.approve([p0.video_id])
    job_id = p0.run([p0.video_id], approval)
    job = p0.wait_job(job_id)

    assert race["fired"] is True
    results = (job.get("result") or {}).get("results") or []
    assert any(item.get("stale_discarded") for item in results), job
    assert job["status"] == "discarded", job

    node = p0.canvas_node(p0.video_id)
    assert node["data"]["active_generation_id"] == "gen_newer"
    assert (node["data"].get("takes") or []) == []
    assert node["outputs"] == {}


def test_partial_provider_failure_is_not_success(p0):
    """批次里有节点真的失败：任务记 ``failed``，节点停在 ``error``。"""
    p0.provider.fail_with = RuntimeError("provider exploded")
    approval = p0.approve([p0.video_id])
    job_id = p0.run([p0.video_id], approval)
    job = p0.wait_job(job_id)

    assert job["status"] == "failed", job
    assert job.get("error"), job
    node = p0.canvas_node(p0.video_id)
    assert node["status"] == "error"
    assert (node["data"].get("takes") or []) == []


def test_success_still_reports_succeeded(p0):
    """对照组：正常出片仍然是 ``succeeded``，语义没被收紧误伤。"""
    approval = p0.approve([p0.video_id])
    job_id = p0.run([p0.video_id], approval)
    job = p0.wait_job(job_id)

    assert job["status"] == "succeeded", job
    node = p0.canvas_node(p0.video_id)
    assert node["status"] == "done"
    assert len(node["data"]["takes"]) == 1
