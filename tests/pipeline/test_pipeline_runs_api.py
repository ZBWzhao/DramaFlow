"""管线运行 REST/SSE 路由测试（Task 3 brief Step 4）。

模式照 test_pipeline_api.py：TestClient 走真实 app，runs 依赖经
server.pipeline_runs.configure/monkeypatch 注入——fake executor + 真 RunStore
（pipeline_test 库）+ 真 EventBroker。asyncpg pool 绑定事件循环，所以 pool 建在
app 自己的循环里（client.portal.call），测完同路收池，不留悬挂连接。

SSE 用 httpx 的 client.stream 收流（brief 指定方式）。live 事件用「executor 延迟
0.4s + 订阅先行」保证确定性：hello 首帧在订阅后立刻下发，executor 的事件都在
延迟窗口之后才 publish，不存在丢事件竞态。
"""
from __future__ import annotations

import asyncio
import functools
import json
import threading
import time
from types import SimpleNamespace

import asyncpg
import pytest

from core.pipeline_run import EventBroker, PipelineRunner, RunStore
from core.postgres import PostgresDatabase
from core.skill_executor import StageResult
from test_pipeline_repository import TEST_DSN, pipeline_test_database  # noqa: F401  session 级 DB 夹具


class FakeLLM:
    """registry 只在 /api/health 里读 .model；会话链路本测试不触。"""

    model = "fake-model"

    async def simple(self, messages):
        return '{"worldview": "默认世界观"}'


class FakeExecutor:
    """可延迟、可一次性失败的替身； 不碰 LLM/门/临时目录。"""

    def __init__(self):
        self.calls: list[str] = []
        self.fail_outcome = False
        self.delay = 0.0

    async def __call__(self, db, project, stage, inputs, **kw):
        self.calls.append(stage)
        if self.delay:
            await asyncio.sleep(self.delay)
        if stage == "outline" and self.fail_outcome:
            self.fail_outcome = False
            return StageResult(False, stage, 5, ["gate: fail"], [{"gate": "x", "detail": "d"}])
        return StageResult(True, stage, 1, ["ok"], [])


