"""FastAPI 服务端：NDJSON 事件流 + 会话管理 + 工具管理。

对照 Toonflow：apps/server/src/routes/agent.ts + runtime/index.ts
  · POST /api/agent        → 一次请求，响应是 application/x-ndjson（一行一个事件）
  · 客户端断开             → 任务继续，按 last_event_id 重连补发
  · POST /api/agent/cancel → 用户点停止，取消一路传到子 Agent
  · GET  /api/tools        → 已装载工具清单（元数据静态解析的结果）
  · POST /api/tools/reload → 热装载（改工具代码不用重启进程）

为什么用 NDJSON 而不是 SSE：和 Toonflow 一样的取舍 —— 一行一个 JSON，
不需要约定 event/data 格式，前端 ReadableStream 按 \n 切就行，调试时 curl 直接可读。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
WEB_DIR = ROOT / "web"
WORKSPACE = ROOT / "workspace"

from core.config import Settings  # noqa: E402
from core.agent import AgentAborted, build_system_prompt  # noqa: E402
from core.llm import LLM  # noqa: E402
from core.loader import list_tools  # noqa: E402
from core.canvas import NODE_SPEC, Canvas, CanvasError  # noqa: E402
from core.canvas_repository import CanvasConflictError  # noqa: E402
from core.media import default_registry  # noqa: E402
from core.composition import (  # noqa: E402
    CompositionError,
    CompositionService,
    ffmpeg_capabilities,
    sha256_file,
    write_srt,
)
from core.media_asset_repository import MediaAssetRecord  # noqa: E402
from core.tts import TTSRegistry  # noqa: E402
from core.media_job_store import MediaJob, MediaJobUncertain, MediaJobWorker, create_media_job_store  # noqa: E402
from core.invocation_store import InvocationState, canonical_hash, canonical_json, new_approval  # noqa: E402
from core.runtime_store import event_id_after, validate_event_id  # noqa: E402
from core.skills import read_skill_file, skills_prompt_section  # noqa: E402
from core.team import TEAMS_ROOT, TeamRunner, load_team  # noqa: E402
from core.production import ProductionRunner  # noqa: E402
from core.production_repository import ProductionRunConflict  # noqa: E402
from server.registry import SessionRegistry  # noqa: E402

registry = SessionRegistry(llm=LLM(Settings.load()), workspace=str(WORKSPACE))
media_jobs = create_media_job_store()
media_worker: MediaJobWorker | None = None
composition_service = CompositionService(WORKSPACE)
tts_registry = TTSRegistry(WORKSPACE)


async def _register_media_asset(
    *, canvas: str, node_id: str, media_type: str, path: str, content_type: str,
    provider: str = "", generation_id: str = "", operation_id: str = "",
) -> None:
    target = (WORKSPACE / str(path).replace("\\", "/").lstrip("/")).resolve()
    try:
        target.relative_to(WORKSPACE.resolve())
    except ValueError as exc:
        raise ValueError(f"媒体资产路径越界：{path}") from exc
    await registry.media_assets.register(
        MediaAssetRecord.create(
            canvas=canvas,
            node_id=node_id,
            media_type=media_type,
            storage_key=path,
            content_type=content_type,
            file_size=target.stat().st_size if target.is_file() else 0,
            sha256=sha256_file(target) if target.is_file() else "",
            provider=provider,
            generation_id=generation_id,
            remote_operation_id=operation_id,
        )
    )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global media_worker
    await registry.ainit()  # 启动时装载工具插件
    await media_jobs.start()
    if os.environ.get("MEDIA_WORKER_EMBEDDED", "true").strip().lower() not in {"0", "false", "no"}:
        media_worker = MediaJobWorker(
            media_jobs,
            _execute_media_job,
            lease_seconds=int(os.environ.get("MEDIA_JOB_LEASE_SECONDS", "30")),
        )
        await media_worker.start()
    try:
        yield
    finally:
        if media_worker is not None:
            await media_worker.close()
            media_worker = None
        await media_jobs.close()
        await registry.aclose()


app = FastAPI(title="toonflow-py-lab", lifespan=lifespan)


@app.exception_handler(CanvasConflictError)
async def canvas_conflict_handler(_: Request, exc: CanvasConflictError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": str(exc),
            "code": "canvas_revision_conflict",
            "canvas": exc.name,
            "expected_revision": exc.expected,
            "actual_revision": exc.actual,
        },
    )


class ChatRequest(BaseModel):
    prompt: str
    session_id: str | None = None
    canvas: str = "main"
    use_sub_agent: bool = True
    media_policy: Literal["plan_only", "ask", "auto_review"] = "ask"


class CancelRequest(BaseModel):
    session_id: str


class FollowupRequest(BaseModel):
    session_id: str
    text: str


class QuestionAnswerRequest(BaseModel):
    answer: str


class CreateSessionRequest(BaseModel):
    canvas: str = "main"
    title: str = ""


class BindSessionRequest(BaseModel):
    canvas: str = "main"


class RenameSessionRequest(BaseModel):
    title: str


class MessageActionRequest(BaseModel):
    action: Literal["delete", "resend", "branch"]


class ReloadRequest(BaseModel):
    name: str


class ExportCanvasRequest(BaseModel):
    canvas: str = "main"
    path: str = ""


class RunCanvasRequest(BaseModel):
    canvas: str = "main"
    only: list[str] = []
    approved: bool = False
    provider: str = "mock"
    model: str = ""
    duration: int = 5
    resolution: str = "720p"
    ratio: str = "16:9"
    generate_audio: bool = False
    approval_id: str = ""
    revision: int | None = None
    production_run_id: str | None = None


class ComposeCanvasRequest(BaseModel):
    canvas: str = "main"
    only: list[str] = []
    approved: bool = False
    approval_id: str = ""
    revision: int | None = None
    production_run_id: str | None = None


class MoveNodeRequest(BaseModel):
    canvas: str = "main"
    x: float
    y: float
    revision: int | None = None


class UpdateCanvasNodeRequest(BaseModel):
    canvas: str = "main"
    label: str | None = None
    status: Literal["draft", "ready", "generating", "done", "error"] | None = None
    data: dict[str, Any] | None = None
    revision: int | None = None


class CreateCanvasNodeRequest(BaseModel):
    canvas: str = "main"
    type: Literal["text", "storyboard", "asset", "prompt", "image_gen", "video_gen", "audio", "tts_gen", "timeline"]
    label: str
    x: float
    y: float
    data: dict[str, Any] = {}
    revision: int | None = None


class ConnectRequest(BaseModel):
    canvas: str = "main"
    source: str
    source_handle: str
    target: str
    target_handle: str
    revision: int | None = None


class DeleteEdgeRequest(BaseModel):
    canvas: str = "main"
    edge_id: str
    revision: int | None = None


class TeamRequest(BaseModel):
    task: str
    team: str = "storyboardTeam"
    session_id: str | None = None
    canvas: str = "main"
    production_run_id: str | None = None


def _line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _require_revision(canvas: Canvas, expected: int | None) -> None:
    if expected is not None and canvas.revision != expected:
        raise CanvasConflictError(canvas.name, expected, canvas.revision)


async def _update_production_stage(
    production_run_id: str | None,
    canvas_name: str,
    stage: str,
    stage_status: str,
    run_status: str,
    *,
    problems: list[str] | None = None,
    artifact_ids: list[str] | None = None,
) -> dict[str, Any] | None:
    """CAS-update a production checkpoint from approval and Worker paths."""
    if not production_run_id:
        return None
    for _ in range(4):
        run = await registry.production_runs.get(production_run_id)
        if run is None:
            raise ValueError("生产任务不存在")
        if run.get("canvas_name") != canvas_name:
            raise ValueError("生产任务与当前画布不匹配")
        current = dict((run.get("stages") or {}).get(stage) or {})
        current.update(
            {
                "stage": stage,
                "status": stage_status,
                "problems": list(problems or []),
                "artifact_ids": list(artifact_ids or current.get("artifact_ids") or []),
                "updated_at": time.time(),
            }
        )
        if stage_status in {"succeeded", "failed"}:
            current["finished_at"] = time.time()
        run["stages"] = {**(run.get("stages") or {}), stage: current}
        run["current_stage"] = stage
        run["status"] = run_status
        try:
            return await registry.production_runs.save(run, int(run.get("revision") or 0))
        except ProductionRunConflict:
            continue
    raise RuntimeError("生产任务状态并发更新冲突，请刷新后重试")


# ---------------------------------------------------------------------------
# 核心：一次 Agent 请求 → NDJSON 流
# ---------------------------------------------------------------------------
async def _stream_run(
    live: Any,
    queue: asyncio.Queue,
    task: asyncio.Task,
    terminal_types: set[str],
) -> AsyncIterator[bytes]:
    """把运行事件送给一个浏览器连接；连接关闭不再等价于取消任务。"""
    started = time.monotonic()
    last_event = started
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                if task.done() and queue.empty():
                    break
                if time.monotonic() - last_event >= 5:
                    last_event = time.monotonic()
                    yield _line(
                        {
                            "type": "ping",
                            "session_id": live.id,
                            "run_id": live.current_run_id,
                            "elapsed": round(time.monotonic() - started, 1),
                            "dropped": live.bus.dropped,
                        }
                    )
                continue
            last_event = time.monotonic()
            yield _line(event.to_dict())
            if event.type in terminal_types:
                break
    finally:
        # 网络抖动只移除这个订阅；真正的停止只允许走 /api/agent/cancel。
        live.bus.unsubscribe(queue)


async def _watch_shared_cancel(live: Any, run_id: str, owner: asyncio.Task) -> None:
    """轮询共享取消位；请求落到别的实例时也能打断真正执行任务的实例。"""
    last_heartbeat = 0.0
    while not owner.done():
        await asyncio.sleep(0.35)
        if time.monotonic() - last_heartbeat >= 5:
            await registry.run_states.heartbeat(run_id)
            last_heartbeat = time.monotonic()
        if await registry.run_states.is_cancelled(run_id):
            live.cancel.set()
            owner.cancel()
            return


async def _execute_agent_run(live: Any, req: ChatRequest, run_id: str) -> None:
    owner = asyncio.current_task()
    assert owner is not None
    watcher = asyncio.create_task(_watch_shared_cancel(live, run_id, owner))
    live.agent.external_followup_loader = lambda: registry.run_states.drain_followups(run_id)
    status, error = "running", None
    try:
        await live.bus.publish("session", session_id=live.id, runs=live.runs, run_id=run_id)
        await live.agent.run(req.prompt)
        status = live.agent.last_status
        if status == "error":
            error = live.agent.last_finish_reason or "agent_error"
        await live.bus.flush()
    except (AgentAborted, asyncio.CancelledError):
        watcher.cancel()
        status = "aborted"
        live.cancel.set()
        await live.bus.publish("aborted", session_id=live.id, reason="user_cancelled")
    except Exception as exc:  # noqa: BLE001
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
        await live.bus.publish("error", session_id=live.id, message=error)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        live.agent.accepting_followups = False
        live.agent.external_followup_loader = None
        try:
            await registry.session_repository.flush()
            await live.bus.flush()
            await live.bus.publish("done", session_id=live.id, stats=registry.stats(live.id), status=status)
        finally:
            try:
                await registry.run_states.finish(run_id, status=status, error=error)
            finally:
                if live.current_run_id == run_id:
                    live.run_kind = None
                    live.current_run_id = None
                    live.bus.run_id = None


async def _execute_team_run(live: Any, req: TeamRequest, run_id: str) -> None:
    owner = asyncio.current_task()
    assert owner is not None
    watcher = asyncio.create_task(_watch_shared_cancel(live, run_id, owner))
    status, error, result = "done", None, None
    user_entry_id: str | None = None
    try:
        await live.bus.publish("session", session_id=live.id, team=req.team, run_id=run_id)
        # 团队成员有各自的子会话，但用户输入和团队最终交付仍应进入父会话。
        # 否则刷新页面后整轮对话会消失，重发/分支也没有可操作的消息节点。
        user_entry_id = live.agent.session.append({"role": "user", "content": req.task})
        live.bus.emit("user_message_committed", entry_id=user_entry_id)
        team_runner = TeamRunner.create(req.team, registry.llm, registry.tools, live.manager, live.bus)
        if req.team == "dramaTeam":
            runner = ProductionRunner(
                team_runner,
                registry.production_runs,
                session_id=live.id,
                canvas_name=req.canvas,
                production_run_id=req.production_run_id,
            )
            result = await runner.run(req.task)
        else:
            result = await team_runner.run(req.task)
        status = result.status
        if result.final.strip():
            assistant_entry_id = live.agent.session.append({"role": "assistant", "content": result.final})
            live.bus.emit(
                "assistant_message_committed",
                entry_id=assistant_entry_id,
                parent_id=user_entry_id,
            )
        await live.bus.flush()
    except asyncio.CancelledError:
        watcher.cancel()
        status = "aborted"
        live.cancel.set()
        await live.bus.publish("aborted", session_id=live.id, reason="user_cancelled")
    except Exception as exc:  # noqa: BLE001
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
        await live.bus.publish("error", session_id=live.id, message=error)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        payload: dict[str, Any] = {"stats": registry.stats(live.id), "status": status}
        if result is not None:
            payload.update(
                {
                    "result": result.final,
                    "usage": result.usage.as_dict(),
                    "usage_all": result.usage_all.as_dict(),
                    "production_run_id": result.production_run_id,
                }
            )
        try:
            await registry.session_repository.flush()
            await live.bus.flush()
            await live.bus.publish("team_done", session_id=live.id, **payload)
        finally:
            try:
                await registry.run_states.finish(run_id, status=status, error=error)
            finally:
                if live.current_run_id == run_id:
                    live.run_kind = None
                    live.current_run_id = None
                    live.bus.run_id = None


@app.post("/api/agent")
async def chat(req: ChatRequest) -> StreamingResponse:
    try:
        live = registry.get_or_create(req.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    registry.bind_canvas(live, req.canvas)
    # 工具集从 full_tools **派生**：不就地改会话状态，否则一次 use_sub_agent=False
    # 会让这个会话之后的每次请求都变少工具（开关粘住）。
    # Agent 只负责把方案做到可生成。真实生成始终走独立审批接口，防止模型自己
    # 调用外部付费 Provider。list_media_models 仍保留，便于它按可用模型写提示词。
    live.agent.tools = live.tools_for(req.use_sub_agent, allow_media_generation=False)
    policy_rules = {
        "plan_only": "用户选择了仅策划：只完成剧本、分镜、资产和正式提示词，不建议立即生成媒体。",
        "ask": "用户选择了逐步询问：遇到会显著改变作品的缺失信息时，集中提出 1-3 个短问题并等待回答；方案完成后提醒用户审阅，不得自行生成。",
        "auto_review": "用户选择了自动准备：信息足够时直接完成全部生产包和生成节点；缺少关键创作信息时仍先提 1-3 个问题。完成后进入人工审批，未审批不得生成。",
    }
    live.agent.session.system_prompt = (
        build_system_prompt(registry.workspace, live.agent.tools, registry.skills)
        + "\n<media-approval>\n"
        + policy_rules[req.media_policy]
        + "\n任何图片或视频生成都必须经过前端审批卡的本次明确确认；不要声称已生成尚未生成的媒体。"
        + "\n需要询问时直接在对话正文中提出清晰、可回答的问题，然后结束本轮等待用户。\n</media-approval>"
    )

    try:
        start_event_id = await registry.event_store.latest_id(live.id) or "0-0"
        run_id = await registry.begin_persisted_run(live, "agent", start_event_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    queue = live.bus.subscribe()
    live.runs += 1
    task = asyncio.create_task(_execute_agent_run(live, req, run_id))
    live.task = task
    return StreamingResponse(
        _stream_run(live, queue, task, {"done"}),
        media_type="application/x-ndjson",
        headers={"X-Session-ID": live.id, "X-Run-ID": run_id},
    )


@app.post("/api/team")
async def run_team(req: TeamRequest) -> StreamingResponse:
    """按 team.json 编排成员：同样返回 NDJSON 事件流。"""
    try:
        live = registry.get_or_create(req.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    registry.bind_canvas(live, req.canvas)
    live.agent.tools = live.tools_for(True)
    try:
        start_event_id = await registry.event_store.latest_id(live.id) or "0-0"
        run_id = await registry.begin_persisted_run(live, "team", start_event_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    queue = live.bus.subscribe()
    live.runs += 1
    task = asyncio.create_task(_execute_team_run(live, req, run_id))
    live.task = task
    return StreamingResponse(
        _stream_run(live, queue, task, {"team_done"}),
        media_type="application/x-ndjson",
        headers={"X-Session-ID": live.id, "X-Run-ID": run_id},
    )


@app.get("/api/events/stream")
async def resume_events(session_id: str, run_id: str, last_event_id: str = "0-0") -> StreamingResponse:
    """从递增事件游标后补发，再无缝切回当前运行的实时队列。"""
    try:
        registry.session_path(session_id)
        cursor = validate_event_id(last_event_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not re.fullmatch(r"[a-f0-9]{32}", run_id or ""):
        raise HTTPException(status_code=400, detail="运行 ID 无效")

    live = registry.sessions.get(session_id)
    queue = live.bus.subscribe() if live else None

    async def replay() -> AsyncIterator[bytes]:
        nonlocal cursor
        seen: set[str] = set()
        terminal_seen = False
        try:
            oldest = await registry.event_store.oldest_id(session_id)
            if cursor != "0-0" and oldest and event_id_after(oldest, cursor):
                yield _line(
                    {
                        "type": "replay_gap",
                        "session_id": session_id,
                        "run_id": run_id,
                        "last_event_id": cursor,
                        "oldest_event_id": oldest,
                    }
                )
            while True:
                rows = await registry.event_store.read_after(session_id, cursor, limit=1000)
                if not rows:
                    break
                for item in rows:
                    cursor = item.event_id
                    if item.payload.get("run_id") != run_id:
                        continue
                    payload = {**item.payload, "event_id": item.event_id}
                    seen.add(item.event_id)
                    terminal_seen = payload.get("type") in {"done", "team_done"} or terminal_seen
                    yield _line(payload)
                if len(rows) < 1000:
                    break
            if terminal_seen:
                return

            # 请求可能落到没有执行该任务的另一实例：没有本地 EventBus 时，
            # 直接以 Redis 游标轮询补发，直到看到终态或共享运行状态结束。
            if queue is None:
                last_event = time.monotonic()
                while True:
                    await asyncio.sleep(0.25)
                    rows = await registry.event_store.read_after(session_id, cursor, limit=1000)
                    delivered = False
                    for item in rows:
                        cursor = item.event_id
                        if item.payload.get("run_id") != run_id:
                            continue
                        delivered = True
                        payload = {**item.payload, "event_id": item.event_id}
                        yield _line(payload)
                        if payload.get("type") in {"done", "team_done"}:
                            return
                    if delivered:
                        last_event = time.monotonic()
                    state = await registry.run_states.get(run_id)
                    if state is None or state.status != "running":
                        return
                    if time.monotonic() - last_event >= 5:
                        last_event = time.monotonic()
                        yield _line(
                            {"type": "ping", "session_id": session_id, "run_id": run_id, "reconnected": True}
                        )

            started = time.monotonic()
            last_event = started
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    active = bool(live and live.task and not live.task.done() and live.current_run_id == run_id)
                    if not active and queue.empty():
                        return
                    if time.monotonic() - last_event >= 5:
                        last_event = time.monotonic()
                        yield _line({"type": "ping", "session_id": session_id, "run_id": run_id, "reconnected": True})
                    continue
                if event.run_id != run_id or (event.event_id and event.event_id in seen):
                    continue
                last_event = time.monotonic()
                if event.event_id:
                    seen.add(event.event_id)
                    cursor = event.event_id
                yield _line(event.to_dict())
                if event.type in {"done", "team_done"}:
                    return
        finally:
            if live is not None and queue is not None:
                live.bus.unsubscribe(queue)

    return StreamingResponse(replay(), media_type="application/x-ndjson")


@app.get("/api/teams")
async def teams() -> dict[str, Any]:
    out = []
    for path in sorted(TEAMS_ROOT.glob("*/team.json")):
        try:
            manifest, _ = load_team(path.parent.name)
            out.append(
                {
                    "name": manifest.name,
                    "display_name": manifest.display_name,
                    "entry": manifest.entry,
                    "members": {
                        name: {"tools": m.tools, "delegates": m.delegates}
                        for name, m in manifest.members.items()
                    },
                }
            )
        except Exception as exc:  # noqa: BLE001
            out.append({"name": path.parent.name, "error": str(exc)})
    return {"teams": out}


@app.get("/api/production/runs/{production_run_id}")
async def production_run_detail(production_run_id: str) -> dict[str, Any]:
    try:
        item = await registry.production_runs.get(production_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if item is None:
        raise HTTPException(status_code=404, detail="生产任务不存在")
    return item


@app.get("/api/sessions/{session_id}/production-run")
async def latest_production_run(session_id: str) -> dict[str, Any]:
    try:
        registry.session_path(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    item = await registry.production_runs.latest_for_session(session_id)
    return {"production_run": item}


@app.post("/api/agent/cancel")
async def cancel(req: CancelRequest) -> dict[str, Any]:
    try:
        registry.session_path(req.session_id)
        ok = await registry.cancel_persisted(req.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True, "session_id": req.session_id}


@app.post("/api/agent/followup")
async def followup(req: FollowupRequest) -> dict[str, Any]:
    try:
        registry.session_path(req.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="补充要求不能为空")
    active = await registry.run_states.active_run(req.session_id)
    if active is None:
        raise HTTPException(status_code=409, detail="当前会话没有正在运行的 Agent")
    if active.kind != "agent":
        raise HTTPException(status_code=409, detail="团队运行暂不支持 Follow-up 队列")
    try:
        pending = await registry.run_states.enqueue_followup(active.run_id, text)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "session_id": req.session_id, "run_id": active.run_id, "pending": pending}


# ---------------------------------------------------------------------------
# 工具与统计
# ---------------------------------------------------------------------------
@app.get("/api/sessions")
async def list_sessions(canvas: str | None = None) -> dict[str, Any]:
    return {"sessions": registry.list_sessions(canvas)}


@app.post("/api/sessions")
async def create_session(req: CreateSessionRequest) -> dict[str, Any]:
    return registry.create_session(req.canvas, req.title)


@app.post("/api/sessions/{session_id}/bind")
async def bind_session(session_id: str, req: BindSessionRequest) -> dict[str, Any]:
    try:
        path = registry.session_path(session_id)
        if not path.exists() and session_id not in registry.sessions:
            raise HTTPException(status_code=404, detail="会话不存在")
        live = registry.get_or_create(session_id)
        registry.bind_canvas(live, req.canvas)
        return {"ok": True, "session_id": session_id, "canvas": req.canvas}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/sessions/{session_id}/rename")
async def rename_session(session_id: str, req: RenameSessionRequest) -> dict[str, Any]:
    try:
        return registry.rename_session(session_id, req.title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/sessions/{session_id}/delete")
async def delete_session(session_id: str) -> dict[str, Any]:
    try:
        path = registry.session_path(session_id)
        if not path.exists() and session_id not in registry.sessions:
            raise HTTPException(status_code=404, detail="会话不存在")
        return {"ok": True, "deleted": registry.delete_session(session_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/sessions/{session_id}/messages/{entry_id}")
async def mutate_message(session_id: str, entry_id: str, req: MessageActionRequest) -> dict[str, Any]:
    try:
        registry.session_path(session_id)
        return registry.mutate_message(session_id, entry_id, req.action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/sessions/{session_id}/questions/{question_id}/answer")
async def answer_question(session_id: str, question_id: str, req: QuestionAnswerRequest) -> dict[str, Any]:
    try:
        registry.session_path(session_id)
        return await registry.answer_question(session_id, question_id, req.answer)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    try:
        path = registry.session_path(session_id)
        if not path.exists() and session_id not in registry.sessions:
            raise HTTPException(status_code=404, detail="会话不存在")
        return await registry.session_detail_async(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/tools")
async def tools() -> dict[str, Any]:
    return {
        "workspace": str(WORKSPACE),
        "tools": [
            {
                "name": entry.name,
                "display_name": entry.display_name,
                "description": entry.metadata.description if entry.metadata else "",
                "version": entry.metadata.version if entry.metadata else "",
                "enabled": entry.enabled,
                "load_error": entry.load_error,
            }
            for entry in list_tools()
        ],
        "loaded": [t.name for t in registry.tools] + ["ask_user", "subagent", "report"],
        "mcp": registry.mcp.status(),
    }


@app.get("/api/mcp/servers")
async def mcp_servers() -> dict[str, Any]:
    """MCP connection health without exposing commands, URLs or environment values."""
    return {"config": str(registry.mcp.config_path), "servers": registry.mcp.status()}


@app.post("/api/tools/reload")
async def reload_tool_api(req: ReloadRequest) -> dict[str, Any]:
    try:
        return {"ok": True, **registry.reload(req.name)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/sessions/{session_id}/stats")
async def stats(session_id: str) -> dict[str, Any]:
    try:
        data = registry.stats(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not data:
        raise HTTPException(status_code=404, detail="会话不存在")
    return data


@app.get("/api/sessions/{session_id}/entries")
async def entries(session_id: str, limit: int = 60) -> dict[str, Any]:
    """看落盘的 JSONL 条目（压缩前的原始对话也在这里）。"""
    try:
        return {"session_id": session_id, "entries": registry.entries(session_id, limit)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/canvas/connect")
async def connect_nodes(req: ConnectRequest) -> dict[str, Any]:
    """连线（带校验）：端口必须存在、类型必须兼容、不许自环。

    校验放在服务端而不是前端：类型规则只有一份，改的时候不会两边不一致。
    被拒绝时返回 400 + 可读原因，前端直接提示给用户。
    """
    canvas = await registry.canvas_repository.load(req.canvas)
    _require_revision(canvas, req.revision)
    try:
        edge = canvas.connect(req.source, req.source_handle, req.target, req.target_handle)
    except CanvasError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await registry.canvas_repository.save(canvas)
    return {"ok": True, "canvas": canvas.name, "edge_id": edge.id, "revision": canvas.revision, "canvas_changed": canvas.name}


@app.post("/api/canvas/edges/delete")
async def delete_canvas_edge(req: DeleteEdgeRequest) -> dict[str, Any]:
    """按边 ID 精确删除连线，不删除或改动两端节点。"""
    canvas = await registry.canvas_repository.load(req.canvas)
    _require_revision(canvas, req.revision)
    removed = canvas.remove_edges([req.edge_id])
    if not removed:
        raise HTTPException(status_code=404, detail="连线不存在或已被删除")
    await registry.canvas_repository.save(canvas)
    return {
        "ok": True,
        "canvas": canvas.name,
        "edge_id": req.edge_id,
        "removed": removed,
        "revision": canvas.revision,
        "canvas_changed": canvas.name,
    }


@app.post("/api/canvas/arrange")
async def arrange_canvas(req: RunCanvasRequest) -> dict[str, Any]:
    """按拓扑分层重排节点（排列逻辑在服务端，前端只是视图）。"""
    canvas = await registry.canvas_repository.load(req.canvas)
    _require_revision(canvas, req.revision)
    try:
        moved = canvas.arrange()
    except CanvasError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await registry.canvas_repository.save(canvas)
    return {"ok": True, "canvas": canvas.name, "moved": moved, "nodes": len(canvas.nodes), "revision": canvas.revision}


def _generation_execution_plan(req: RunCanvasRequest, canvas: Canvas) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    jobs = canvas.generation_jobs()
    if req.only:
        jobs = [job for job in jobs if job["node_id"] in set(req.only)]
    planned: list[dict[str, Any]] = []
    for job in jobs:
        available = [
            item for item in default_registry.list_models()
            if item.get("provider") == req.provider and item.get("kind") == job["kind"]
        ]
        selected_model = req.model if any(item.get("id") == req.model for item in available) else (
            str(available[0]["id"]) if available else ""
        )
        planned.append(
            {
                "node_id": job["node_id"],
                "kind": job["kind"],
                "ready": bool(job["ready"]),
                "prompt_hash": canonical_hash(job.get("prompt") or ""),
                "references": [
                    {"path": ref.get("path", ""), "type": ref.get("type", ""), "mime_type": ref.get("mime_type", "")}
                    for ref in job.get("refs", []) if ref.get("path")
                ],
                "model": selected_model,
            }
        )
    payload = {
        "canvas": canvas.name,
        "canvas_revision": canvas.revision,
        "provider": req.provider,
        "requested_model": req.model,
        "duration": req.duration,
        "resolution": req.resolution,
        "ratio": req.ratio,
        "generate_audio": req.generate_audio,
        "production_run_id": req.production_run_id,
        "jobs": planned,
    }
    return jobs, payload


@app.post("/api/canvas/approvals")
async def create_canvas_approval(req: RunCanvasRequest) -> dict[str, Any]:
    """把用户看到的完整生成参数固化为一次性审批，后续任何变化都会使哈希失配。"""
    canvas = await registry.canvas_repository.load(req.canvas)
    _require_revision(canvas, req.revision)
    jobs, payload = _generation_execution_plan(req, canvas)
    if not jobs:
        raise HTTPException(status_code=400, detail="没有可审批的生成节点")
    missing_model = [item["node_id"] for item in payload["jobs"] if item["ready"] and not item["model"]]
    if missing_model:
        raise HTTPException(status_code=400, detail=f"Provider {req.provider} 缺少节点需要的模型")
    approval = new_approval(f"canvas.run:{canvas.name}", payload)
    await registry.approvals.create(approval)
    return {
        "ok": True,
        "approval_id": approval.approval_id,
        "args_hash": approval.args_hash,
        "expires_at": approval.expires_at,
        "canvas": canvas.name,
        "total": len(jobs),
        "ready": sum(1 for job in jobs if job["ready"]),
    }


@app.post("/api/canvas/run")
async def run_canvas(req: RunCanvasRequest) -> dict[str, Any]:
    """审批后按拓扑序执行生成节点；没有本次明确审批一律拒绝。"""
    if not req.approved or not req.approval_id:
        raise HTTPException(status_code=428, detail="需要人工审批：请确认 Provider、模型和生成节点后再执行")
    canvas = await registry.canvas_repository.load(req.canvas)
    _require_revision(canvas, req.revision)
    jobs, execution_payload = _generation_execution_plan(req, canvas)
    if not jobs:
        raise HTTPException(
            status_code=400,
            detail="没有可执行的生成节点：先建 image_gen / video_gen 节点，并把 prompt 节点的 prompt 端口连过去",
        )
    try:
        await registry.approvals.consume(
            req.approval_id,
            f"canvas.run:{canvas.name}",
            canonical_hash(execution_payload),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    job_key = canonical_hash({"approval_id": req.approval_id, "execution": execution_payload})
    job_id = f"media_{job_key[:32]}"
    plan_by_node = {item["node_id"]: item for item in execution_payload["jobs"]}
    frozen_jobs = [
        {
            "node_id": job["node_id"],
            "label": job["label"],
            "kind": job["kind"],
            "ready": bool(job["ready"]),
            "prompt": job.get("prompt") or "",
            "references": [
                {"path": ref["path"], "type": ref["type"], "mime_type": ref.get("mime_type", "")}
                for ref in job.get("refs", []) if ref.get("path")
            ],
            "model": plan_by_node[job["node_id"]]["model"],
            "args_hash": canonical_hash(plan_by_node[job["node_id"]]),
            "generation_id": f"{job_id}:{job['node_id']}",
        }
        for job in jobs
    ]
    queued_payload = {
        "canvas": canvas.name,
        "approval_id": req.approval_id,
        "provider": req.provider,
        "requested_model": req.model,
        "duration": req.duration,
        "resolution": req.resolution,
        "ratio": req.ratio,
        "generate_audio": req.generate_audio,
        "production_run_id": req.production_run_id,
        "jobs": frozen_jobs,
    }
    # generation_id 在入队前写入节点；旧 Worker 返回时必须再次校验它。
    # 已存在的幂等任务不能重写该值，否则用户重放旧 HTTP 请求会让旧任务重新成为“当前代”。
    if await media_jobs.get(job_id) is None:
        canvas = await registry.canvas_repository.load(req.canvas)
        _require_revision(canvas, int(execution_payload["canvas_revision"]))
        for item in frozen_jobs:
            if item["ready"]:
                canvas.update_node(item["node_id"], data={"active_generation_id": item["generation_id"]})
        await registry.canvas_repository.save(canvas)
    queued, created = await media_jobs.enqueue(
        MediaJob(
            job_id=job_id,
            idempotency_key=job_key,
            approval_id=req.approval_id,
            canvas=canvas.name,
            payload_json=canonical_json(queued_payload),
        )
    )
    await _update_production_stage(
        req.production_run_id,
        canvas.name,
        "generating",
        "running",
        "generating",
        artifact_ids=[item["node_id"] for item in frozen_jobs if item["ready"]],
    )
    return {
        "ok": True,
        "accepted": True,
        "created": created,
        "total": len(jobs),
        "ready": sum(1 for job in jobs if job["ready"]),
        **queued.public(),
    }


def _composition_execution_plan(req: ComposeCanvasRequest, canvas: Canvas) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    jobs = canvas.timeline_jobs()
    if req.only:
        selected = set(req.only)
        jobs = [job for job in jobs if job["node_id"] in selected]
    frozen: list[dict[str, Any]] = []
    for job in jobs:
        clips: list[dict[str, Any]] = []
        for clip in job["clips"]:
            item = dict(clip)
            if item.get("path"):
                item["asset"] = composition_service.inspect(item["path"], "video")
            clips.append(item)
        audio = dict(job["audio"]) if job.get("audio") else None
        if audio and audio.get("path"):
            audio["asset"] = composition_service.inspect(audio["path"], "audio")
        frozen.append(
            {
                "node_id": job["node_id"],
                "label": job["label"],
                "ready": bool(job["ready"]),
                "clips": clips,
                "audio": audio,
                "tts": dict(job["tts"]) if job.get("tts") else None,
                "subtitle_text": job.get("subtitle_text") or "",
                "subtitle_hash": canonical_hash(job.get("subtitle_text") or ""),
                "output": job["output"],
            }
        )
    payload = {
        "job_kind": "composition",
        "canvas": canvas.name,
        "canvas_revision": canvas.revision,
        "production_run_id": req.production_run_id,
        "jobs": frozen,
    }
    return jobs, payload


@app.post("/api/canvas/composition-approvals")
async def create_composition_approval(req: ComposeCanvasRequest) -> dict[str, Any]:
    capability = ffmpeg_capabilities()
    if not capability["available"]:
        raise HTTPException(status_code=503, detail=capability["reason"])
    canvas = await registry.canvas_repository.load(req.canvas)
    _require_revision(canvas, req.revision)
    try:
        jobs, payload = _composition_execution_plan(req, canvas)
    except CompositionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not jobs:
        raise HTTPException(status_code=400, detail="没有时间线节点可供合成")
    if not any(job["ready"] for job in jobs):
        raise HTTPException(status_code=400, detail="时间线还没有全部就绪：请连接已经生成或上传的视频")
    approval = new_approval(f"canvas.compose:{canvas.name}", payload)
    await registry.approvals.create(approval)
    return {
        "ok": True,
        "approval_id": approval.approval_id,
        "args_hash": approval.args_hash,
        "expires_at": approval.expires_at,
        "canvas": canvas.name,
        "total": len(jobs),
        "ready": sum(1 for job in jobs if job["ready"]),
        "plans": payload["jobs"],
    }


@app.post("/api/canvas/compose")
async def compose_canvas(req: ComposeCanvasRequest) -> dict[str, Any]:
    if not req.approved or not req.approval_id:
        raise HTTPException(status_code=428, detail="需要人工审批：请确认片段顺序、裁剪范围和输出规格后再合成")
    canvas = await registry.canvas_repository.load(req.canvas)
    _require_revision(canvas, req.revision)
    try:
        jobs, execution_payload = _composition_execution_plan(req, canvas)
    except CompositionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        await registry.approvals.consume(
            req.approval_id,
            f"canvas.compose:{canvas.name}",
            canonical_hash(execution_payload),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    job_key = canonical_hash({"approval_id": req.approval_id, "execution": execution_payload})
    job_id = f"media_{job_key[:32]}"
    frozen_jobs = []
    for item in execution_payload["jobs"]:
        frozen_jobs.append(
            {
                **item,
                "generation_id": f"{job_id}:{item['node_id']}",
                "args_hash": canonical_hash(item),
            }
        )
    queued_payload = {
        "job_kind": "composition",
        "canvas": canvas.name,
        "approval_id": req.approval_id,
        "production_run_id": req.production_run_id,
        "jobs": frozen_jobs,
    }
    if await media_jobs.get(job_id) is None:
        canvas = await registry.canvas_repository.load(req.canvas)
        _require_revision(canvas, int(execution_payload["canvas_revision"]))
        for item in frozen_jobs:
            if item["ready"]:
                canvas.update_node(item["node_id"], data={"active_generation_id": item["generation_id"]})
                if item.get("tts"):
                    canvas.update_node(item["tts"]["node_id"], data={"active_generation_id": item["generation_id"]})
        await registry.canvas_repository.save(canvas)
    queued, created = await media_jobs.enqueue(
        MediaJob(
            job_id=job_id,
            idempotency_key=job_key,
            approval_id=req.approval_id,
            canvas=canvas.name,
            payload_json=canonical_json(queued_payload),
        )
    )
    await _update_production_stage(
        req.production_run_id,
        canvas.name,
        "composing",
        "running",
        "composing",
        artifact_ids=[item["node_id"] for item in frozen_jobs if item["ready"]],
    )
    return {
        "ok": True,
        "accepted": True,
        "created": created,
        "total": len(jobs),
        "ready": sum(1 for job in jobs if job["ready"]),
        **queued.public(),
    }


async def _save_composition_state(
    canvas_name: str,
    node_id: str,
    generation_id: str,
    *,
    status: str,
    output: dict[str, Any] | None = None,
    output_port: str = "video",
    output_type: str = "VIDEO",
) -> bool:
    """CAS retry for Worker writes; never overwrites a newer generation."""
    for _ in range(4):
        canvas = await registry.canvas_repository.load(canvas_name)
        node = canvas.require(node_id)
        if node.data.get("active_generation_id") != generation_id:
            return False
        if output is not None:
            node.outputs[output_port] = {"data_type": output_type, "value": output}
        canvas.update_node(node_id, status=status)
        try:
            await registry.canvas_repository.save(canvas)
            return True
        except CanvasConflictError:
            continue
    raise CanvasConflictError(canvas_name, canvas.revision, (await registry.canvas_repository.load(canvas_name)).revision)


async def _execute_composition_job(task: MediaJob, payload: dict[str, Any]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    done = 0
    for item in payload.get("jobs", []):
        if not item.get("ready"):
            results.append({"node_id": item["node_id"], "label": item["label"], "ok": False, "error": "输入视频尚未就绪"})
            continue
        generation_id = str(item["generation_id"])
        if not await _save_composition_state(task.canvas, item["node_id"], generation_id, status="generating"):
            results.append({
                "node_id": item["node_id"], "label": item["label"], "ok": False,
                "error": "时间线已有更新一代任务，旧合成已停止", "stale_discarded": True,
            })
            continue
        render_plan = {
            "clips": [
                {
                    "path": clip["path"],
                    "trim_start": clip.get("trim_start", 0),
                    "trim_end": clip.get("trim_end"),
                }
                for clip in item["clips"]
            ],
            "audio": {"path": item["audio"]["path"]} if item.get("audio") else None,
            "output": item["output"],
        }
        if item.get("tts") and not render_plan["audio"]:
            try:
                narration = await asyncio.to_thread(
                    tts_registry.synthesize,
                    item["tts"],
                    f"{task.job_id}-{item['tts']['node_id']}",
                )
                audio_output = {
                    "path": narration.path,
                    "mime": narration.mime,
                    "provider": narration.provider,
                    "mock": narration.mock,
                    "generation_id": generation_id,
                }
                await _save_composition_state(
                    task.canvas,
                    item["tts"]["node_id"],
                    generation_id,
                    status="done",
                    output=audio_output,
                    output_port="audio",
                    output_type="AUDIO",
                )
                await _register_media_asset(
                    canvas=task.canvas,
                    node_id=item["tts"]["node_id"],
                    media_type="audio",
                    path=narration.path,
                    content_type=narration.mime,
                    provider=narration.provider,
                    generation_id=generation_id,
                )
                render_plan["audio"] = {"path": narration.path}
            except Exception as exc:  # noqa: BLE001
                await _save_composition_state(
                    task.canvas, item["node_id"], generation_id, status="error"
                )
                results.append({
                    "node_id": item["node_id"], "label": item["label"], "ok": False,
                    "error": f"旁白生成失败：{type(exc).__name__}: {exc}",
                })
                continue
        duration = sum(float((clip.get("asset") or {}).get("duration") or 0) for clip in item["clips"])
        if item.get("subtitle_text"):
            subtitle_relative = f"assets/rendered/{task.job_id}/{item['node_id']}.srt"
            subtitle_path = WORKSPACE / subtitle_relative
            await asyncio.to_thread(write_srt, subtitle_path, item["subtitle_text"], duration)
            render_plan["subtitle"] = {"path": subtitle_relative}
        try:
            rendered = await asyncio.to_thread(
                composition_service.render,
                render_plan,
                f"{task.job_id}-{item['node_id']}",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await _save_composition_state(task.canvas, item["node_id"], generation_id, status="error")
            results.append({
                "node_id": item["node_id"], "label": item["label"], "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        output = {
            "path": rendered.path,
            "mime": rendered.mime,
            "provider": "ffmpeg",
            "duration": rendered.duration,
            "width": rendered.width,
            "height": rendered.height,
            "fps": rendered.fps,
            "file_size": rendered.file_size,
            "sha256": rendered.sha256,
            "generation_id": generation_id,
        }
        if not await _save_composition_state(
            task.canvas, item["node_id"], generation_id, status="done", output=output
        ):
            results.append({
                "node_id": item["node_id"], "label": item["label"], "ok": False,
                "error": "合成完成，但时间线已更新，旧结果未覆盖新版本", "path": rendered.path,
                "stale_discarded": True,
            })
            continue
        await _register_media_asset(
            canvas=task.canvas,
            node_id=item["node_id"],
            media_type="video",
            path=rendered.path,
            content_type=rendered.mime,
            provider="ffmpeg",
            generation_id=generation_id,
        )
        done += 1
        results.append({"node_id": item["node_id"], "label": item["label"], "ok": True, **output})
    total = len(payload.get("jobs", []))
    production_run_id = str(payload.get("production_run_id") or "") or None
    passed = total > 0 and done == total
    await _update_production_stage(
        production_run_id,
        task.canvas,
        "composing",
        "succeeded" if passed else "failed",
        "final_review" if passed else "limited",
        problems=[] if passed else [item.get("error", "成片合成未完成") for item in results if not item.get("ok")],
        artifact_ids=[item["node_id"] for item in results if item.get("ok")],
    )
    if passed:
        await _update_production_stage(
            production_run_id,
            task.canvas,
            "final_review",
            "succeeded",
            "completed",
            artifact_ids=[item["node_id"] for item in results if item.get("ok")],
        )
        await _update_production_stage(
            production_run_id,
            task.canvas,
            "completed",
            "succeeded",
            "completed",
            artifact_ids=[item["node_id"] for item in results if item.get("ok")],
        )
    return {"ok": passed, "canvas": task.canvas, "kind": "composition", "total": total, "done": done, "results": results}


async def _execute_media_job(task: MediaJob) -> dict[str, Any]:
    """Worker 使用审批时冻结的提示词和参数执行，不重新读取可能已变化的输入。"""
    payload = json.loads(task.payload_json)
    if payload.get("job_kind") == "composition":
        return await _execute_composition_job(task, payload)
    canvas_name = str(payload["canvas"])
    canvas = await registry.canvas_repository.load(canvas_name)
    results: list[dict[str, Any]] = []
    done = 0

    async def latest_canvas() -> Canvas:
        return await registry.canvas_repository.load(canvas_name)

    for job in payload["jobs"]:
        if not job["ready"]:
            results.append({"node_id": job["node_id"], "label": job["label"], "ok": False, "error": "没有输入提示词"})
            continue
        canvas = await latest_canvas()
        current_node = canvas.require(job["node_id"])
        if current_node.data.get("active_generation_id") != job["generation_id"]:
            results.append({
                "node_id": job["node_id"], "label": job["label"], "ok": False,
                "error": "该节点已有更新一代生成任务，本任务已停止", "stale_discarded": True,
                "generation_id": job["generation_id"],
            })
            continue
        idempotency_key = canonical_hash({"approval_id": task.approval_id, "node_id": job["node_id"]})
        invocation, created = await registry.invocations.prepare(
            InvocationState(
                invocation_id=f"inv_{idempotency_key[:32]}",
                idempotency_key=idempotency_key,
                session_id="",
                run_id=task.job_id,
                tool_name="media.generate",
                args_hash=job["args_hash"],
                args_json=canonical_json(job),
            )
        )
        resume_operation_id = ""
        if not created:
            if invocation.args_hash != job["args_hash"]:
                results.append({
                    "node_id": job["node_id"], "label": job["label"], "ok": False,
                    "error": "媒体调用幂等键与冻结参数冲突", "invocation_id": invocation.invocation_id,
                })
                continue
            if invocation.status in {"succeeded", "failed"} and invocation.result_json:
                restored = json.loads(invocation.result_json)
                canvas = await latest_canvas()
                if canvas.require(job["node_id"]).data.get("active_generation_id") == job["generation_id"]:
                    canvas.update_node(job["node_id"], status="done" if restored.get("ok") else "error")
                    await registry.canvas_repository.save(canvas)
                results.append({**restored, "idempotent_replay": True, "invocation_id": invocation.invocation_id})
                if restored.get("ok"):
                    done += 1
                continue
            if invocation.status in {"executing", "unknown"}:
                if invocation.remote_operation_id:
                    resume_operation_id = invocation.remote_operation_id
                else:
                    canvas = await latest_canvas()
                    if canvas.require(job["node_id"]).data.get("active_generation_id") == job["generation_id"]:
                        canvas.update_node(job["node_id"], status="error")
                        await registry.canvas_repository.save(canvas)
                    results.append({
                        "node_id": job["node_id"], "label": job["label"], "ok": False,
                        "error": "上次生成结果不确定且没有 operation_id，已禁止自动重试",
                        "invocation_id": invocation.invocation_id,
                    })
                    continue
        canvas = await latest_canvas()
        if canvas.require(job["node_id"]).data.get("active_generation_id") != job["generation_id"]:
            results.append({
                "node_id": job["node_id"], "label": job["label"], "ok": False,
                "error": "开始执行前发现节点已有更新一代任务", "stale_discarded": True,
            })
            continue
        canvas.update_node(job["node_id"], status="generating")
        await registry.canvas_repository.save(canvas)
        if not resume_operation_id:
            try:
                await registry.invocations.mark_executing(invocation.invocation_id)
            except RuntimeError:
                current = await registry.invocations.get(invocation.invocation_id)
                canvas = await latest_canvas()
                if canvas.require(job["node_id"]).data.get("active_generation_id") == job["generation_id"]:
                    canvas.update_node(job["node_id"], status="error")
                    await registry.canvas_repository.save(canvas)
                results.append({
                    "node_id": job["node_id"], "label": job["label"], "ok": False,
                    "error": "相同生成调用正在执行或状态不确定，已禁止并发重复执行",
                    "invocation_id": invocation.invocation_id,
                    "invocation_status": current.status if current else "missing",
                })
                continue
        operation_seen: dict[str, str] = {}
        try:
            selected_model = job["model"]
            if not selected_model:
                raise ValueError(f"Provider {payload['provider']} 没有可用的 {job['kind']} 模型")
            loop = asyncio.get_running_loop()

            def remember_operation(operation_id: str) -> None:
                operation_seen["id"] = operation_id
                future = asyncio.run_coroutine_threadsafe(
                    registry.invocations.set_remote_operation(invocation.invocation_id, operation_id), loop
                )
                future.result(timeout=10)

            media_request = {
                "prompt": job["prompt"],
                "provider": payload["provider"],
                "model": selected_model,
                "size": "1024x576" if job["kind"] == "image" else "1280x720",
                "duration": payload["duration"],
                "resolution": payload["resolution"],
                "ratio": payload["ratio"],
                "generate_audio": payload["generate_audio"],
                "references": job["references"],
                "operation_callback": remember_operation,
            }
            if resume_operation_id:
                media = await asyncio.to_thread(
                    default_registry.resume, job["kind"], resume_operation_id, media_request, WORKSPACE
                )
            else:
                media = await asyncio.to_thread(default_registry.generate, job["kind"], media_request, WORKSPACE)
        except asyncio.CancelledError:
            await registry.invocations.mark_unknown(invocation.invocation_id, "媒体 Worker 关闭时调用仍在执行")
            raise
        except Exception as exc:  # noqa: BLE001
            canvas = await latest_canvas()
            if canvas.require(job["node_id"]).data.get("active_generation_id") == job["generation_id"]:
                canvas.update_node(job["node_id"], status="error")
                await registry.canvas_repository.save(canvas)
            failed = {"node_id": job["node_id"], "label": job["label"], "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if operation_seen or resume_operation_id:
                await registry.invocations.mark_unknown(invocation.invocation_id, failed["error"])
                raise MediaJobUncertain(
                    f"节点 {job['label']} 的远端任务状态待核对；operation_id="
                    f"{operation_seen.get('id') or resume_operation_id}"
                ) from exc
            else:
                await registry.invocations.complete(invocation.invocation_id, failed, failed=True)
            results.append({**failed, "invocation_id": invocation.invocation_id})
            continue
        canvas = await latest_canvas()
        if canvas.require(job["node_id"]).data.get("active_generation_id") != job["generation_id"]:
            stale = {
                "node_id": job["node_id"], "label": job["label"], "ok": False,
                "error": "生成已完成，但节点已有更新一代任务，旧结果未写入画布",
                "path": media.path, "provider": media.provider, "stale_discarded": True,
                "generation_id": job["generation_id"],
            }
            await registry.invocations.complete(invocation.invocation_id, stale)
            results.append({**stale, "invocation_id": invocation.invocation_id})
            continue
        node = canvas.require(job["node_id"])
        port = "image" if job["kind"] == "image" else "video"
        node.outputs[port] = {
            "data_type": port.upper(),
            "value": {"path": media.path, "mime": media.mime, "mock": media.mock, "provider": media.provider},
        }
        canvas.update_node(job["node_id"], status="done")
        await registry.canvas_repository.save(canvas)
        await _register_media_asset(
            canvas=canvas.name,
            node_id=job["node_id"],
            media_type=port,
            path=media.path,
            content_type=media.mime,
            provider=media.provider,
            generation_id=job["generation_id"],
            operation_id=operation_seen.get("id") or resume_operation_id or "",
        )
        done += 1
        succeeded = {
            "node_id": job["node_id"], "label": job["label"], "ok": True,
            "path": media.path, "provider": media.provider, "generation_id": job["generation_id"],
            "operation_id": operation_seen.get("id") or resume_operation_id or None,
        }
        await registry.invocations.complete(invocation.invocation_id, succeeded)
        results.append({**succeeded, "invocation_id": invocation.invocation_id})

    total = len(payload["jobs"])
    production_run_id = str(payload.get("production_run_id") or "") or None
    passed = total > 0 and done == total
    await _update_production_stage(
        production_run_id,
        canvas.name,
        "generating",
        "succeeded" if passed else "failed",
        "waiting_composition" if passed else "limited",
        problems=[] if passed else [item.get("error", "媒体生成未完成") for item in results if not item.get("ok")],
        artifact_ids=[item["node_id"] for item in results if item.get("ok")],
    )
    if passed:
        await _update_production_stage(
            production_run_id,
            canvas.name,
            "waiting_composition",
            "waiting_composition",
            "waiting_composition",
            artifact_ids=[item["node_id"] for item in results if item.get("ok")],
        )
    return {
        "ok": passed,
        "canvas": canvas.name,
        "provider": payload["provider"],
        "model": payload["requested_model"],
        "total": total,
        "done": done,
        "results": results,
    }


@app.get("/api/media/jobs/{job_id}")
async def media_job_status(job_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"media_[a-f0-9]{32}", job_id or ""):
        raise HTTPException(status_code=400, detail="媒体任务 ID 无效")
    job = await media_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="媒体任务不存在或已过期")
    if job.status in {"failed", "unknown"}:
        try:
            payload = json.loads(job.payload_json)
            production_run_id = str(payload.get("production_run_id") or "") or None
            stage = "composing" if payload.get("job_kind") == "composition" else "generating"
            await _update_production_stage(
                production_run_id,
                job.canvas,
                stage,
                "failed" if job.status == "failed" else "unknown",
                "limited",
                problems=[job.error or f"后台任务进入 {job.status}"],
            )
        except (ValueError, RuntimeError, ProductionRunConflict):
            # 查询媒体任务不能因为附属进度同步失败而变成 500；状态仍可从任务本身读取。
            pass
    return {"ok": True, **job.public()}


@app.post("/api/media/jobs/{job_id}/reconcile")
async def reconcile_media_job(job_id: str) -> dict[str, Any]:
    """人工确认后恢复 unknown 任务；有远端 operation_id 时只查询，不重复创建。"""
    if not re.fullmatch(r"media_[a-f0-9]{32}", job_id or ""):
        raise HTTPException(status_code=400, detail="媒体任务 ID 无效")
    task = await media_jobs.get(job_id)
    if task is None:
        raise HTTPException(status_code=404, detail="媒体任务不存在或已过期")
    if task.status != "unknown":
        raise HTTPException(status_code=409, detail="只有 unknown 任务需要人工恢复")
    payload = json.loads(task.payload_json)
    for item in payload.get("jobs", []):
        invocation_key = canonical_hash({"approval_id": task.approval_id, "node_id": item["node_id"]})
        invocation = await registry.invocations.get(f"inv_{invocation_key[:32]}")
        if invocation and invocation.status in {"executing", "unknown"} and not invocation.remote_operation_id:
            raise HTTPException(
                status_code=409,
                detail=f"节点 {item['label']} 的远端 operation_id 未保存，无法安全恢复，请到 Provider 控制台核对",
            )
    try:
        queued = await media_jobs.requeue_unknown(job_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "requeued": True, **queued.public()}




@app.get("/api/media/providers")
async def media_providers() -> dict[str, Any]:
    """只返回安全的能力元数据，绝不返回 API Key。"""
    return {"providers": default_registry.describe(), "active": default_registry.active}


@app.get("/api/canvas/generation-plan")
async def generation_plan(name: str = "main") -> dict[str, Any]:
    canvas = await registry.canvas_repository.load(name)
    jobs = canvas.generation_jobs()
    return {
        "canvas": canvas.name,
        "jobs": [
            {key: value for key, value in job.items() if key != "prompt"}
            for job in jobs
        ],
        "ready": sum(1 for job in jobs if job["ready"]),
        "total": len(jobs),
        "providers": default_registry.describe(),
    }


@app.get("/api/canvas/composition-plan")
async def composition_plan(name: str = "main") -> dict[str, Any]:
    canvas = await registry.canvas_repository.load(name)
    jobs = canvas.timeline_jobs()
    return {
        "canvas": canvas.name,
        "jobs": jobs,
        "ready": sum(1 for job in jobs if job["ready"]),
        "total": len(jobs),
        "ffmpeg": ffmpeg_capabilities(),
        "tts_providers": tts_registry.describe(),
    }


@app.get("/api/canvases")
async def list_canvases() -> dict[str, Any]:
    """工作区里有哪些画布（前端下拉切换用）。"""
    names = await registry.canvas_repository.list()
    metadata = {}
    for name in names:
        canvas = await registry.canvas_repository.load(name)
        metadata[name] = canvas.meta
    return {"canvases": names, "meta": metadata}


@app.get("/api/canvas")
async def get_canvas(name: str = "main") -> dict[str, Any]:
    """画布数据（前端只负责画，拓扑序与体检由服务端算好）。"""
    canvas = await registry.canvas_repository.load(name)
    order: list[str] = []
    problems: list[str] = []
    try:
        order = canvas.topo_order()
    except CanvasError as exc:
        problems.append(str(exc))
    problems += canvas.validate()
    node_payloads = []
    for node in canvas.nodes.values():
        payload = node.to_dict()
        # 卡片和详情面板需要能阅读/滚动完整正文；JSON 文件仍只保存 content_path，
        # API 展示时再读取，避免画布文件重复存一份长文本。
        payload["content"] = canvas.read_text(node.id) if node.data.get("content_path") else ""
        node_payloads.append(payload)
    return {
        "name": canvas.name,
        "meta": canvas.meta,
        "exists": canvas.persisted,
        "revision": canvas.revision,
        "nodes": node_payloads,
        "edges": [e.to_dict() for e in canvas.edges],
        "viewport": canvas.viewport,
        "topo_order": order,
        "problems": sorted(set(problems)),
        "node_spec": NODE_SPEC,  # 端口定义给前端算连线锚点用（前端不重复实现一份）
    }


@app.post("/api/canvas/nodes/{node_id}/position")
async def move_node(node_id: str, req: MoveNodeRequest) -> dict[str, Any]:
    """前端拖动节点后回写位置（画布状态在服务端，所以必须落盘）。"""
    canvas = await registry.canvas_repository.load(req.canvas)
    _require_revision(canvas, req.revision)
    try:
        canvas.update_node(node_id, position={"x": req.x, "y": req.y})
    except CanvasError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    await registry.canvas_repository.save(canvas)
    return {"ok": True, "node_id": node_id, "position": {"x": req.x, "y": req.y}, "revision": canvas.revision}


@app.post("/api/canvas/nodes/{node_id}")
async def update_canvas_node(node_id: str, req: UpdateCanvasNodeRequest) -> dict[str, Any]:
    """Edit safe node metadata with the same optimistic lock as drag/connect."""
    canvas = await registry.canvas_repository.load(req.canvas)
    _require_revision(canvas, req.revision)
    try:
        node = canvas.update_node(node_id, label=req.label, status=req.status, data=req.data)
    except CanvasError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await registry.canvas_repository.save(canvas)
    return {"ok": True, "canvas": canvas.name, "node": node.to_dict(), "revision": canvas.revision}


@app.post("/api/canvas/nodes")
async def create_canvas_node(req: CreateCanvasNodeRequest) -> dict[str, Any]:
    """在画布指定坐标创建卡片（供空白处右键菜单使用）。"""
    board = await registry.canvas_repository.load(req.canvas)
    _require_revision(board, req.revision)
    try:
        node = board.add_node(req.type, req.label, position={"x": req.x, "y": req.y}, data=req.data)
    except CanvasError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await registry.canvas_repository.save(board)
    return {"ok": True, "canvas": board.name, "node_id": node.id, "node": node.to_dict(), "revision": board.revision}


@app.post("/api/canvas/upload-media")
async def upload_canvas_media(
    request: Request,
    canvas: str = "main",
    kind: Literal["image", "video", "audio"] = "image",
    filename: str = "media",
    node_id: str = "",
    x: float | None = None,
    y: float | None = None,
    revision: int | None = None,
) -> dict[str, Any]:
    """上传本地图片/视频，创建或替换 ToonFlow 风格的独立媒体节点。

    使用原始请求体而不是 multipart：少一个运行依赖，前端仍可直接发送 File。
    """
    max_bytes = 100 * 1024 * 1024
    declared_size = int(request.headers.get("content-length") or 0)
    if declared_size > max_bytes:
        raise HTTPException(status_code=413, detail="文件不能超过 100 MB")
    content = await request.body()
    if not content:
        raise HTTPException(status_code=400, detail="文件不能为空")
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail="文件不能超过 100 MB")

    allowed = {
        "image": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"},
        "video": {".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"},
        "audio": {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"},
    }
    original = Path(filename).name
    suffix = Path(original).suffix.lower()
    if suffix not in allowed[kind]:
        kind_label = {"image": "图片", "video": "视频", "audio": "音频"}[kind]
        raise HTTPException(status_code=415, detail=f"不支持的{kind_label}格式：{suffix or '无扩展名'}")
    mime = (request.headers.get("content-type") or "").split(";", 1)[0].lower()
    if mime in ("", "application/octet-stream"):
        mime = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
            ".gif": "image/gif", ".bmp": "image/bmp", ".avif": "image/avif", ".mp4": "video/mp4",
            ".webm": "video/webm", ".mov": "video/quicktime", ".m4v": "video/x-m4v",
            ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
            ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".aac": "audio/aac",
            ".ogg": "audio/ogg", ".flac": "audio/flac",
        }.get(suffix, "application/octet-stream")
    if not mime.startswith(f"{kind}/"):
        kind_label = {"image": "图片", "video": "视频", "audio": "音频"}[kind]
        raise HTTPException(status_code=415, detail=f"请选择{kind_label}文件")

    board = await registry.canvas_repository.load(canvas)
    _require_revision(board, revision)
    if node_id:
        try:
            node = board.require(node_id)
        except CanvasError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if node.type != kind:
            raise HTTPException(status_code=400, detail=f"节点「{node.label}」不是{kind}素材节点")
    else:
        title = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff._ -]+", "", Path(original).stem).strip()[:60] or kind
        position = {"x": x, "y": y} if x is not None and y is not None else None
        kind_label = {"image": "图片", "video": "视频", "audio": "音频"}[kind]
        node = board.add_node(kind, f"上传{kind_label}·{title}", position=position)

    safe_stem = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff_-]+", "-", Path(original).stem).strip("-")[:48] or kind
    relative = f"assets/uploads/{node.id}/{safe_stem}-{uuid.uuid4().hex[:8]}{suffix}"
    target = WORKSPACE / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(content)
    os.replace(temporary, target)

    port = kind
    data_type = kind.upper()
    node.outputs[port] = {
        "data_type": data_type,
        "value": {"path": relative, "mime_type": mime, "original_name": original, "uploaded": True},
    }
    node.data.update({"filename": original, "mime_type": mime, "size": len(content)})
    node.status = "ready"
    await registry.canvas_repository.save(board)
    await _register_media_asset(
        canvas=board.name,
        node_id=node.id,
        media_type=kind,
        path=relative,
        content_type=mime,
        provider="upload",
    )
    return {
        "ok": True,
        "canvas": board.name,
        "node_id": node.id,
        "kind": kind,
        "path": relative,
        "bytes": len(content),
        "replaced": bool(node_id),
        "revision": board.revision,
    }


@app.post("/api/canvas/export")
async def export_canvas(req: ExportCanvasRequest) -> dict[str, Any]:
    """导出全片提示词：{{ref N}} 展开成参考声明，按片段号排列，落盘并返回全文。"""
    canvas = await registry.canvas_repository.load(req.canvas)
    try:
        markdown = canvas.render_all()
    except CanvasError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    relative = req.path or f"assets/export/{canvas.name}-prompts.md"
    target = WORKSPACE / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")
    segments = len([n for n in canvas.nodes.values() if n.type == "prompt"])
    return {"ok": True, "path": relative, "segments": segments, "chars": len(markdown), "markdown": markdown}


@app.get("/api/sessions/{session_id}/subagents")
async def subagents(session_id: str) -> dict[str, Any]:
    """子 Agent 视图。进程重启后从父会话 JSONL 恢复，未跑完的会带 interrupted 标记。"""
    try:
        return {"session_id": session_id, "sub_agents": registry.subagents(session_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/skills")
async def skills() -> dict[str, Any]:
    """技能清单（常驻上下文）+ 正文体量（按需加载的部分），顺带给出省了多少。"""
    entries = registry.skills
    section = skills_prompt_section(entries)
    body_chars = sum(len(read_skill_file(e.name, "SKILL.md")) for e in entries)
    return {
        "skills": [
            {"name": e.name, "description": e.description, "location": e.location, "body_chars": e.body_chars}
            for e in entries
        ],
        "errors": registry.skill_errors,
        "prompt_chars": len(section),
        "body_chars": body_chars,
        "prompt_section": section,
    }


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """顺带报出实际在用的模型：调试"思考块为什么不出现"这类问题时，
    第一件要看的就是"服务到底连的哪个模型"（曾经因为进程没吃到环境变量而白查一轮）。"""
    settings = Settings.load()
    return {
        "ok": True,
        "sessions": len(registry.sessions),
        # 两个都报：`model` 是"当前环境想要什么"，`session_model` 是"会话实例实际绑的是什么"。
        # 这俩不一致时（进程启动早于环境变量、或实例被缓存）一眼就能看出来。
        "model": settings.model,
        "session_model": registry.llm.model,
        "base_url": settings.base_url,
        "event_store": type(registry.event_store).__name__,
        "run_state_store": type(registry.run_states).__name__,
        "question_store": type(registry.question_states).__name__,
        "invocation_store": type(registry.invocations).__name__,
        "approval_store": type(registry.approvals).__name__,
        "canvas_repository": type(registry.canvas_repository).__name__,
        "session_repository": type(registry.session_repository).__name__,
        "production_run_repository": type(registry.production_runs).__name__,
        "outbox_publisher": registry.outbox_publisher.stats() if registry.outbox_publisher else None,
        "media_job_store": type(media_jobs).__name__,
        "media_asset_repository": type(registry.media_assets).__name__,
        "media_worker": media_worker.worker_id if media_worker else None,
        "ffmpeg": ffmpeg_capabilities(),
    }


# 产物目录：前端用 <img src="/assets/generated/xxx.png"> 直接显示生成的图
(WORKSPACE / "assets").mkdir(parents=True, exist_ok=True)
app.mount("/assets", StaticFiles(directory=str(WORKSPACE / "assets")), name="assets")
# 前端产物：优先挂 Vue 版（web-vue/dist），旧版挂在 /legacy 作为退路。
# 这样"新前端"是默认入口，出问题还能回退，不需要改代码之外的东西。
VUE_DIST = ROOT / "web-vue" / "dist"
if VUE_DIST.exists():
    app.mount("/legacy", StaticFiles(directory=str(WEB_DIR), html=True), name="legacy")
    app.mount("/", StaticFiles(directory=str(VUE_DIST), html=True), name="vue")
else:
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
