"""produce 撞轮次上限后的行为：自动续跑（治本）+ 终态如实透出（不再静默）。

背景：_PRODUCE_INSTRUCTION 的完整活（30 段 × storyboard/prompt/video_gen/连线）
远超 AGENT_MAX_TURNS=40，两次实跑都停在 max_turns=limited，而 job 一律标
succeeded——制作页片段队列只剩种子节点，用户毫无感知。
"""
import time

from conftest import make_project

SCRIPT = "第1集 开场\n林生走进陈家客厅，拿起桌上的茶杯。\n第2集 争执\n林生与苏晚在陈家客厅争吵。\n"


class FakeAgentRunner:
    """按剧本回放每条腿的终态；模拟真实 runner 的 finally（释放会话运行权）。"""

    def __init__(self, registry, statuses):
        self.registry = registry
        self.statuses = list(statuses)
        self.prompts: list[str] = []
        self.max_turns_seen: list[int] = []

    async def __call__(self, live, request, run_id):
        self.prompts.append(request.prompt)
        self.max_turns_seen.append(live.agent.max_turns)
        status = self.statuses.pop(0) if self.statuses else "completed"
        live.agent.last_status = status
        # 真实 _execute_agent_run 的 finally 会释放运行权，不做会挡住下一腿的 claim
        await self.registry.run_states.finish(run_id, status=status)
        live.current_run_id = None


def _arm(monkeypatch, pipeline, statuses):
    from server import studio as studio_module

    runner = FakeAgentRunner(pipeline.registry, statuses)
    monkeypatch.setattr(studio_module, "_agent_runner", runner)
    return runner


def _produce_and_wait(client, canvas, episode=1, timeout=15):
    job = client.post("/api/studio/produce", json={"canvas": canvas, "episode": episode}).json()["job"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/studio/jobs/{job['job_id']}").json()
        if data.get("status") not in {"queued", "running"}:
            return data
        time.sleep(0.1)
    raise AssertionError("produce 任务没有在限时内结束")


def test_produce_resumes_after_max_turns(pipeline, monkeypatch):
    runner = _arm(monkeypatch, pipeline, ["limited", "completed"])
    canvas = make_project(pipeline.client, title="续跑项目", script=SCRIPT)["canvas"]

    job = _produce_and_wait(pipeline.client, canvas)

    assert job["status"] == "succeeded"
    assert len(runner.prompts) == 2, "limited 后应自动续跑一次"
    assert "续" in runner.prompts[1] or "补齐" in runner.prompts[1], "续跑提示词要说明只补缺失"
    assert job.get("resume_attempts") == 1
    assert all(t > 40 for t in runner.max_turns_seen), "produce 腿要有独立放大的轮次预算"


def test_produce_reports_limited_after_exhausted_legs(pipeline, monkeypatch):
    from server import studio as studio_module

    runner = _arm(monkeypatch, pipeline, ["limited", "limited"])
    monkeypatch.setattr(studio_module, "PRODUCE_MAX_LEGS", 2)
    canvas = make_project(pipeline.client, title="透出项目", script=SCRIPT)["canvas"]

    job = _produce_and_wait(pipeline.client, canvas)

    assert job["status"] == "limited"
    assert "继续" in (job.get("error") or "")
    assert len(runner.prompts) == 2


def test_produce_reports_agent_error_as_failed(pipeline, monkeypatch):
    runner = _arm(monkeypatch, pipeline, ["error"])
    canvas = make_project(pipeline.client, title="报错项目", script=SCRIPT)["canvas"]

    job = _produce_and_wait(pipeline.client, canvas)

    assert job["status"] == "failed"
    assert job.get("error")
