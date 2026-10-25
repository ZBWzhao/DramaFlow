"""管线运行状态机（阶段 2 Task 3）：阶段推进 + 确认门 + 断点续跑 + 进度广播。

三件套：
- RunStore：pipeline_runs 表（migrations/0006）的 asyncpg SQL 直写。stage_status
  JSONB 是唯一持久阶段账本，SQL 侧 jsonb || 合并（同名阶段整条替换），不做读改写。
- EventBroker：进程内、按 run_id 分桶的多订阅者扇播（SSE 每连接一个队列）。
- PipelineRunner：asyncio 后台任务逐阶段驱动 Task 2 的 run_stage；outline 过门后
  停在人工确认门（红线：门不可跳过，gated 状态下一律不推进），approve 后从断点
  续跑（已 done 的阶段不重跑）。

计数口径（Task 2 review 遗留）：stage_status 里的 attempts / gate_runs 只认
StageResult.iterations / StageResult.gate_log；broker 里透传的 gate 事件混着
自检门与 read_gate_report 重跑，一律不作数。

无脏数据：run_stage 门过才经 pipeline_repository.save 落产物，失败/异常路径
runner 只写 pipeline_runs 自己这一行，上游产物原样不动。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Callable

from . import pipeline_repository, skill_executor
from .postgres import PostgresDatabase

# 阶段推进顺序与确认门位置（全篇口径：确认门在 outline 之后，不可跳过）。
STAGE_ORDER = ["outline", "script", "characters", "art", "staging", "storyboard"]
GATE_STAGE = "outline"

# 与 0006 的 CHECK 约束一致；提前校验给出可读报错，不撞数据库。
RUN_STATUSES = ("running", "gated", "failed", "done", "cancelled")


def _decode_jsonb(raw: Any) -> Any:
    """JSONB 容错解码：asyncpg 默认把 jsonb 读成 str；已设 codec 时是原生对象。"""
    if isinstance(raw, (dict, list)) or raw is None:
        return raw
    return json.loads(raw)


# ---------------------------------------------------------------------------
# RunStore：pipeline_runs 表 SQL 直写
# ---------------------------------------------------------------------------

class RunStore:
    """pipeline_runs 的 asyncpg 读写；db 取 PostgresDatabase（db.client() = 池）。"""

    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    def _client(self) -> Any:
        return self.db.client()

    @classmethod
    def _row(cls, row: Any) -> dict:
        return {
            "run_id": str(row["run_id"]),
            "project_id": str(row["project_id"]),
            "novel_text": str(row["novel_text"]),
            "status": str(row["status"]),
            "stage": str(row["stage"]),
            "stage_status": _decode_jsonb(row["stage_status"]) or {},
            "feedback": str(row["feedback"] or ""),
            "findings": _decode_jsonb(row["findings"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    async def create(self, project_id: str, novel_text: str, run_id: str = "") -> dict:
        """新建一行（status=running / stage=outline / stage_status={}，列默认值）。"""
        row = await self._client().fetchrow(
            "INSERT INTO pipeline_runs(run_id, project_id, novel_text) VALUES($1, $2, $3)"
            " RETURNING *",
            run_id or uuid.uuid4().hex, project_id, novel_text,
        )
        return self._row(row)

    async def get(self, run_id: str) -> dict | None:
        row = await self._client().fetchrow(
            "SELECT * FROM pipeline_runs WHERE run_id = $1", run_id
        )
        return self._row(row) if row is not None else None

    async def update_stage(self, run_id: str, stage: str, patch: dict) -> None:
        """合并一条阶段状态进 stage_status 并把 stage 列指向该阶段。

        jsonb || 是顶层合并：patch 形如 {stage: {...}}，同名阶段条目整条替换，
        其他阶段条目原样保留——并发写不同阶段互不覆盖。updated_at 由 SQL 侧刷新。
        """
        await self._client().execute(
            "UPDATE pipeline_runs SET stage = $2,"
            " stage_status = COALESCE(stage_status, '{}'::jsonb) || $3::jsonb,"
            " updated_at = extract(epoch from now())"
            " WHERE run_id = $1",
            run_id, stage, json.dumps({stage: patch}, ensure_ascii=False, default=str),
        )

    async def set_status(
        self, run_id: str, status: str, *, stage: str | None = None, findings: list | None = None
    ) -> None:
        """run 级状态迁移；stage / findings 可选覆盖，findings=None 表示保留原值
        （gated/done 不动历史 findings，failed 必带 findings，retry 时传 [] 清空）。"""
        if status not in RUN_STATUSES:
            raise ValueError(f"非法运行状态：{status}（合法值：{'、'.join(RUN_STATUSES)}）")
        sets = ["status = $2", "updated_at = extract(epoch from now())"]
        args: list[Any] = [run_id, status]
        if stage is not None:
            args.append(stage)
            sets.append(f"stage = ${len(args)}")
        if findings is not None:
            args.append(json.dumps(findings, ensure_ascii=False, default=str))
            sets.append(f"findings = ${len(args)}::jsonb")
        await self._client().execute(
            f"UPDATE pipeline_runs SET {', '.join(sets)} WHERE run_id = $1", *args
        )

    async def approve(self, run_id: str) -> bool:
        """确认门放行：仅 gated → running（原子守护；门未到/重复确认返回 False）。"""
        updated = await self._client().fetchval(
            "UPDATE pipeline_runs SET status = 'running', updated_at = extract(epoch from now())"
            " WHERE run_id = $1 AND status = 'gated' RETURNING 1",
            run_id,
        )
        return updated is not None

    async def append_feedback(self, run_id: str, text: str) -> None:
        """追加一条人工意见（换行连接）；approve 前的「带意见重写」走这里。"""
        await self._client().execute(
            "UPDATE pipeline_runs SET"
            " feedback = CASE WHEN feedback = '' THEN $2 ELSE feedback || chr(10) || $2 END,"
            " updated_at = extract(epoch from now())"
            " WHERE run_id = $1",
            run_id, text,
        )

    async def stage_history(self, run_id: str) -> list[dict]:
        """从 stage_status JSONB 派生阶段历史（STAGE_ORDER 序，未跑的阶段不出现）。"""
        run = await self.get(run_id)
        if run is None:
            return []
        stage_status = run["stage_status"]
        return [{**stage_status[stage], "stage": stage}
                for stage in STAGE_ORDER if stage in stage_status]


# ---------------------------------------------------------------------------
# EventBroker：进程内 run 级扇播
# ---------------------------------------------------------------------------

class EventBroker:
    """进程内、按 run_id 分桶的事件扇播（SSE 每个连接 subscribe 一个队列）。

    没有委托 core/events.py 的 EventBus（实现前已读，二者不等价，注释留档）：
    EventBus 是会话维度（session_id、AgentEvent 形状、可选 EventStore 持久化
    分发任务），管线运行需要的是 run_id 维度、plain dict、fire-and-forget 的
    多订阅者扇播——硬套会把会话/持久化语义带进来。队列默认无界：publish 永不
    阻塞、从不抛错（进度广播不配炸掉推进任务），连接生命周期由 SSE 生成器
    finally 里的 unsubscribe 负责，不残留队列。
    """

    def __init__(self, maxsize: int = 0) -> None:
        self._maxsize = maxsize
        self._subs: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subs.setdefault(run_id, []).append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        queues = self._subs.get(run_id)
        if not queues:
            return
        try:
            queues.remove(queue)
        except ValueError:
            pass
        if not queues:
            self._subs.pop(run_id, None)

    def publish(self, run_id: str, event: dict) -> None:
        """扇播给该 run 的全部订阅者；无订阅者是 no-op，队列满丢最老保最新。"""
        for queue in list(self._subs.get(run_id, ())):
            if queue.maxsize > 0 and queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    def close(self) -> None:
        self._subs.clear()


# ---------------------------------------------------------------------------
# PipelineRunner：后台推进状态机
# ---------------------------------------------------------------------------

class PipelineRunner:
    """逐阶段驱动 run_stage 的后台状态机；executor 可注入（测试替身）。"""

    def __init__(
        self,
        store: RunStore,
        db: PostgresDatabase,
        *,
        broker: EventBroker,
        executor: Callable | None = None,
        llm: Any = None,
        max_iterations: int = 5,
    ) -> None:
        self.store = store
        self.db = db
        self.broker = broker
        # 缺省接 Task 2 的真执行器；测试注入 fake（__call__(db, project, stage, inputs, **kw)）。
        self.executor = executor or skill_executor.run_stage
        self.llm = llm
        self.max_iterations = max_iterations
        self._tasks: dict[str, asyncio.Task] = {}

    async def start(self, run_id: str) -> None:
        """后台推进一个 run（create_task；句柄存 _tasks 供 wait_idle/aclose）。"""
        existing = self._tasks.get(run_id)
        if existing is not None and not existing.done():
            return  # 同一 run 已有推进任务（approve/retry 与手动 start 并发），不叠加
        self._tasks[run_id] = asyncio.create_task(
            self._drive(run_id), name=f"pipeline-run:{run_id}"
        )

    async def resume(self, run_id: str) -> None:
        """approve 后从断点续跑（_drive 跳过已 done 的阶段，不重跑确认过的大纲）。"""
        # gated 状态的前一轮推进只剩收尾（publish + 出清 _tasks），等它退出再重启，
        # 否则 start 的去重护栏会把这次 resume 当成"已有推进中"而静默跳过。
        await self._await_previous(run_id)
        await self.start(run_id)

    async def retry(self, run_id: str, stage: str) -> None:
        """重置失败阶段并重跑：置 pending/attempts=0、清 run 级 findings 后 start。"""
        run = await self.store.get(run_id)
        if run is None:
            raise KeyError(f"运行不存在：{run_id}")
        if stage not in STAGE_ORDER:
            raise ValueError(f"未知阶段：{stage}")
        if run["status"] != "failed":
            raise RuntimeError(f"只有 failed 状态可以重试（当前 {run['status']}）")
        if stage != run["stage"]:
            raise RuntimeError(f"只能重试失败阶段 {run['stage']}")
        await self._await_previous(run_id)  # 上一轮已 failed 但可能还在收尾广播
        await self.store.update_stage(
            run_id, stage, {"status": "pending", "attempts": 0, "gate_runs": 0, "findings": []}
        )
        await self.store.set_status(run_id, "running", stage=stage, findings=[])
        self.broker.publish(run_id, {"type": "retry", "stage": stage})
        await self.start(run_id)

    async def _await_previous(self, run_id: str) -> None:
        """等上一轮推进任务完全退出（只用于失败/确认门后的显式重启入口；
        start 的去重护栏照旧，不等待——长驱动中的二次 start 立即返回）。"""
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            try:
                await task
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001  上一轮的异常已在 _drive 外壳落库
                pass

    async def wait_idle(self) -> None:
        """等当前所有推进任务结束（测试同步点；任务异常不外抛——状态已落库）。"""
        tasks = list(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self) -> None:
        """服务收尾：取消全部推进任务（lifespan shutdown 调用）。"""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- 内部 -------------------------------------------------------------

    async def _drive(self, run_id: str) -> None:
        """推进任务外壳：任何基建级意外（store/broker/未捕获异常）都落成 failed
        运行 + 事件，绝不静默挂死（Task 2 review：不得假设执行器必返 StageResult）。"""
        try:
            await self._drive_inner(run_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            try:
                await self.store.set_status(
                    run_id, "failed",
                    findings=[{"gate": "runner", "detail": f"{type(exc).__name__}: {exc}"}],
                )
            except Exception:  # noqa: BLE001  存储也炸了：只剩广播可用
                pass
            self.broker.publish(
                run_id, {"type": "failed", "stage": "", "error": f"{type(exc).__name__}: {exc}"}
            )
        finally:
            if self._tasks.get(run_id) is asyncio.current_task():
                self._tasks.pop(run_id, None)

    async def _drive_inner(self, run_id: str) -> None:
        run = await self.store.get(run_id)
        if run is None:
            return
        # gated 红线：确认门前不推进；cancelled/done/failed 同样只经显式入口进入。
        if run["status"] != "running":
            return
        try:
            start_index = STAGE_ORDER.index(run["stage"])
        except ValueError:
            start_index = 0
        for stage in STAGE_ORDER[start_index:]:
            # Task 5/6 才补齐其余阶段规格：STAGE_SPECS 没有的阶段不可运行，跳过。
            # 因此当前只有 outline 可跑，outline 过门（approve 后）即无剩余可运行
            # 阶段 → done。Task 5/6 落地规格后这里自然收紧为全六阶段推进，无需改机。
            if stage not in skill_executor.STAGE_SPECS:
                continue
            # 断点续跑：已 done 的阶段不重跑（approve → resume 不会重复确认过的大纲）。
            if (run["stage_status"].get(stage) or {}).get("status") == "done":
                continue
            await self.store.update_stage(
                run_id, stage,
                {"status": "running", "attempts": 0, "gate_runs": 0, "findings": []},
            )
            self.broker.publish(run_id, {"type": "stage", "stage": stage, "status": "running"})
            inputs = {"novel_text": run["novel_text"], **await self._upstream(run, stage)}

            def _on_event(event: dict, _stage: str = stage) -> None:
                self.broker.publish(run_id, self._relay_event(_stage, event))

            try:
                result = await self.executor(
                    self.db, run["project_id"], stage, inputs,
                    llm=self.llm, max_iterations=self.max_iterations, on_event=_on_event,
                )
            except Exception as exc:  # noqa: BLE001
                # 基建异常（llm.complete / 门 spawn 等）不是 StageResult：落 failed +
                # findings + 事件；产物侧由 run_stage 约定保证「门过才落库」，无脏数据。
                findings = [{"gate": "infrastructure", "detail": f"{type(exc).__name__}: {exc}"}]
                await self.store.update_stage(run_id, stage, {"status": "failed", "findings": findings})
                await self.store.set_status(run_id, "failed", stage=stage, findings=findings)
                self.broker.publish(
                    run_id, {"type": "failed", "stage": stage, "findings": findings}
                )
                return
            await self.store.update_stage(run_id, stage, {
                "status": "done" if result.ok else "failed",
                "attempts": result.iterations,
                "gate_runs": len(result.gate_log),   # 门执行次数（含自检/重跑），非通过数
                "findings": list(result.findings),
            })
            if not result.ok:
                await self.store.set_status(
                    run_id, "failed", stage=stage, findings=list(result.findings)
                )
                self.broker.publish(
                    run_id, {"type": "failed", "stage": stage, "findings": list(result.findings)}
                )
                return
            if stage == GATE_STAGE:
                await self.store.set_status(run_id, "gated", stage=stage)
                self.broker.publish(run_id, {"type": "gate", "stage": stage})
                return  # 确认门前不推进（红线）：approve + resume 才继续
        # 循环走完 = 没有剩余可运行阶段 → done（stage 指向管线末阶段）。
        await self.store.set_status(run_id, "done", stage=STAGE_ORDER[-1])
        self.broker.publish(run_id, {"type": "done", "stage": STAGE_ORDER[-1]})

    async def _upstream(self, run: dict, stage: str) -> dict:
        """按 spec.gate_needs 把已落库上游产物塞进 inputs（Task 5 约定：
        inputs[need] = 该产物 content；执行器侧 gate_needs 驱动落盘供门使用）。"""
        needs = skill_executor.STAGE_SPECS[stage].get("gate_needs") or []
        inputs: dict = {}
        for need in needs:
            row = await pipeline_repository.get(self.db, run["project_id"], need)
            if row is not None:
                inputs[need] = row["content"]
        return inputs

    @staticmethod
    def _relay_event(stage: str, event: dict) -> dict:
        """执行器内部事件 → SSE 事件：补 stage 标签；gate/done 重打标。

        run 级 "gate"/"done" 是 Task 4 前端契约里的「确认门 / 整体完成」语义，
        阶段级同名事件让位为 stage_gate / stage_done，避免前端把每次权威门误读成
        进入确认门。
        """
        tagged = dict(event)
        kind = tagged.get("type")
        if kind in ("gate", "done"):
            tagged["type"] = f"stage_{kind}"
        tagged["stage"] = stage
        return tagged
