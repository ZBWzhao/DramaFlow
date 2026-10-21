"""P0-6：删除节点（含级联删边、严格 revision、生成中删除不复活节点）。

背景：画布此前只有"增"没有"删"——后端没有 `DELETE /api/canvas/nodes/{id}`，
前端右键菜单的"删除节点"是灰的，废稿只能一直留着。删除看似简单，真正的坑在三条：

1. 删节点必须把它的入边/出边一起删掉，否则画布会留下"悬空边"，体检和导出都会报错；
2. 删除是写操作，必须带 revision：基于旧视图删节点，会把别人刚连上的线一起删掉；
3. 生成中的节点被删除时，Worker 不能在写回时把节点"复活"或把任务判成失败——
   结果已经没有归属，应当记成"过期丢弃"。
"""

from __future__ import annotations


def _node_ids(p0) -> set[str]:
    return {node["id"] for node in p0.canvas_doc()["nodes"]}


def _incident_edges(doc: dict, node_id: str) -> list[dict]:
    return [edge for edge in doc["edges"] if node_id in (edge["source"], edge["target"])]


def test_delete_node_removes_node_and_incident_edges(p0):
    """正常删除：节点消失、相关连线消失、无关节点保留、revision 前进。"""
    doc = p0.canvas_doc()
    assert p0.video_id in _node_ids(p0)
    incident = _incident_edges(doc, p0.video_id)
    assert incident, "最小画布应有一条指向视频节点的连线"

    response = p0.delete_node(p0.video_id, int(doc["revision"]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["removed"] == 1
    assert body["removed_edges"] == len(incident)

    after = p0.canvas_doc()
    ids = {node["id"] for node in after["nodes"]}
    assert p0.video_id not in ids
    assert p0.prompt_id in ids and p0.image_id in ids
    assert not _incident_edges(after, p0.video_id), "删除节点后不应留下悬空边"
    assert int(after["revision"]) > int(doc["revision"])


def test_delete_node_without_revision_is_rejected(p0):
    """不缺省放行：不带 revision 的删除必须被拒绝，节点仍在。"""
    response = p0.delete_node(p0.video_id)
    assert response.status_code in {400, 409, 422, 428}, response.text
    assert p0.video_id in _node_ids(p0)


def test_delete_node_with_stale_revision_is_rejected(p0):
    """过期 revision：409 + 明确的冲突 code，节点仍在。"""
    response = p0.delete_node(p0.video_id, p0.revision() - 1)
    assert response.status_code == 409, response.text
    assert response.json().get("code") == "canvas_revision_conflict"
    assert p0.video_id in _node_ids(p0)


def test_delete_missing_node_returns_404(p0):
    response = p0.delete_node("node-does-not-exist", p0.revision())
    assert response.status_code == 404, response.text


def test_deleted_prompt_node_makes_generation_not_ready(p0):
    """删掉提示词节点后，生成卡回到"没有可用提示词"，而不是拿到空提示词照常出片。"""
    response = p0.delete_node(p0.prompt_id, p0.revision())
    assert response.status_code == 200, response.text

    plan = p0.client.get("/api/canvas/generation-plan", params={"name": "main"})
    assert plan.status_code == 200, plan.text
    jobs = {job["node_id"]: job for job in plan.json()["jobs"]}
    assert p0.video_id in jobs
    assert jobs[p0.video_id]["ready"] is False
    assert jobs[p0.video_id]["ready_reason"]


def test_deleting_node_during_generation_discards_result(p0):
    """生成中删除节点：任务记 discarded，不复活节点、不落 take、不判失败。"""
    p0.provider.delay = 0.4
    approval = p0.approve([p0.video_id])
    job_id = p0.run([p0.video_id], approval)

    response = p0.delete_node(p0.video_id, p0.revision())
    assert response.status_code == 200, response.text

    job = p0.wait_job(job_id)
    assert job["status"] == "discarded", job
    assert job.get("error") is None, job
    doc = p0.canvas_doc()
    assert p0.video_id not in {node["id"] for node in doc["nodes"]}
    assert not _incident_edges(doc, p0.video_id)
