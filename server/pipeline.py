"""管线产物导入·查询·导出·删除路由（/api/pipeline）。

路由契约（阶段 1 Task 3 dispatch 定稿，Task 4-8 依赖）：
- POST /projects/{canvas}/artifacts/{artifact_type}  导入：prepare 合并整形后落库
- GET  /projects/{canvas}/artifacts                  列表（不含 content）
- GET  /projects/{canvas}/artifacts/{type}           单条（含 content / source_run_id；
                                                     script 另附逐集正文视图 episodes[]）
- GET  /projects/{canvas}/artifacts/{type}/export    附件导出（文件名 = 注册表 filename）
- PUT  /projects/{canvas}/artifacts/script/episodes/{no}  集级编辑落库（edited_text）
- DELETE /projects/{canvas}/artifacts/{type}         删除单行产物（阶段 2 Task 1）

schema_version 的唯一写者在本路由：请求体显式给出（非空串）优先，否则从产出技能
pipeline_skills/<skill>/SKILL.md frontmatter 的 `version:` 行推导（进程内缓存，
解析函数在 core.pipeline_artifacts.resolve_schema_version）；
两者都拿不到记空串并附告警 finding。DB 句柄取自 registry.canvas_repository
（Postgres 后端才有 `.database`）；file 后端无 DB，路由一律 503。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core import pipeline_artifacts, pipeline_import, pipeline_repository
from core.pipeline_artifacts import resolve_schema_version
from core.pipeline_import import (
    SCRIPT_EPISODE_FIELD,
    SCRIPT_EPISODES_FIELD,
    PipelineImportError,
    derive_episode_text,
)

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

# 兼容别名：阶段 1 命名保留（测试与潜在调用方按 _resolve_schema_version 引用不破）。
_resolve_schema_version = resolve_schema_version

# 由 server/app.py 在装配阶段注入，避免反向 import app 造成循环依赖
# （照 server/studio.py 的 configure 模式）。PostgresDatabase | None。
_database: Any = None

NO_DB_MESSAGE = "pipeline artifacts 需要 Postgres（compose.postgres.yml）"


class ImportBody(BaseModel):
    content: Any
    source_run_id: str = ""
    schema_version: str = ""


def configure(*, registry: Any = None, database: Any = None) -> None:
    """依赖注入：默认从 registry.canvas_repository 取 Postgres 后端的共享 DB 句柄；
    file 后端没有 `.database` → None（全部路由 503）。测试可显式传 database。"""
    global _database
    _database = database if database is not None else _database_from_registry(registry)


def _database_from_registry(registry: Any) -> Any:
    repo = getattr(registry, "canvas_repository", None)
    return getattr(repo, "database", None)


# 建池锁 + 它所属的事件循环：asyncio.Lock 绑定首个使用它的事件循环，而
# TestClient 每个用例都是新循环，跨循环复用旧锁会 RuntimeError。锁本就只
# 护栏同一循环内的并发首请求，故按运行中的循环重建即可。
_pool_lock: asyncio.Lock | None = None
_pool_lock_loop: asyncio.AbstractEventLoop | None = None


def _pool_lock_for_loop() -> asyncio.Lock:
    global _pool_lock, _pool_lock_loop
    loop = asyncio.get_running_loop()
    if _pool_lock is None or _pool_lock_loop is not loop:
        _pool_lock = asyncio.Lock()
        _pool_lock_loop = loop
    return _pool_lock


async def startup() -> None:
    """lifespan 调用：服务启动时建好管线数据库池（幂等）。

    惰性建池仍保留为兜底，但正常路径不再有"首个请求承担建池"的竞态窗口。
    """
    await ensure_started()


async def ensure_started() -> bool:
    """惰性启动共享池；无 DB 配置或启动失败返回 False。

    asyncpg pool 绑定事件循环，而本模块在 import 阶段装配，TestClient 场景下
    每个用例都是新循环，故建池必须发生在请求所在的服务循环里。正常路径是
    lifespan startup 已建好（pool 就位直接返回）；兜底路径用模块级
    asyncio.Lock 串行化并在锁内双重检查，两个并发首请求不会都见 pool is None
    而重复 start（PostgresDatabase._users 会多计，导致收池时不归零）。
    """
    db = _database
    if db is None:
        return False
    if db.pool is not None:
        return True
    async with _pool_lock_for_loop():
        if db.pool is None:
            await db.start()
    return True


async def _db_or_none() -> Any:
    try:
        return _database if await ensure_started() else None
    except Exception:  # Postgres 已配置但连不上：按"无 DB 可用"降级为 503
        return None


def _no_db() -> JSONResponse:
    return JSONResponse(status_code=503, content={"message": NO_DB_MESSAGE})


def _unknown_type(artifact_type: str) -> JSONResponse:
    return JSONResponse(
        status_code=404, content={"message": f"未知产物类型：{artifact_type}"}
    )


def _missing(artifact_type: str) -> JSONResponse:
    return JSONResponse(
        status_code=404, content={"message": f"产物不存在：{artifact_type}"}
    )


@router.post("/projects/{canvas}/artifacts/{artifact_type}")
async def import_artifact(canvas: str, artifact_type: str, body: ImportBody):
    try:
        art = pipeline_artifacts.get(artifact_type)
    except KeyError:
        return _unknown_type(artifact_type)
    db = await _db_or_none()
    if db is None:
        return _no_db()
    schema_version, findings = _resolve_schema_version(art, body.schema_version)
    existing_row = await pipeline_repository.get(db, canvas, artifact_type)
    try:
        content, merge_findings = pipeline_import.prepare(
            art, body.content, existing_row["content"] if existing_row else None
        )
    except PipelineImportError as exc:
        rejected = exc.to_dict()
        if not rejected["findings"]:
            # prepare 只把「可落库的告警」放 findings，致命结构错误 findings 为空；
            # API 层保证 422 的 findings 非空（前端列表渲染），把拒绝原因并入。
            rejected["findings"] = [rejected["message"]]
        return JSONResponse(status_code=422, content=rejected)
    findings.extend(merge_findings)
    # prepare 返回值可能与请求体共享引用，这里原样交给 save 序列化，绝不就地修改。
    saved = await pipeline_repository.save(
        db, canvas, artifact_type, content,
        source_run_id=body.source_run_id, schema_version=schema_version,
    )
    return {
        "artifact_type": artifact_type,
        "schema_version": saved["schema_version"],
        "updated_at": saved["updated_at"],
        "findings": findings,
    }


@router.get("/projects/{canvas}/artifacts")
async def list_artifacts(canvas: str):
    db = await _db_or_none()
    if db is None:
        return _no_db()
    rows = await pipeline_repository.list_all(db, canvas)
    return {
        "artifacts": [
            {
                "artifact_type": row["artifact_type"],
                "schema_version": row["schema_version"],
                "findings": row["findings"],  # 0008：分级门标记（Task 4 面板渲染）
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]
    }


def _find_row(rows: list[dict], artifact_type: str) -> dict | None:
    return next((row for row in rows if row["artifact_type"] == artifact_type), None)


@router.get("/projects/{canvas}/artifacts/{artifact_type}")
async def read_artifact(canvas: str, artifact_type: str):
    db = await _db_or_none()
    if db is None:
        return _no_db()
    # source_run_id 只在 list_all 里有（get 返回形状随任务演进加键，加法契约——0008 起 findings 也在）。
    row = _find_row(await pipeline_repository.list_all(db, canvas), artifact_type)
    if row is None:
        return _missing(artifact_type)
    body = {
        "artifact_type": artifact_type,
        "schema_version": row["schema_version"],
        "content": row["content"],
        "source_run_id": row["source_run_id"],
        "findings": row["findings"],  # 0008：分级门标记（Task 4 面板渲染）
        "updated_at": row["updated_at"],
    }
    if artifact_type == "script":
        # Task 5：剧本详情附带逐集正文视图。text 只是响应字段，不落库——
        # content 保持契约原样（集对象上允许出现的唯一附加键是集级编辑的 edited_text）。
        body["episodes"] = _script_episode_views(row["content"])
    return body


def _script_episode_views(content: Any) -> list[dict]:
    """剧本详情响应的逐集正文视图：[{ep, hook, chars, edited, text}]。

    text = edited_text（集级编辑落库的本地改稿）优先，否则按 flow 节拍确定性推导
    （pipeline_import.derive_episode_text）；edited 标记是否处于本地改稿状态。
    content 不是可识别的剧本形状时返回空表（详情主体照常下发，前端按空视图回退）。
    """
    episodes = content.get(SCRIPT_EPISODES_FIELD) if isinstance(content, dict) else None
    if not isinstance(episodes, list):
        return []
    views: list[dict] = []
    for episode in episodes:
        if not isinstance(episode, dict):
            continue
        edited = episode.get("edited_text")
        has_edit = isinstance(edited, str) and bool(edited)
        text = edited if has_edit else derive_episode_text(episode)
        views.append(
            {
                SCRIPT_EPISODE_FIELD: episode.get(SCRIPT_EPISODE_FIELD),
                "hook": episode.get("hook") or "",
                "chars": len(text),
                "edited": has_edit,
                "text": text,
            }
        )
    return views


class EpisodeTextBody(BaseModel):
    text: str


@router.put("/projects/{canvas}/artifacts/script/episodes/{no}")
async def save_episode_text(canvas: str, no: int, body: EpisodeTextBody):
    """集级编辑落库（Task 5）：body {"text"} 写入该集对象的 edited_text 后整行复检保存。

    上游重导入覆盖本地编辑：edited_text 挂在集对象上，该集被上游批次重导入时
    被整对象替换（fresh upstream content），edited_text 随之消失；批次外的集
    原样保留（本地编辑还在）——合并语义全在 pipeline_import.prepare，本路由不改。
    """
    db = await _db_or_none()
    if db is None:
        return _no_db()
    row = _find_row(await pipeline_repository.list_all(db, canvas), "script")
    if row is None:
        return _missing("script")
    content = row["content"]  # list_all 每次现查现解码，就地改不污染任何缓存
    episodes = content.get(SCRIPT_EPISODES_FIELD) if isinstance(content, dict) else None
    if not isinstance(episodes, list):
        message = "script: 库内产物无法识别集数组，无法按集保存"
        return JSONResponse(status_code=422, content={"message": message, "findings": [message]})
    target = next(
        (
            e for e in episodes
            if isinstance(e, dict) and e.get(SCRIPT_EPISODE_FIELD) == no
        ),
        None,
    )
    if target is None:
        return JSONResponse(status_code=404, content={"message": f"集不存在：{no}"})
    target["edited_text"] = body.text
    try:
        # prepare 复检：与导入同一整形入口，保证落库行始终是可落库的契约形状
        merged, _findings = pipeline_import.prepare(
            pipeline_artifacts.get("script"), content, None
        )
    except PipelineImportError as exc:
        rejected = exc.to_dict()
        if not rejected["findings"]:  # 同导入路由：422 的 findings 保证非空
            rejected["findings"] = [rejected["message"]]
        return JSONResponse(status_code=422, content=rejected)
    await pipeline_repository.save(
        db, canvas, "script", merged,
        source_run_id=row["source_run_id"], schema_version=row["schema_version"],
    )
    return await read_artifact(canvas, "script")


@router.get("/projects/{canvas}/artifacts/{artifact_type}/export")
async def export_artifact(canvas: str, artifact_type: str):
    try:
        art = pipeline_artifacts.get(artifact_type)
    except KeyError:
        return _unknown_type(artifact_type)
    db = await _db_or_none()
    if db is None:
        return _no_db()
    row = _find_row(await pipeline_repository.list_all(db, canvas), artifact_type)
    if row is None:
        return _missing(artifact_type)
    return JSONResponse(
        content=row["content"],
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename={art.filename}"},
    )


@router.delete("/projects/{canvas}/artifacts/{artifact_type}")
async def delete_artifact(canvas: str, artifact_type: str):
    """删除单行产物（阶段 2 Task 1）：管线重跑前清槽用，repo 层删不到也返回成功。"""
    try:
        art = pipeline_artifacts.get(artifact_type)  # 未知类型沿用 _unknown_type 404
    except KeyError:
        return _unknown_type(artifact_type)
    db = await _db_or_none()
    if db is None:
        return _no_db()
    await pipeline_repository.delete(db, canvas, art.name)
    return {"message": f"已删除 {canvas}/{art.name}"}
