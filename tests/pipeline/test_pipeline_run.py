"""管线运行状态机测试（Task 3 brief Step 1）：真库 RunStore + 注入 fake executor。

两层"真"夹一层"假"（照 test_skill_executor.py 的惯例）：
- DB 是真库——session 级夹具复用 test_pipeline_repository.py 的 pipeline_test 库；
  每用例独立 asyncio.run + 独立 pool + TRUNCATE（本仓库无 pytest-asyncio，
  brief 伪码里的 pytest.mark.asyncio 一律转 asyncio.run 风格）。
- 状态机、确认门、事件契约、断点续跑、无脏数据约束全部是真的。
- 只有 executor 是假的：同步返回 StageResult，不碰 LLM/质量门/临时目录。

STAGE_SPECS 现在只有 outline：默认路径下 outline 过门（approve 后）即 done
（没有剩余可运行阶段）。要验证全 STAGE_ORDER 推进，用 monkeypatch 把
skill_executor.STAGE_SPECS 换成六阶段桩——Task 5/6 落地真规格后行为不变。
"""
from __future__ import annotations

import asyncio

import asyncpg
import pytest

from core import pipeline_repository, skill_executor
from core.pipeline_run import EventBroker, GATE_STAGE, PipelineRunner, RunStore, STAGE_ORDER
from core.postgres import PostgresDatabase
from core.skill_executor import StageResult
from test_pipeline_repository import TEST_DSN, pipeline_test_database  # noqa: F401  session 级库装配


class FakeExecutor:
    """brief Step 1 的替身：记录调用；可指定一次性失败阶段与基建炸点。"""

    def __init__(self, fail_stage: str = "", emit_events: bool = False, delay: float = 0.0):
        self.calls: list[str] = []
        self.fail_stage = fail_stage
        self.emit_events = emit_events
        self.boom_stage = ""
        self.delay = delay

    async def __call__(self, db, project, stage, inputs, **kw):
        self.calls.append(stage)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.boom_stage and stage == self.boom_stage:
            raise RuntimeError("llm 连接被对端重置")
        on_event = kw.get("on_event")
        if self.emit_events and callable(on_event):
            on_event({"type": "gate", "ok": True, "round": 1})
            on_event({"type": "done", "ok": True})
        if stage == self.fail_stage:
            self.fail_stage = ""
            return StageResult(False, stage, 5, ["gate: fail"], [{"gate": "x", "detail": "不合规"}])
        return StageResult(True, stage, 1, ["ok"], [])


# 六阶段桩规格：让 runner 把 STAGE_ORDER 全部视为可运行（Task 5/6 前的状态机全貌）。
ALL_STAGE_SPECS_STUB = {stage: {"artifact": stage, "gate_needs": []} for stage in STAGE_ORDER}


def _run_scenario(scenario) -> None:
    """在同一个 asyncio.run 里装配 pool + TRUNCATE + 跑用例 + 关 pool。"""

    async def _runner() -> None:
        database = PostgresDatabase(TEST_DSN, min_size=1, max_size=2)
        database.pool = await asyncpg.create_pool(
            TEST_DSN, min_size=1, max_size=2, command_timeout=30
        )
        try:
            await database.pool.execute("TRUNCATE pipeline_runs")
            await database.pool.execute("TRUNCATE pipeline_artifacts")
            await scenario(database)
        finally:
            await database.pool.close()

    asyncio.run(_runner())


def _drain(queue: asyncio.Queue) -> list[dict]:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def test_runner_pauses_at_confirmation_gate(pipeline_test_database):
    async def scenario(database) -> None:
        store, broker = RunStore(database), EventBroker()
        executor = FakeExecutor(emit_events=True)
        runner = PipelineRunner(store, database, executor=executor, broker=broker)
        run = await store.create("p1", "小说正文……")
        events = broker.subscribe(run["run_id"])

        await runner.start(run["run_id"])
        await runner.wait_idle()
        state = await store.get(run["run_id"])
        assert state["status"] == "gated" and state["stage"] == GATE_STAGE
        assert executor.calls == ["outline"]  # 确认门前不推进
        # 事件契约（订阅先于推进，顺序确定）：stage 起跑 → 执行器内部 gate/done
        # 重打标 stage_gate/stage_done → run 级确认门 gate（stage 标签齐全）。
        seq = [(e["type"], e.get("stage")) for e in _drain(events)]
        assert seq == [("stage", "outline"), ("stage_gate", "outline"),
                       ("stage_done", "outline"), ("gate", "outline")]

        approved = await store.approve(run["run_id"])
        assert approved is True
        await runner.resume(run["run_id"])
        await runner.wait_idle()
        state = await store.get(run["run_id"])
        assert state["status"] == "done"
        # 当前 STAGE_SPECS 只有 outline：后续阶段无规格、不可运行 → 无剩余阶段即 done
        # （Task 5/6 补齐规格后收紧为全 STAGE_ORDER，见下一用例）。
        assert [c["stage"] for c in await store.stage_history(run["run_id"])] == ["outline"]
        assert state["stage_status"]["outline"] == {
            "status": "done", "attempts": 1, "gate_runs": 1, "findings": [],
        }

    _run_scenario(scenario)


