"""管线运行 REST/SSE 路由（/api/pipeline/runs，阶段 2 Task 3）。

- POST /runs                                {project_id, novel_text} 建运行并后台推进
- GET  /runs/{run_id}                        运行快照（含 stage_history 派生）
- GET  /runs/{run_id}/events                 SSE 进度直播（text/event-stream）
- POST /runs/{run_id}/approve                确认门放行（仅 gated，不可跳过 → 否则 409）
- POST /runs/{run_id}/retry  {stage}         重置失败阶段重跑（仅 failed → 否则 409）
- POST /runs/{run_id}/feedback {text}        追加人工意见（approve 前带意见重写用）

装配模式照 server/pipeline.py：模块级句柄 + configure() 注入（不反向 import app），
工厂 build_pipeline_runs(db, llm) 供 server/app.py 装配；file 后端（无 DB）句柄为
None，全部路由 503。SSE 端点仿 server/studio.py story_analysis_run 的
StreamingResponse 模式：先订阅再查行（不漏 subscribe 与 get 之间的事件），生成器
finally 反注册队列；done/failed 为终态收流，gate 非终态——approve 后同一连接
继续收后续进度。事件丢失兜底：hello 首帧带运行快照（status/stage），晚连上的
客户端至少能从快照同步状态（broker 是 fire-and-forget，不回放历史）。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.pipeline_run import EventBroker, PipelineRunner, RunStore

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
    runner = PipelineRunner(store, db, broker=broker, llm=llm or _default_llm())
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
