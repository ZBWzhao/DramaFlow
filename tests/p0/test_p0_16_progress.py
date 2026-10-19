"""P0-16 生成进度：后端把粗粒度阶段写进 node.data.generation_progress，前端才能画真进度环。

阶段约定（total=3）：1 submitting（调用模型）/ 2 writing（下载与写回）/ 终态清空。
"""

import time


def _wait_node(p0, node_id: str, predicate, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = p0.canvas_node(node_id)
        if predicate(last):
            return last
        time.sleep(0.05)
    raise AssertionError(f"节点在 {timeout}s 内没有满足条件，最后状态：{last}")


def test_progress_is_written_while_generating_and_cleared_after(p0):
    """生成中能看到 step/total；成功后进度清空、状态 done。"""
    video_id = p0.video_id
    # 让 Provider 慢一点，才能在生成中途读到进度
    p0.provider.delay = 1.0
    approval_id = p0.approve([video_id])
    job_id = p0.run([video_id], approval_id)

    node = _wait_node(
        p0,
        video_id,
        lambda item: ((item.get("data") or {}).get("generation_progress") or {}).get("step", 0) >= 1,
    )
    progress = node["data"]["generation_progress"]
    assert progress["total"] == 3, progress
    assert progress["stage"] in {"submitting", "writing"}, progress
    assert node["status"] in {"generating", "done"}, node["status"]

    job = p0.wait_job(job_id)
    assert job["status"] == "succeeded", job

    node = p0.canvas_node(video_id)
    assert node["status"] == "done"
    assert (node["data"] or {}).get("generation_progress") is None
    # 进度字段只是展示数据，不能污染真实产物
    assert (node.get("outputs") or {}).get("video", {}).get("value", {}).get("path")


def test_progress_is_cleared_when_generation_fails(p0):
    """失败后不能留着「生成中 2/3」的残影。"""
    video_id = p0.video_id
    p0.provider.fail_with = RuntimeError("provider 挂了")
    approval_id = p0.approve([video_id])
    job_id = p0.run([video_id], approval_id)

    job = p0.wait_job(job_id)
    assert job["status"] == "failed", job

    node = p0.canvas_node(video_id)
    assert node["status"] == "error"
    assert (node["data"] or {}).get("generation_progress") is None
