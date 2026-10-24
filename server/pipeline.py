"""管线产物导入·查询·导出路由（/api/pipeline）。

路由契约（阶段 1 Task 3 dispatch 定稿，Task 4-8 依赖）：
- POST /projects/{canvas}/artifacts/{artifact_type}  导入：prepare 合并整形后落库
- GET  /projects/{canvas}/artifacts                  列表（不含 content）
- GET  /projects/{canvas}/artifacts/{type}           单条（含 content / source_run_id）
- GET  /projects/{canvas}/artifacts/{type}/export    附件导出（文件名 = 注册表 filename）

schema_version 的唯一写者在本路由：请求体显式给出（非空串）优先，否则从产出技能
pipeline_skills/<skill>/SKILL.md frontmatter 的 `version:` 行推导（进程内缓存）；
两者都拿不到记空串并附告警 finding。DB 句柄取自 registry.canvas_repository
（Postgres 后端才有 `.database`）；file 后端无 DB，路由一律 503。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core import pipeline_artifacts, pipeline_import, pipeline_repository
from core.pipeline_artifacts import ArtifactType
from core.pipeline_import import PipelineImportError

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

# 由 server/app.py 在装配阶段注入，避免反向 import app 造成循环依赖
# （照 server/studio.py 的 configure 模式）。PostgresDatabase | None。
_database: Any = None

NO_DB_MESSAGE = "pipeline artifacts 需要 Postgres（compose.postgres.yml）"

_SKILLS_ROOT = Path(__file__).resolve().parent.parent / "pipeline_skills"
# frontmatter version 进程内缓存：技能文件运行期不变，含不可读的空串结果也缓存，
# 避免每次导入重复读盘。清空即失效（仅测试需要）。
_VERSION_CACHE: dict[str, str] = {}

# version: 可在 frontmatter 顶层，也可缩进在 metadata: 下，故匹配任意缩进。
_VERSION_LINE = re.compile(r"^\s*version:\s*(\S+)\s*$", re.MULTILINE)


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


async def ensure_started() -> bool:
    """惰性启动共享池；无 DB 配置或启动失败返回 False。

    asyncpg pool 绑定事件循环，而本模块在 import 阶段装配、首个请求才真正建池，
    此时已在请求所在的服务循环里，TestClient 场景同样成立。
    """
    db = _database
    if db is None:
        return False
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


def _resolve_schema_version(art: ArtifactType, requested: str) -> tuple[str, list[str]]:
    """显式非空优先；否则 frontmatter `version:` 推导；不可读 → ("", 告警 finding)。"""
    explicit = (requested or "").strip()
    if explicit:
        return explicit, []
    version = _VERSION_CACHE.get(art.skill)
    if version is None:
        version = ""
        try:
            text = (_SKILLS_ROOT / art.skill / "SKILL.md").read_text(encoding="utf-8")
            frontmatter = text.split("---", 2)[1] if text.startswith("---") else ""
            match = _VERSION_LINE.search(frontmatter)
            if match:
                version = match.group(1)
        except (OSError, IndexError):
            pass
        _VERSION_CACHE[art.skill] = version
    if version:
        return version, []
    return "", [
        f"{art.name}: 未能从技能 {art.skill}/SKILL.md frontmatter 读取 version，"
        "schema_version 记为空串"
    ]


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
    # source_run_id 只在 list_all 里有（get 的三键返回形状是 Task 2 已锁定的契约）。
    row = _find_row(await pipeline_repository.list_all(db, canvas), artifact_type)
    if row is None:
        return _missing(artifact_type)
    return {
        "artifact_type": artifact_type,
        "schema_version": row["schema_version"],
        "content": row["content"],
        "source_run_id": row["source_run_id"],
        "updated_at": row["updated_at"],
    }


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
