"""管线运行状态机测试（Task 3 brief Step 1）：真库 RunStore + 注入 fake executor。

两层"真"夹一层"假"（照 test_skill_executor.py 的惯例）：
- DB 是真库——session 级夹具复用 test_pipeline_repository.py 的 pipeline_test 库；
  每用例独立 asyncio.run + 独立 pool + TRUNCATE（本仓库无 pytest-asyncio，
  brief 伪码里的 pytest.mark.asyncio 一律转 asyncio.run 风格）。
- 状态机、确认门、事件契约、断点续跑、无脏数据约束全部是真的。
- 只有 executor 是假的：同步返回 StageResult，不碰 LLM/质量门/临时目录。

STAGE_SPECS 真规格六阶段齐备（Task 5 outline+script、Task 6 制片四层）：approve 后
一路推进到 storyboard 才 done。要验证与规格表解耦的状态机全貌，仍可用 monkeypatch
把 skill_executor.STAGE_SPECS 换成六阶段桩——全推进语义两套规格下一致。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import asyncpg
import pytest

from core import pipeline_repository, skill_executor
from core.pipeline_run import EventBroker, GATE_STAGE, PipelineRunner, RunStore, STAGE_ORDER
from core.postgres import PostgresDatabase
from core.skill_executor import StageResult
from test_pipeline_repository import TEST_DSN, pipeline_test_database  # noqa: F401  session 级库装配

ROOT_DIR = Path(__file__).resolve().parents[2]
MIGRATION_0007 = ROOT_DIR / "migrations" / "0007_failure_recovery.sql"


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


# 六阶段桩规格：与真规格内容解耦地验证「runner 按全 STAGE_ORDER 推进」——
# fake executor 不碰产物，gate_needs 置空即不读上游。
ALL_STAGE_SPECS_STUB = {stage: {"artifact": stage, "gate_needs": []} for stage in STAGE_ORDER}


class SeedRecordingExecutor:
    """failure-recovery Task 2 替身：记录每次收到的种子 kwargs；首轮失败带回
    StageResult.draft，重试轮按「门过才落库」的 run_stage 约定写产物并成功。"""

    def __init__(self) -> None:
        self.seen: list[dict] = []
        self.fail_first = True

    async def __call__(self, db, project, stage, inputs, **kw):
        self.seen.append({"initial_draft": kw.get("initial_draft"),
                          "user_instruction": kw.get("user_instruction")})
        if self.fail_first:
            self.fail_first = False
            return StageResult(False, stage, 5, ["gate: fail"],
                               [{"gate": "x", "detail": "d"}], draft={"source": "x"})
        await pipeline_repository.save(db, project, stage, {"source": "y"})
        return StageResult(True, stage, 1, ["ok"], [], draft=None)


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
            # 0007 列（failure-recovery）在本文件每个场景前幂等补齐：session 级
            # bootstrap（test_pipeline_repository）只手工跑 0005/0006，新迁移
            # 落地沿用同一约定——ADD COLUMN IF NOT EXISTS 重复执行无害。
            await database.pool.execute(MIGRATION_0007.read_text(encoding="utf-8"))
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
        assert state["status"] == "done" and state["stage"] == STAGE_ORDER[-1]
        # Task 6 起六阶段规格齐备：approve 后一路推进到 storyboard 才 done。
        assert [c["stage"] for c in await store.stage_history(run["run_id"])] == list(STAGE_ORDER)
        assert state["stage_status"]["outline"] == {
            "status": "done", "attempts": 1, "gate_runs": 1, "findings": [],
        }
        assert state["stage_status"]["script"] == {
            "status": "done", "attempts": 1, "gate_runs": 1, "findings": [],
        }
        assert state["stage_status"]["storyboard"] == {
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
        drained = _drain(events)
        assert drained[-1]["type"] == "failed"
        # 执行器异常在 _drive_inner 内被拦：失败事件带真实阶段（非 ""）
        assert drained[-1]["stage"] == "outline"
        assert await pipeline_repository.get(database, "p-boom", "outline") is None

    _run_scenario(scenario)


class _BrokenLedgerStore(RunStore):
    """update_stage 永远炸的账本：模拟 stage_status 写库级基建失败。执行器异常
    在 _drive_inner 内就被拦下，只有这种 store/broker 级意外才逃得进 _drive
    外壳——外层失败事件的 stage 哨兵只有这条路径能验证。"""

    async def update_stage(self, run_id: str, stage: str, patch: dict) -> None:
        raise RuntimeError("stage_status 写库炸了")


def test_outer_shell_failure_stage_sentinel_is_last_known_stage(pipeline_test_database):
    """stage 哨兵（fastfollow-2）：外层外壳失败事件的 stage 不再发 ""，取
    stage_status 里最后一个已落账阶段（此处账本只有 outline，script 起跑即炸）。"""

    async def scenario(database) -> None:
        good = RunStore(database)
        run = await good.create("p-boom-outer", "小说正文……")
        # 账本预置 outline 已 done、run 从 script 续跑（确认门已批准过的形态）
        await good.update_stage(run["run_id"], "outline",
                                {"status": "done", "attempts": 1, "gate_runs": 1, "findings": []})
        await good.set_status(run["run_id"], "running", stage="script")

        store = _BrokenLedgerStore(database)
        broker = EventBroker()
        runner = PipelineRunner(store, database, executor=FakeExecutor(), broker=broker)
        events = broker.subscribe(run["run_id"])

        await runner.start(run["run_id"])
        await runner.wait_idle()  # 不上抛
        failed = [e for e in _drain(events) if e["type"] == "failed"]
        assert failed and failed[-1]["stage"] == "outline"   # 最后已知阶段，不是 ""
        assert "RuntimeError" in failed[-1]["error"]
        state = await good.get(run["run_id"])
        assert state["status"] == "failed"
        assert state["findings"][0]["gate"] == "runner"

    _run_scenario(scenario)


def test_outer_shell_failure_stage_sentinel_none_without_ledger(pipeline_test_database):
    """stage 哨兵的空账本分支：stage_status 无任何已落账阶段（update_stage 一炸
    账本就永远是空的）时，失败事件 stage 是 None 而非 ""——消费端按未知 fallback。"""

    async def scenario(database) -> None:
        store = _BrokenLedgerStore(database)
        broker = EventBroker()
        runner = PipelineRunner(store, database, executor=FakeExecutor(), broker=broker)
        run = await store.create("p-boom-none", "小说正文……")
        await store.set_status(run["run_id"], "running")   # stage 列默认 outline，账本仍空
        events = broker.subscribe(run["run_id"])

        await runner.start(run["run_id"])
        await runner.wait_idle()
        failed = [e for e in _drain(events) if e["type"] == "failed"]
        assert failed and failed[-1]["stage"] is None
        assert "RuntimeError" in failed[-1]["error"]

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


# ---------------------------------------------------------------------------
# failure-recovery Task 2：重试种子管道（stage_drafts 列 + 种子注入 + 端点透传）
# ---------------------------------------------------------------------------

def test_retry_seed_roundtrip_and_clear(pipeline_test_database):
    """种子的存/取/清：draft 按 stage 位隔离（jsonb 顶层合并/删位），instruction
    单值；clear 同时归零两位，但只清本阶段的 draft 位（其他阶段原样保留）。"""
    async def scenario(database) -> None:
        store = RunStore(database)
        rid = (await store.create("p-seed", "小说正文……"))["run_id"]
        # 无种子：零值默认（draft={} / instruction=""，消费方无需判 None）
        assert await store.get_retry_seed(rid, "outline") == {"draft": {}, "instruction": ""}

        await store.save_stage_draft(rid, "outline", {"episodes": []})
        assert await store.get_retry_seed(rid, "outline") == {
            "draft": {"episodes": []}, "instruction": "",
        }
        # set_retry_seed 一次 UPDATE 写两位（runner.retry 的落位方式）
        await store.set_retry_seed(rid, "outline", {"episodes": [], "source": "改过的"},
                                   "只补 warnings")
        assert await store.get_retry_seed(rid, "outline") == {
            "draft": {"episodes": [], "source": "改过的"}, "instruction": "只补 warnings",
        }
        # 不同阶段的草稿位互不覆盖；_row 两个新键带出
        await store.save_stage_draft(rid, "script", {"eps": [1]})
        row = await store.get(rid)
        assert row["stage_drafts"] == {"outline": {"episodes": [], "source": "改过的"},
                                       "script": {"eps": [1]}}
        assert row["retry_instruction"] == "只补 warnings"

        await store.clear_retry_seed(rid, "outline")
        assert await store.get_retry_seed(rid, "outline") == {"draft": {}, "instruction": ""}
        row = await store.get(rid)
        assert row["stage_drafts"] == {"script": {"eps": [1]}}  # 只清 outline 位
        assert row["retry_instruction"] == ""                    # 意见随之归零

    _run_scenario(scenario)


def test_runner_failure_persists_draft_and_success_clears(pipeline_test_database, monkeypatch):
    monkeypatch.setattr(skill_executor, "STAGE_SPECS", dict(ALL_STAGE_SPECS_STUB))

    async def scenario(database) -> None:
        store, broker = RunStore(database), EventBroker()
        executor = SeedRecordingExecutor()
        runner = PipelineRunner(store, database, executor=executor, broker=broker)
        run = await store.create("p-seed-runner", "小说正文……")

        # 首轮失败：无种子时注入零值；最终草稿落进 stage_drafts（编辑器始终拿到最新一版）
        await runner.start(run["run_id"])
        await runner.wait_idle()
        state = await store.get(run["run_id"])
        assert state["status"] == "failed" and state["stage"] == "outline"
        assert executor.seen == [{"initial_draft": {}, "user_instruction": ""}]
        assert state["stage_drafts"] == {"outline": {"source": "x"}}

        # 带草稿+意见重试：runner 把种子原样喂给 executor；成功即两位清零
        await runner.retry(run["run_id"], "outline",
                           draft={"source": "y"}, instruction="只补 warnings")
        await runner.wait_idle()
        state = await store.get(run["run_id"])
        assert executor.seen[1] == {"initial_draft": {"source": "y"},
                                    "user_instruction": "只补 warnings"}
        assert state["stage_drafts"] == {} and state["retry_instruction"] == ""
        assert state["status"] == "gated"  # outline 过确认门照旧停车（红线不变）
        saved = await pipeline_repository.get(database, "p-seed-runner", "outline")
        assert saved["content"] == {"source": "y"}  # 产物落库（替身按门过约定写入）

    _run_scenario(scenario)


def test_retry_endpoint_accepts_draft_and_instruction(monkeypatch):
    """retry 请求体扩展 draft/instruction：端点原样透传给 runner（假 runner 捕获
    参数）；draft 非对象是客户端契约错误 → 400，且不触达 runner。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server import pipeline_runs as runs_module

    class CapturingRunner:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def retry(self, run_id, stage, draft=None, instruction=""):
            self.calls.append({"run_id": run_id, "stage": stage,
                               "draft": draft, "instruction": instruction})

    class FakeStore:
        async def get(self, run_id):
            return {"run_id": run_id, "status": "failed", "stage": "outline"}

    runner = CapturingRunner()
    monkeypatch.setattr(runs_module, "_store", FakeStore())
    monkeypatch.setattr(runs_module, "_runner", runner)
    monkeypatch.setattr(runs_module, "_broker", EventBroker())

    # 只挂 runs 路由的最小 app：本测试不碰 DB，也没有全局 lifespan 依赖。
    app = FastAPI()
    app.include_router(runs_module.router)
    with TestClient(app) as client:
        response = client.post("/api/pipeline/runs/run-seed/retry", json={
            "stage": "outline", "draft": {"source": "y"}, "instruction": "只补 warnings",
        })
        assert response.status_code == 200, response.text
        assert runner.calls == [{"run_id": "run-seed", "stage": "outline",
                                 "draft": {"source": "y"},
                                 "instruction": "只补 warnings"}]

        for bad_draft in (["not", "an", "object"], "手滑发了字符串", 42):
            response = client.post("/api/pipeline/runs/run-seed/retry", json={
                "stage": "outline", "draft": bad_draft,
            })
            assert response.status_code == 400, (bad_draft, response.status_code)
        assert len(runner.calls) == 1  # 400 的请求不触达 runner


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


