"""管线运行 REST/SSE 路由（/api/pipeline/runs，阶段 2 Task 3）。

- POST /runs                                {project_id, novel_text} 建运行并后台推进
- GET  /runs/{run_id}                        运行快照（含 stage_history 派生）
- GET  /runs/{run_id}/events                 SSE 进度直播（text/event-stream）
- POST /runs/{run_id}/approve                确认门放行（仅 gated，不可跳过 → 否则 409）
- POST /runs/{run_id}/retry  {stage}         重置失败阶段重跑（仅 failed → 否则 409）
- POST /runs/{run_id}/feedback {text}        追加人工意见（确认前「记录意见」，随运行留档，供后续改稿参考）
- GET  /projects/{canvas}/runs               按项目列最近 runs（fastfollow Task 2：F5 重挂载恢复运行卡）

改稿脑（阶段 2 Task 8）：
- GET  /runs/{run_id}/stats                  运行详情聚合（iterations/gate_passes/stages）
- GET  /projects/{canvas}/editor/pending     改稿提案流水（审批卡数据源）
- POST /projects/{canvas}/editor/apply       审批落稿 + script 自动体检 + 门不过回滚
- POST /projects/{canvas}/editor/reject      驳回提案（产物不动）

装配模式照 server/pipeline.py：模块级句柄 + configure() 注入（不反向 import app），
工厂 build_pipeline_runs(db, llm) 供 server/app.py 装配；file 后端（无 DB）句柄为
None，全部路由 503。SSE 端点仿 server/studio.py story_analysis_run 的
StreamingResponse 模式：先订阅再查行（不漏 subscribe 与 get 之间的事件），生成器
finally 反注册队列；收流口径：hello 对订阅时已是 done/cancelled 的运行即收流
（终态、不会再有事件），failed 保持打开等 retry 事件——运行中则以 done/failed
事件为终态收流，gate 非终态，approve 后同一连接继续收后续进度。事件丢失兜底：
hello 首帧带运行快照（status/stage），晚连上的
客户端至少能从快照同步状态（broker 是 fire-and-forget，不回放历史）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from core import pipeline_editor
from core.pipeline_run import EventBroker, PipelineRunner, RunStore, STAGE_ORDER

router = APIRouter(prefix="/api/pipeline", tags=["pipeline-runs"])

# 由 server/app.py 装配阶段注入；测试可 monkeypatch 这三个句柄。
_store: RunStore | None = None
_runner: PipelineRunner | None = None
_broker: EventBroker | None = None

NO_DB_MESSAGE = "pipeline runs 需要 Postgres（compose.postgres.yml）"


def configure(*, store: RunStore, runner: PipelineRunner, broker: EventBroker) -> None:
    global _store, _runner, _broker
    _store, _runner, _broker = store, runner, broker


def build_pipeline_runs(db: Any, llm: Any = None) -> tuple[RunStore, EventBroker, PipelineRunner]:
    """lifespan/装配用工厂：真链路 executor = skill_executor.run_stage（Task 2）。"""
    store = RunStore(db)
    broker = EventBroker()
    # 装配级预算调优：真模型冒烟（skill_executor.__main__，--max-iterations 默认 20）
    # 默认 5 几乎必耗尽；16 轮曾刚好差一口气；2026-10-25 实测《最后一单》outline
    # 真跑 18 轮过门（gate_runs=7）——按证据放宽到与冒烟 CLI 一致的 20。
    # 只在此装配点放宽；run_stage/PipelineRunner 的保守默认 5 按台账裁定保持不动。
    runner = PipelineRunner(store, db, broker=broker, llm=llm or _default_llm(),
                            max_iterations=20)
    return store, broker, runner


def _default_llm() -> Any:
    """真链路 LLMPort：Task 2 的 _RouterLLM（core/llm.LLM 响应整形）。延迟导入。"""
    from core.skill_executor import _RouterLLM
    return _RouterLLM()


async def shutdown() -> None:
    """lifespan 收尾：取消推进任务、清空订阅（未装配时是 no-op）。"""
    if _runner is not None:
        await _runner.aclose()
    if _broker is not None:
        _broker.close()


async def sweep_interrupted_runs() -> int:
    """lifespan 启动清扫（fastfollow Task 2）：服务器重启后 running 的 run 已
    没有驱动任务，统一转 failed 让用户可重试（gated parked 不碰）。未装配 DB
    （file 后端）时 no-op 返回 0。清扫失败只记告警不拦启动——它欠的是「下次
    重启再扫一遍」，漏扫的行下次启动还在 running，仍会被同一语句收走。"""
    if _store is None:
        return 0
    try:
        return await _store.sweep_interrupted()
    except Exception as exc:  # noqa: BLE001  池未就绪/库暂时连不上：启动优先
        logging.getLogger("toonflow.pipeline_runs").warning(
            "启动清扫 interrupted runs 失败（下次重启会再试）：%s: %s",
            type(exc).__name__, exc,
        )
        return 0


async def _deps() -> tuple[RunStore, PipelineRunner, EventBroker]:
    if _store is None or _runner is None or _broker is None:
        raise HTTPException(status_code=503, detail=NO_DB_MESSAGE)
    # 复用 artifacts 路由的惰性建池锁：正常路径 lifespan 已起池，这里是兜底。
    from server import pipeline as pipeline_api
    try:
        await pipeline_api.ensure_started()
    except Exception as exc:  # noqa: BLE001  Postgres 连不上按「无 DB 可用」降级
        raise HTTPException(status_code=503, detail=NO_DB_MESSAGE) from exc
    return _store, _runner, _broker


class CreateRunBody(BaseModel):
    project_id: str
    novel_text: str


class RetryBody(BaseModel):
    stage: str


class FeedbackBody(BaseModel):
    text: str


@router.post("/runs")
async def create_run(body: CreateRunBody) -> dict:
    if not body.project_id.strip():
        raise HTTPException(status_code=400, detail="project_id 不能为空")
    if not body.novel_text.strip():
        raise HTTPException(status_code=400, detail="小说正文不能为空")
    store, runner, _broker = await _deps()
    run = await store.create(body.project_id, body.novel_text)
    await runner.start(run["run_id"])
    return {"run_id": run["run_id"], "status": run["status"], "stage": run["stage"]}


@router.get("/runs/{run_id}")
async def read_run(run_id: str) -> dict:
    store, _runner, _broker = await _deps()
    run = await store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行不存在：{run_id}")
    run["history"] = await store.stage_history(run_id)
    return run


@router.get("/projects/{canvas}/runs")
async def list_runs(canvas: str) -> dict:
    """按项目列最近 runs（updated_at 倒序，limit 10；fastfollow Task 2）。

    F5 重挂载的恢复数据源：前端只需 run_id/status/stage/updated_at 四个轻量
    字段找回运行卡，novel_text/stage_status 等重字段走单条 GET，不进列表。
    """
    store, _runner, _broker = await _deps()
    runs = await store.list_by_project(canvas)
    return {"runs": [{"run_id": r["run_id"], "status": r["status"],
                      "stage": r["stage"], "updated_at": r["updated_at"]}
                     for r in runs]}


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str) -> StreamingResponse:
    store, _runner, broker = await _deps()
    queue = broker.subscribe(run_id)  # 先订阅再查行：subscribe 与 get 之间的事件不丢
    try:
        run = await store.get(run_id)
    except Exception:
        broker.unsubscribe(run_id, queue)
        raise
    if run is None:
        broker.unsubscribe(run_id, queue)
        raise HTTPException(status_code=404, detail=f"运行不存在：{run_id}")

    async def gen():
        try:
            # hello 首帧 = 快照（broker 不回放历史，晚连上的客户端从这里同步状态）
            yield _sse({"type": "hello", "run_id": run_id,
                        "status": run["status"], "stage": run["stage"]})
            if run["status"] in ("done", "cancelled"):
                # 订阅时已是终态：后续不会再有任何事件，不收流的话连接与其
                # broker 队列会一直泄漏到客户端断开（Task 4 刷新已完成运行必踩）。
                # failed 保持打开——retry 之后还有新事件可等。
                return
            while True:
                event = await queue.get()
                yield _sse(event)
                if event.get("type") in ("done", "failed"):
                    break  # 终态收流；gate 非终态，approve 后同一连接继续收进度
        finally:
            # 客户端断开由 StreamingResponse 取消生成器，走 finally 反注册
            broker.unsubscribe(run_id, queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/approve")
async def approve_run(run_id: str) -> dict:
    store, runner, _broker = await _deps()
    run = await store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行不存在：{run_id}")
    approved = await store.approve(run_id)
    if not approved:
        # 红线：确认门不可跳过——只有 gated 能放行
        raise HTTPException(
            status_code=409,
            detail=f"只有 gated 状态可以确认（当前 {run['status']}），确认门不可跳过",
        )
    await runner.resume(run_id)
    return {"ok": True, "run_id": run_id, "status": "running"}


@router.post("/runs/{run_id}/retry")
async def retry_run(run_id: str, body: RetryBody) -> dict:
    store, runner, _broker = await _deps()
    if await store.get(run_id) is None:
        raise HTTPException(status_code=404, detail=f"运行不存在：{run_id}")
    try:
        await runner.retry(run_id, body.stage)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "run_id": run_id, "stage": body.stage, "status": "running"}


@router.post("/runs/{run_id}/feedback")
async def post_feedback(run_id: str, body: FeedbackBody) -> dict:
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="意见内容不能为空")
    store, _runner, _broker = await _deps()
    if await store.get(run_id) is None:
        raise HTTPException(status_code=404, detail=f"运行不存在：{run_id}")
    await store.append_feedback(run_id, text)
    return {"ok": True, "run_id": run_id}


# ---------------------------------------------------------------------------
# 运行详情（阶段 2 Task 8）：按权威账本聚合的运行统计
#
# 计数口径与 runner 的 stage_status bookkeeping 同源（Task 2 review 定案）：
# attempts = StageResult.iterations、gate_runs = len(StageResult.gate_log)，
# 一律读 pipeline_runs.stage_status 这份唯一持久账本，绝不从 SSE gate 行里数
# （broker 的 gate 事件混着自检门与 read_gate_report 重跑，不作数）。
# gate_passes = 权威门通过并落库的阶段数（stage_status.status == "done"）——
# stage_status 里没有逐次门事件的通过位，"阶段 done" 是唯一可信的通过事实。
# ---------------------------------------------------------------------------

def aggregate_run_stats(run: dict) -> dict:
    """从一行运行记录聚合统计（纯函数；测试直接喂 store.get 的结果）。"""
    stages: dict[str, dict] = {}
    for stage in STAGE_ORDER:
        entry = run["stage_status"].get(stage)
        if not entry:
            continue
        stages[stage] = {
            "attempts": int(entry.get("attempts") or 0),
            "gate_runs": int(entry.get("gate_runs") or 0),
            "ok": entry.get("status") == "done",
        }
    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "stage": run["stage"],
        "iterations": sum(s["attempts"] for s in stages.values()),
        "gate_passes": sum(1 for s in stages.values() if s["ok"]),
        "stages": stages,
    }


@router.get("/runs/{run_id}/stats")
async def run_stats(run_id: str) -> dict:
    store, _runner, _broker = await _deps()
    run = await store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行不存在：{run_id}")
    return aggregate_run_stats(run)


# ---------------------------------------------------------------------------
# 改稿脑审批端点（阶段 2 Task 8）：提案 → 审批 → 自动体检
# 落稿唯一入口是 apply（先落库、script 再重跑真门、门不过回滚）——
# agent 工具环里没有 apply，模型永远写不了产物。
# ---------------------------------------------------------------------------

class ApplyEditBody(BaseModel):
    edit_id: str


class RejectEditBody(BaseModel):
    edit_id: str


@router.get("/projects/{canvas}/editor/pending")
async def list_editor_pending(canvas: str) -> dict:
    await _deps()
    return {"edits": pipeline_editor.list_edits(canvas)}


@router.post("/projects/{canvas}/editor/apply")
async def apply_editor_edit(canvas: str, body: ApplyEditBody):
    store, _runner, _broker = await _deps()
    try:
        result = await pipeline_editor.apply_pending_edit(store.db, canvas, body.edit_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not result.get("ok") and result.get("message"):
        # 结构非法（import 拒收、未落库）走管线 422 契约 {message, findings}；
        # 门不过已回滚的结果是"体检如实执行"的 200（ok=false + rolled_back=true），
        # 前端据此渲染「已回滚」而不是把它当请求错误。
        return JSONResponse(status_code=422, content=result)
    return result


@router.post("/projects/{canvas}/editor/reject")
async def reject_editor_edit(canvas: str, body: RejectEditBody) -> dict:
    await _deps()
    try:
        edit = pipeline_editor.reject(canvas, body.edit_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "edit_id": edit["edit_id"], "status": edit["status"]}