def test_full_stage_order_traversal_when_all_specs_exist(pipeline_test_database, monkeypatch):
    monkeypatch.setattr(skill_executor, "STAGE_SPECS", dict(ALL_STAGE_SPECS_STUB))

    async def scenario(database) -> None:
        store, broker = RunStore(database), EventBroker()
        executor = FakeExecutor()
        runner = PipelineRunner(store, database, executor=executor, broker=broker)
        run = await store.create("p-all", "小说正文……")

        await runner.start(run["run_id"])
        await runner.wait_idle()
        assert (await store.get(run["run_id"]))["status"] == "gated"  # 确认门仍拦在 outline 后
        assert executor.calls == ["outline"]

        await store.approve(run["run_id"])
        await runner.resume(run["run_id"])
        await runner.wait_idle()
        state = await store.get(run["run_id"])
        assert state["status"] == "done" and state["stage"] == STAGE_ORDER[-1]
        assert executor.calls == STAGE_ORDER  # 断点续跑不重跑 outline
        assert [c["stage"] for c in await store.stage_history(run["run_id"])] == STAGE_ORDER

    _run_scenario(scenario)


def test_stage_failure_marks_failed_and_preserves_upstream(pipeline_test_database, monkeypatch):
    monkeypatch.setattr(skill_executor, "STAGE_SPECS", dict(ALL_STAGE_SPECS_STUB))

    async def scenario(database) -> None:
        # 预置上游产物：script 失败绝不能碰它
        await pipeline_repository.save(database, "p-fail", "outline",
                                       {"source": "上游大纲", "episodes": []})
        store, broker = RunStore(database), EventBroker()
        executor = FakeExecutor(fail_stage="script")
        runner = PipelineRunner(store, database, executor=executor, broker=broker)
        run = await store.create("p-fail", "小说正文……")
        events = broker.subscribe(run["run_id"])

        await runner.start(run["run_id"])
        await runner.wait_idle()
        # 确认门先拦住（outline 过门必停，红线），放行后才轮到 script 失败
        assert (await store.get(run["run_id"]))["status"] == "gated"
        assert await store.approve(run["run_id"]) is True
        await runner.resume(run["run_id"])
        await runner.wait_idle()
        state = await store.get(run["run_id"])
        assert state["status"] == "failed" and state["stage"] == "script"
        assert state["findings"] == [{"gate": "x", "detail": "不合规"}]
        assert state["stage_status"]["outline"]["status"] == "done"
        assert state["stage_status"]["script"]["status"] == "failed"
        saved = await pipeline_repository.get(database, "p-fail", "outline")
        assert saved["content"] == {"source": "上游大纲", "episodes": []}  # 无脏数据
        assert [e["type"] for e in _drain(events)][-1] == "failed"

        # 重试失败阶段：重置后重跑 → 一路过到 done
        await runner.retry(run["run_id"], "script")
        await runner.wait_idle()
        state = await store.get(run["run_id"])
        assert state["status"] == "done" and state["findings"] == []  # retry 清掉旧 findings
        assert executor.calls == ["outline", "script", "script",
                                  "characters", "art", "staging", "storyboard"]
        assert state["stage_status"]["script"]["attempts"] == 1  # 重跑后的真实迭代数

    _run_scenario(scenario)


def test_infrastructure_exception_becomes_failed_run(pipeline_test_database):
    """Task 2 review 遗留：executor 炸出（不返 StageResult）必须落 failed，不静默挂死。"""

    async def scenario(database) -> None:
        store, broker = RunStore(database), EventBroker()
        executor = FakeExecutor()
        executor.boom_stage = "outline"
        runner = PipelineRunner(store, database, executor=executor, broker=broker)
        run = await store.create("p-boom", "小说正文……")
        events = broker.subscribe(run["run_id"])

        await runner.start(run["run_id"])
        await runner.wait_idle()  # 不上抛
        state = await store.get(run["run_id"])
        assert state["status"] == "failed" and state["stage"] == "outline"
        assert state["findings"][0]["gate"] == "infrastructure"
        assert "RuntimeError" in state["findings"][0]["detail"]
        assert state["stage_status"]["outline"]["status"] == "failed"
        types = [e["type"] for e in _drain(events)]
        assert types[-1] == "failed"
        assert await pipeline_repository.get(database, "p-boom", "outline") is None

    _run_scenario(scenario)