def test_list_by_project_orders_and_filters(pipeline_test_database):
    """fastfollow Task 2：F5 重挂载的数据源——按项目过滤、updated_at 倒序。"""
    async def scenario(database) -> None:
        store = RunStore(database)
        rid_a1 = (await store.create("p-list-a", "正文 A1"))["run_id"]
        await store.create("p-list-b", "正文 B")  # 别的项目：不得串进来
        rid_a2 = (await store.create("p-list-a", "正文 A2"))["run_id"]
        # 同批 INSERT 的 updated_at 可能落在同一微秒；把 a1 显式推到未来，
        # 排序断言才完全确定（a1 变成"最新的"）。
        await database.pool.execute(
            "UPDATE pipeline_runs SET updated_at = extract(epoch from now()) + 30"
            " WHERE run_id = $1",
            rid_a1,
        )

        rows = await store.list_by_project("p-list-a")
        assert [r["run_id"] for r in rows] == [rid_a1, rid_a2]  # 新的在前
        assert all(r["project_id"] == "p-list-a" for r in rows)
        assert await store.list_by_project("p-list-none") == []
        assert len(await store.list_by_project("p-list-a", limit=1)) == 1  # limit 生效

    _run_scenario(scenario)


def test_sweep_interrupted_fails_only_running(pipeline_test_database):
    """fastfollow Task 2：重启清扫——running（进程没了驱动任务）→ failed 可重试；
    gated 是 parked 状态（确认门红线，批准流程还要用）绝不碰；done 同样原状。"""
    async def scenario(database) -> None:
        store = RunStore(database)
        running = await store.create("p-sweep", "正文 running")
        gated = await store.create("p-sweep", "正文 gated")
        done = await store.create("p-sweep", "正文 done")
        await store.set_status(gated["run_id"], "gated")
        await store.set_status(done["run_id"], "done", stage="storyboard")
        # running 行带历史 findings 与阶段账本：清扫只追加 findings，绝不碰账本
        old = [{"gate": "x", "detail": "历史 finding"}]
        await store.set_status(running["run_id"], "failed", stage="outline", findings=old)
        await store.update_stage(running["run_id"], "outline",
                                 {"status": "done", "attempts": 2, "gate_runs": 1, "findings": []})
        # 最后再摆回 running/script（update_stage 会把 stage 列指向所写阶段，故先写账本）：
        # 等价"服务器重启时正在跑 script"的落库形态；findings=None 保留
        await store.set_status(running["run_id"], "running", stage="script")

        assert await store.sweep_interrupted() == 1  # 只有 running 一行被扫

        row = await store.get(running["run_id"])
        assert row["status"] == "failed"
        assert row["findings"] == old + [{"gate": "runtime",
                                          "detail": "服务器重启中断本次运行，可重试"}]
        # 无脏数据红线：stage_status / stage / novel_text 原样
        assert row["stage_status"]["outline"] == {
            "status": "done", "attempts": 2, "gate_runs": 1, "findings": [],
        }
        assert row["stage"] == "script"
        assert row["novel_text"] == "正文 running"
        assert (await store.get(gated["run_id"]))["status"] == "gated"   # 确认门红线
        assert (await store.get(done["run_id"]))["status"] == "done"

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
