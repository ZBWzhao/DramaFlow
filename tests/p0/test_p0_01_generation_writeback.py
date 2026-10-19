"""P0-1：生成期间的并发编辑不应让 Worker 的产物写不回去。

现状（RED）：``_execute_media_job`` 在 1601 行 reload 画布、1643 行 save 写回，
中间没有任何冲突重试；只要这期间有别的写者（用户拖节点 / 前端自动保存坐标 /
另一个 Agent 改画布）提交了 revision，save 抛 CanvasConflictError，
异常直接冲出 Worker → 任务 failed。此时 Provider 已经真实生成并计费，
产物只剩磁盘上一个没人引用的文件，节点停在 generating，且不会自动重试。

修复后（GREEN）：Worker 写回遇到 revision 冲突应"重载 → 只重放自己的增量 → 重试"，
本测试在注入一次并发编辑后仍要求：任务成功、节点 done、take 落盘、并发编辑保留。
"""

from __future__ import annotations


def test_p0_01_control_single_writer_succeeds(p0) -> None:
    """对照组：同样的夹具、同样的 FakeProvider，只要没有并发编辑就必须成功。

    这一条证明上面的失败来自"并发写"本身，而不是夹具或 Provider 替身的问题。
    """
    approval = p0.approve([p0.video_id])
    job_id = p0.run([p0.video_id], approval)
    job = p0.wait_job(job_id)
    node = p0.canvas_node(p0.video_id)
    assert job.get("status") == "succeeded", {"job": job, "node": node}
    assert node.get("status") == "done"
    assert len(node.get("data", {}).get("takes") or []) == 1


def test_p0_01_artifact_survives_concurrent_edit(p0) -> None:
    approval = p0.approve([p0.video_id])
    # 在 Worker 将要写回 "done + take" 的那一刻，插入一次并发的用户编辑（拖动提示词卡）。
    race = p0.inflate_competing_edit_before_writeback(p0.prompt_id)
    job_id = p0.run([p0.video_id], approval)
    job = p0.wait_job(job_id)

    node = p0.canvas_node(p0.video_id)
    prompt = p0.canvas_node(p0.prompt_id)
    takes = node.get("data", {}).get("takes") or []
    diagnosis = {
        "job_status": job.get("status"),
        "job_error": job.get("error"),
        "provider_calls": len(p0.provider.calls),
        "race_injected": race["fired"],
        "node_status": node.get("status"),
        "takes": len(takes),
        "outputs": node.get("outputs"),
        "prompt_position": prompt.get("position"),
    }

    assert race["fired"] is True, f"竞态注入未触发（夹具失效）：{diagnosis}"
    # 费用已经真实发生：Provider 被调用了一次
    assert len(p0.provider.calls) == 1, diagnosis
    # 期望：产物最终落在画布上，任务成功
    assert job.get("status") == "succeeded", diagnosis
    assert node.get("status") == "done", diagnosis
    assert len(takes) == 1, diagnosis
    video = (node.get("outputs") or {}).get("video") or {}
    path = (video.get("value") or {}).get("path")
    assert path, diagnosis
    assert (p0.workspace / path).is_file(), diagnosis
    # 期望：并发的那次编辑没有被吞掉
    assert prompt["position"] == {"x": 999.0, "y": 999.0}, diagnosis


def test_p0_01_stale_task_does_not_overwrite_newer_generation(p0) -> None:
    """合并式写回不能把"旧任务覆盖新任务"变成合法行为。

    用户在生成期间又点了一次生成（同一个节点有更新一代的
    ``active_generation_id``）：旧任务写回时必须整份丢弃 —— 不写产物、不写 take，
    也不把新一代任务的 generating 状态改回 done。
    """
    approval = p0.approve([p0.video_id])
    race = p0.supersede_generation_before_writeback(p0.video_id)
    job_id = p0.run([p0.video_id], approval)
    job = p0.wait_job(job_id)

    node = p0.canvas_node(p0.video_id)
    results = (job.get("result") or {}).get("results") or []
    diagnosis = {
        "job_status": job.get("status"),
        "job_result": job.get("result"),
        "race_injected": race["fired"],
        "node_status": node.get("status"),
        "active_generation_id": node.get("data", {}).get("active_generation_id"),
        "takes": len(node.get("data", {}).get("takes") or []),
        "outputs": node.get("outputs"),
    }
    assert race["fired"] is True, f"竞态注入未触发（夹具失效）：{diagnosis}"
    assert len(p0.provider.calls) == 1, diagnosis
    # 旧任务结果必须被整份丢弃（结果里明确标记 stale_discarded）。
    # 注意：任务表当前仍把"正常返回但产物被丢弃"记成 succeeded（语义缺口，P1 统一）；
    # 这里只锁"产物不落地、不覆盖新一代任务"这条硬不变量。
    assert any(item.get("stale_discarded") for item in results), diagnosis
    assert node.get("data", {}).get("active_generation_id") == "gen_newer", diagnosis
    assert node.get("status") == "generating", diagnosis
    assert len(node.get("data", {}).get("takes") or []) == 0, diagnosis
    assert not (node.get("outputs") or {}).get("video"), diagnosis