@pytest.fixture()
def runs_api(pipeline_test_database, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import server.app as app_module
    from server import pipeline_runs as runs_module
    from core.canvas_repository import create_canvas_repository
    from core.session_repository import JsonlSessionRepository
    from server.registry import SessionRegistry

    database = PostgresDatabase(TEST_DSN, min_size=1, max_size=2)
    store = RunStore(database)
    broker = EventBroker()
    executor = FakeExecutor()
    runner = PipelineRunner(store, database, executor=executor, broker=broker)
    monkeypatch.setattr(runs_module, "_store", store)
    monkeypatch.setattr(runs_module, "_runner", runner)
    monkeypatch.setattr(runs_module, "_broker", broker)

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    registry = SessionRegistry(
        llm=FakeLLM(),
        workspace=str(workspace),
        canvas_repository=create_canvas_repository(workspace),
        session_repository=JsonlSessionRepository(tmp_path / "sessions"),
    )
    monkeypatch.setattr(app_module, "WORKSPACE", workspace)
    monkeypatch.setattr(app_module, "registry", registry)

    with TestClient(app_module.app) as client:
        # 池必须建在 app 自己的循环里；PostgresDatabase.start() 会跑全量迁移
        # （0002/0003 在 pipeline_test 上不可用），所以直接 create_pool 装配。
        pool = client.portal.call(
            functools.partial(asyncpg.create_pool, TEST_DSN,
                              min_size=1, max_size=2, command_timeout=30)
        )
        database.pool = pool
        client.portal.call(pool.execute, "TRUNCATE pipeline_runs")
        client.portal.call(pool.execute, "TRUNCATE pipeline_artifacts")
        yield SimpleNamespace(client=client, store=store, broker=broker, executor=executor)
        # 先停推进任务再收池（app 自己的循环里收，不留悬挂连接）
        from server import pipeline_runs as runs_module
        client.portal.call(runs_module.shutdown)
        if database.pool is not None:
            client.portal.call(database.close)


def _create_run(client, project="proj-runs", novel="短篇小说正文。" * 12) -> str:
    response = client.post(
        "/api/pipeline/runs", json={"project_id": project, "novel_text": novel}
    )
    assert response.status_code == 200, response.text
    return response.json()["run_id"]


def _poll_run(client, run_id: str, statuses: set[str], timeout: float = 6.0) -> dict:
    """轮询直到运行进入目标状态（推进任务跑在 app 循环里，HTTP 轮询是唯一同步点）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = client.get(f"/api/pipeline/runs/{run_id}").json()
        if row["status"] in statuses:
            return row
        time.sleep(0.05)
    raise AssertionError(f"运行未在 {timeout}s 内进入 {statuses}，最后状态：{row['status']}")


def _read_sse(response) -> list[dict]:
    return [json.loads(line[len("data: "):])
            for line in response.iter_lines()
            if line.startswith("data: ")]


# ---------------------------------------------------------------------------
# REST：创建 → gated → approve → done
# ---------------------------------------------------------------------------

def test_create_run_persists_row_and_pauses_at_gate(runs_api):
    client = runs_api.client
    response = client.post(
        "/api/pipeline/runs", json={"project_id": "proj-runs", "novel_text": "小说正文……"}
    )
    body = response.json()
    assert response.status_code == 200 and body["run_id"]
    assert body["status"] == "running" and body["stage"] == "outline"

    row = _poll_run(client, body["run_id"], {"gated"})  # 推进任务在 app 循环里，轮询等它
    assert runs_api.executor.calls == ["outline"]  # 确认门前不推进

    assert row["project_id"] == "proj-runs"
    assert row["status"] == "gated" and row["stage"] == "outline"
    assert row["stage_status"]["outline"]["status"] == "done"
    assert [c["stage"] for c in row["history"]] == ["outline"]

    # POST approve 后（wait_idle ≈ 轮询到终态）status done
    assert client.post(f"/api/pipeline/runs/{body['run_id']}/approve", json={}).status_code == 200
    row = _poll_run(client, body["run_id"], {"done"})
    assert row["history"][0]["stage"] == "outline"
    # Task 6 起六阶段规格齐备：approve 后一路推进到 storyboard 才 done。
    assert runs_api.executor.calls == ["outline", "script", "characters",
                                       "art", "staging", "storyboard"]


def test_create_run_validation_and_missing_run(runs_api):
    client = runs_api.client
    assert client.post("/api/pipeline/runs", json={"project_id": " ", "novel_text": "x"}).status_code == 400
    assert client.post("/api/pipeline/runs", json={"project_id": "p", "novel_text": "  "}).status_code == 400
    assert client.get("/api/pipeline/runs/missing").status_code == 404
    assert client.post("/api/pipeline/runs/missing/approve", json={}).status_code == 404
    assert client.post("/api/pipeline/runs/missing/retry", json={"stage": "outline"}).status_code == 404
    assert client.post("/api/pipeline/runs/missing/feedback", json={"text": "x"}).status_code == 404


def test_approve_rejects_non_gated_runs(runs_api):
    client = runs_api.client
    run_id = _create_run(client)
    _poll_run(client, run_id, {"gated"})
    client.post(f"/api/pipeline/runs/{run_id}/approve", json={})
    _poll_run(client, run_id, {"done"})
    # done 状态再确认：409——确认门不可跳过/不可重复
    response = client.post(f"/api/pipeline/runs/{run_id}/approve", json={})
    assert response.status_code == 409


def test_retry_resets_failed_stage_and_reruns(runs_api):
    client = runs_api.client
    runs_api.executor.fail_outcome = True
    run_id = _create_run(client)
    row = _poll_run(client, run_id, {"failed"})
    assert row["stage"] == "outline"
    assert row["findings"] == [{"gate": "x", "detail": "d"}]

    # retry 守卫：非法阶段 400、成功运行 409
    assert client.post(f"/api/pipeline/runs/{run_id}/retry", json={"stage": "nope"}).status_code == 400

    response = client.post(f"/api/pipeline/runs/{run_id}/retry", json={"stage": "outline"})
    assert response.status_code == 200, response.text
    # outline 是确认门阶段：重跑过门后再次停在 gated，需人工再确认（门不可跳过）
    row = _poll_run(client, run_id, {"gated"})
    assert runs_api.executor.calls == ["outline", "outline"]
    assert row["stage_status"]["outline"]["attempts"] == 1
    assert client.post(f"/api/pipeline/runs/{run_id}/approve", json={}).status_code == 200
    row = _poll_run(client, run_id, {"done"})
    assert row["findings"] == []  # 重试清掉上一轮失败 findings
    # done 后再 retry：409
    assert client.post(f"/api/pipeline/runs/{run_id}/retry", json={"stage": "outline"}).status_code == 409


def test_feedback_appends_lines(runs_api):
    client = runs_api.client
    run_id = _create_run(client)
    assert client.post(f"/api/pipeline/runs/{run_id}/feedback",
                       json={"text": "第一集节奏太慢"}).status_code == 200
    assert client.post(f"/api/pipeline/runs/{run_id}/feedback",
                       json={"text": "主角出场提前"}).status_code == 200
    row = client.get(f"/api/pipeline/runs/{run_id}").json()
    assert row["feedback"] == "第一集节奏太慢\n主角出场提前"
    assert client.post(f"/api/pipeline/runs/{run_id}/feedback", json={"text": "  "}).status_code == 400


# ---------------------------------------------------------------------------
# SSE：hello 快照、live 事件、终态收流、approve 后同流收尾
#
# starlette TestClient 的流式响应是"缓冲到 ASGI 调用结束"（handle_request 阻塞在
# portal.call），所以 client.stream 只能测会自行收流的终态场景（failed/done）；
# gated 流按设计不终止——这两个场景改为直接驱动端点返回的生成器（sse_direct
# 夹具，自建 pool 于用例自身循环，不经 TestClient）。
# ---------------------------------------------------------------------------

@pytest.fixture()
def sse_direct(pipeline_test_database, monkeypatch):
    """不经 TestClient 的 runs 依赖：pool 由用例在自己的 asyncio.run 循环里装配。"""
    from server import pipeline_runs as runs_module

    database = PostgresDatabase(TEST_DSN, min_size=1, max_size=2)
    store = RunStore(database)
    broker = EventBroker()
    executor = FakeExecutor()
    runner = PipelineRunner(store, database, executor=executor, broker=broker)
    monkeypatch.setattr(runs_module, "_store", store)
    monkeypatch.setattr(runs_module, "_runner", runner)
    monkeypatch.setattr(runs_module, "_broker", broker)
    yield SimpleNamespace(store=store, runner=runner, broker=broker,
                          executor=executor, database=database)


async def _collect(gen, *, stop_after=None, expect_timeout=False, timeout=5.0):
    """逐帧消费 SSE 生成器；expect_timeout=True 时把「等不到下一帧」当成功返回。"""
    events = []
    while True:
        try:
            events.append(json.loads((await asyncio.wait_for(gen.__anext__(), timeout))
                                     .removeprefix("data: ")))
        except asyncio.TimeoutError:
            if expect_timeout:
                return events, True  # 流保持打开（gate 非终态）
            raise
        if stop_after is not None and len(events) >= stop_after:
            return events, False


def test_sse_hello_carries_snapshot_when_events_were_missed(sse_direct):
    """broker 是 fire-and-forget：晚连上的客户端从 hello 快照同步状态，且 gate 不收流。"""

    async def scenario() -> None:
        database = sse_direct.database
        database.pool = await asyncpg.create_pool(
            TEST_DSN, min_size=1, max_size=2, command_timeout=30
        )
        try:
            await database.pool.execute("TRUNCATE pipeline_runs")
            await database.pool.execute("TRUNCATE pipeline_artifacts")
            from server import pipeline_runs as runs_module

            run = await sse_direct.store.create("proj-sse", "小说正文……")
            await sse_direct.runner.start(run["run_id"])
            # executor 无延迟：订阅前 run 已 gated，事件全部错过
            while (await sse_direct.store.get(run["run_id"]))["status"] != "gated":
                await asyncio.sleep(0.02)

            response = await runs_module.run_events(run["run_id"])
            events, stayed_open = await _collect(response.body_iterator,
                                                 expect_timeout=True, timeout=0.6)
            assert events[0]["type"] == "hello"
            assert events[0]["status"] == "gated" and events[0]["stage"] == "outline"
            assert stayed_open is True  # gate 非终态：流保持打开等 approve 后续事件
            await response.body_iterator.aclose()  # 触发 finally 反注册
            assert sse_direct.broker._subs.get(run["run_id"]) is None
        finally:
            await database.pool.close()

    asyncio.run(scenario())


def test_sse_live_events_deliver_gate_in_order(sse_direct):
    """订阅先于推进：hello → stage → gate 依序到达；hello 快照是 running。"""

    async def scenario() -> None:
        database = sse_direct.database
        database.pool = await asyncpg.create_pool(
            TEST_DSN, min_size=1, max_size=2, command_timeout=30
        )
        try:
            await database.pool.execute("TRUNCATE pipeline_runs")
            await database.pool.execute("TRUNCATE pipeline_artifacts")
            from server import pipeline_runs as runs_module

            run = await sse_direct.store.create("proj-sse", "小说正文……")
            response = await runs_module.run_events(run["run_id"])  # 先订阅
            await sse_direct.runner.start(run["run_id"])           # 后推进（同循环）
            events, _ = await _collect(response.body_iterator, stop_after=3)
            assert [e["type"] for e in events] == ["hello", "stage", "gate"]
            assert events[0]["status"] == "running"
            assert events[1] == {"type": "stage", "stage": "outline", "status": "running"}
            assert events[2]["type"] == "gate" and events[2]["stage"] == "outline"
            await response.body_iterator.aclose()
        finally:
            await database.pool.close()

    asyncio.run(scenario())


def test_sse_terminal_run_closes_stream_after_hello(runs_api):
    """订阅时已是终态（done/cancelled）：hello 之后立即收流，连接与队列不泄漏。

    done 流现在会自行终止，所以 client.stream 可用（gated 流不行，见上文说明）。
    """
    client = runs_api.client
    run_id = _create_run(client)
    _poll_run(client, run_id, {"gated"})
    assert client.post(f"/api/pipeline/runs/{run_id}/approve", json={}).status_code == 200
    _poll_run(client, run_id, {"done"})
    with client.stream("GET", f"/api/pipeline/runs/{run_id}/events") as response:
        events = _read_sse(response)  # 不主动 break：hello 后服务端自己收流
    assert [e["type"] for e in events] == ["hello"]
    assert events[0]["status"] == "done" and events[0]["run_id"] == run_id


def test_sse_failed_event_terminates_stream(runs_api):
    client = runs_api.client
    runs_api.executor.fail_outcome = True
    runs_api.executor.delay = 0.2
    run_id = _create_run(client)
    with client.stream("GET", f"/api/pipeline/runs/{run_id}/events") as response:
        events = _read_sse(response)  # 不主动 break：failed 终态由服务端收流
    assert [e["type"] for e in events] == ["hello", "stage", "failed"]
    assert events[-1]["stage"] == "outline"
    assert events[-1]["findings"] == [{"gate": "x", "detail": "d"}]


def test_sse_stream_completes_after_approve(runs_api):
    """端到端：hello → stage → gate →（另一连接 approve）→ done，流自行收尾。

    TestClient 上下文内所有请求共享同一个 portal 事件循环，阻塞在 SSE 上的
    主线程不妨碍另一线程的 approve POST 在同一循环里排队执行。
    """
    client = runs_api.client
    runs_api.executor.delay = 0.2
    run_id = _create_run(client)

    def _approve_later():
        time.sleep(1.0)  # > executor 延迟：gate 事件先落袋，approve 再放行
        response = client.post(f"/api/pipeline/runs/{run_id}/approve", json={})
        assert response.status_code == 200

    approver = threading.Thread(target=_approve_later)
    approver.start()
    try:
        with client.stream("GET", f"/api/pipeline/runs/{run_id}/events") as response:
            events = _read_sse(response)  # done 终态服务端收流，循环自然结束
    finally:
        approver.join()
    types = [e["type"] for e in events]
    assert types[0] == "hello" and types[-1] == "done"
    assert "gate" in types