def test_gated_run_does_not_advance_and_start_is_idempotent(pipeline_test_database):
    async def scenario(database) -> None:
        store, broker = RunStore(database), EventBroker()
        executor = FakeExecutor(delay=0.05)
        runner = PipelineRunner(store, database, executor=executor, broker=broker)
        run = await store.create("p-idem", "小说正文……")

        # 快速连发两次 start：同一 run 只允许一个推进任务
        await runner.start(run["run_id"])
        await runner.start(run["run_id"])
        await runner.wait_idle()
        assert executor.calls == ["outline"]
        assert (await store.get(run["run_id"]))["status"] == "gated"

        # gated 红线：对 gated 状态再次 start 不推进
        await runner.start(run["run_id"])
        await runner.wait_idle()
        assert executor.calls == ["outline"]

        # done 状态同样不推进
        await store.approve(run["run_id"])
        await runner.resume(run["run_id"])
        await runner.wait_idle()
        assert (await store.get(run["run_id"]))["status"] == "done"
        calls_after_done = list(executor.calls)
        await runner.start(run["run_id"])
        await runner.wait_idle()
        assert executor.calls == calls_after_done

    _run_scenario(scenario)


def test_retry_guards(pipeline_test_database):
    async def scenario(database) -> None:
        store, broker = RunStore(database), EventBroker()
        executor = FakeExecutor(fail_stage="outline")
        runner = PipelineRunner(store, database, executor=executor, broker=broker)
        run = await store.create("p-guard", "小说正文……")
        await runner.start(run["run_id"])
        await runner.wait_idle()
        assert (await store.get(run["run_id"]))["status"] == "failed"

        with pytest.raises(ValueError):  # 未知阶段
            await runner.retry(run["run_id"], "nope")
        with pytest.raises(RuntimeError):  # 阶段与失败阶段不符
            await runner.retry(run["run_id"], "script")
        # 正确重试走通后（→ done），failed 之外的阶段守卫依旧
        await runner.retry(run["run_id"], "outline")
        await runner.wait_idle()
        with pytest.raises(RuntimeError):  # done 不可重试
            await runner.retry(run["run_id"], "outline")

    _run_scenario(scenario)


def test_store_crud_approve_guard_and_feedback(pipeline_test_database):
    async def scenario(database) -> None:
        store = RunStore(database)
        run = await store.create("p-store", "小说正文")
        assert run["status"] == "running" and run["stage"] == "outline"
        assert run["stage_status"] == {} and run["feedback"] == "" and run["findings"] is None
        assert (await store.get(run["run_id"]))["novel_text"] == "小说正文"
        assert await store.get("no-such-run") is None

        await store.update_stage(run["run_id"], "outline", {"status": "running", "attempts": 0})
        await store.update_stage(run["run_id"], "outline", {"status": "done", "attempts": 3, "findings": []})
        state = await store.get(run["run_id"])
        assert state["stage_status"]["outline"] == {"status": "done", "attempts": 3, "findings": []}
        assert state["stage"] == "outline"  # update_stage 把 stage 列指向当前阶段

        assert await store.approve(run["run_id"]) is False  # running 不可确认
        await store.set_status(run["run_id"], "gated")
        await store.set_status(run["run_id"], "failed", stage="outline",
                               findings=[{"gate": "x", "detail": "d"}])
        state = await store.get(run["run_id"])
        assert state["status"] == "failed" and state["stage"] == "outline"
        assert state["findings"] == [{"gate": "x", "detail": "d"}]
        await store.set_status(run["run_id"], "running")  # findings=None 保留原值
        assert (await store.get(run["run_id"]))["findings"] == [{"gate": "x", "detail": "d"}]

        await store.append_feedback(run["run_id"], "第一集节奏太慢")
        await store.append_feedback(run["run_id"], "主角出场提前")
        assert (await store.get(run["run_id"]))["feedback"] == "第一集节奏太慢\n主角出场提前"

        await store.set_status(run["run_id"], "gated")
        assert await store.approve(run["run_id"]) is True
        assert await store.approve(run["run_id"]) is False  # 重复确认拒绝

        with pytest.raises(ValueError):  # 非法状态提前拒绝，不撞 DB CHECK
            await store.set_status(run["run_id"], "bogus")

    _run_scenario(scenario)


def test_broker_fanout_unsubscribe_and_isolation() -> None:
    async def scenario() -> None:
        broker = EventBroker()
        q1, q2 = broker.subscribe("r1"), broker.subscribe("r1")
        q3 = broker.subscribe("r2")

        broker.publish("missing-run", {"n": 0})  # 无订阅者 no-op
        broker.publish("r1", {"n": 1})
        assert q1.get_nowait() == {"n": 1}
        assert q2.get_nowait() == {"n": 1}
        assert q3.empty()  # run 之间隔离

        broker.unsubscribe("r1", q1)
        broker.unsubscribe("r1", q1)  # 重复反注册安全
        broker.publish("r1", {"n": 2})
        assert q1.empty()
        assert q2.get_nowait() == {"n": 2}

    asyncio.run(scenario())
