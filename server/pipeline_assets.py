"""管线产物 → 资产库 同步桥（阶段 2 Task 7：三套脑切换）。

分层裁决（spec 8.2，已定，不在本模块重议）：内容生产 = shuohao pipeline
（四卡退役）；资产沉淀 = studio 资产库语义——「表是草稿、库是资产，
只增不改不删」。本模块是两者之间唯一的桥：

    POST /projects/{canvas}/artifacts/characters/sync-assets   cast.json 入库
    POST /projects/{canvas}/artifacts/art/sync-assets          art.json 入库

响应契约 {"message": "...", "added": n}。读侧是管线产物 DB（Postgres 后端才有；
file 后端 503，沿用 server/pipeline.py 的 {"message"} 形态与 404 文案）；写侧是
画布资产库——merge（core.asset_tables.merge_asset_tables，只增不改不删）之后，
复用制片台抽出的执行函数 studio._apply_asset_sync_plan，走同一条
mutate_canvas_with_retry 串行写路径，不新增未串行化的写路径。

映射取值路径以 docs/pipeline/data-contract.md 为准校正（Task 7 brief 的示意
代码是按 outline.json 的角色形状 / 臆测的 scene["style"] 写的）：
- characters（cast.json）：description ← persona.identity（缺则 oneLiner），
  bio ← persona.appearance（外形小传，语义对齐 CharacterRow；叙事 arc 不入 bio）；
- art：scenes[] → 场景行（description ← summary 设计意图）、
  props[] → 道具行（description ← summary 戏剧功能）。art.json 没有 style 键，
  不产 look 行——look 属于角色形象，角色侧行模型不含出处时留空。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.asset_tables import AssetTables, CharacterRow, merge_asset_tables
from core.canvas import Canvas
from core.canvas_repository import mutate_canvas_with_retry
from core import pipeline_repository

router = APIRouter(prefix="/api/pipeline", tags=["pipeline-assets"])

# 本桥可入库的产物类型：角色设定卡 / 美术设定集（数据契约的注册名）。
SYNCABLE_TYPES = ("characters", "art")

_NOT_CONFIGURED_MESSAGE = "资产库（制片台适配层）尚未装配"


def _clip(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def characters_to_rows(content: dict) -> list[dict]:
    """cast.json → CharacterRow 形状的行（name/description/bio）。

    description 截 500、bio 截 1500：与 CharacterRow 的 max_length 一致，
    让校验在写画布之前就先过。无 name 的卡跳过；name 超 60 截断（同名归一
    交给 merge_asset_tables，只增语义不受影响）。
    """
    rows: list[dict] = []
    for card in content.get("characters", []) or []:
        if not isinstance(card, dict):
            continue
        name = str(card.get("name") or "").strip()[:60]
        if not name:
            continue
        persona = card.get("persona") if isinstance(card.get("persona"), dict) else {}
        description = str(persona.get("identity") or card.get("oneLiner") or "").strip()
        rows.append({
            "name": name,
            "description": _clip(description, 500),
            "bio": _clip(str(persona.get("appearance") or "").strip(), 1500),
        })
    return rows


def art_to_rows(content: dict) -> dict[str, list[dict]]:
    """art.json → 场景/道具行（merge_asset_tables 的 scenes/props 消费形状）。

    name 截 60、description 截 500：与 characters_to_rows 同一套上限（60/500/1500
    的前两档；场景/道具行没有 bio 栏，1500 档用不上），让校验在写画布之前就先过，
    避免超长 summary 把整次同步卡在校验上。
    """
    def _plain(items: Any) -> list[dict]:
        rows = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()[:60]
            if not name:
                continue
            rows.append({"name": name,
                         "description": _clip(str(item.get("summary") or "").strip(), 500)})
        return rows

    return {
        "scenes": _plain(content.get("scenes")),
        "props": _plain(content.get("props")),
    }


def _tables_from_content(content: Any, artifact_type: str) -> AssetTables:
    """产物 content → AssetTables；content 不是对象或行全空时是空表（added=0）。"""
    if not isinstance(content, dict):
        return AssetTables()
    if artifact_type == "characters":
        return AssetTables(characters=[CharacterRow(**row) for row in characters_to_rows(content)])
    rows = art_to_rows(content)
    return AssetTables(scenes=rows["scenes"], props=rows["props"])


@router.post("/projects/{canvas}/artifacts/{artifact_type}/sync-assets")
async def sync_artifact_assets(canvas: str, artifact_type: str) -> dict:
    """管线产物 → 资产库：读产物 → 组表 → merge（只增不改不删）→ 复用 studio 落库。"""
    if artifact_type not in SYNCABLE_TYPES:
        return JSONResponse(status_code=404, content={"message": f"未知产物类型：{artifact_type}"})

    # 读侧依赖（管线 DB）与 404/503 形态全部沿用 server/pipeline.py。
    from server import pipeline as pipeline_api

    db = await pipeline_api._db_or_none()
    if db is None:
        return pipeline_api._no_db()
    row = pipeline_api._find_row(await pipeline_repository.list_all(db, canvas), artifact_type)
    if row is None:
        return pipeline_api._missing(artifact_type)

    # 写侧依赖（资产库）：制片台适配层未装配时按 503 拒绝（沿用 {"message"} 形态）。
    from server import studio as studio_api

    if studio_api._registry is None:
        return JSONResponse(status_code=503, content={"message": _NOT_CONFIGURED_MESSAGE})
    tables = _tables_from_content(row["content"], artifact_type)
    total = len(tables.characters) + len(tables.scenes) + len(tables.props)
    if not total:
        return {"message": f"「{artifact_type}」产物里没有可入库的行", "added": 0}
    await studio_api._load(canvas)  # 画布不存在 → 404（沿用制片台 _load 形态）

    label = {"characters": "角色设定", "art": "美术设定"}[artifact_type]
    graph = await studio_api._load(canvas)
    if not getattr(graph, "persisted", False):
        # file 后端的 load 对不存在的画布返回空白未持久化画布——这里必须显式挡下，
        # 否则同步会把一个幽灵画布文件落盘（任一环节失败不产生脏数据）。
        return JSONResponse(status_code=404, content={"message": f"画布不存在：{canvas}"})
    counts: dict[str, int] = {}

    def mutate(graph: Canvas) -> bool:
        # plan 在 mutate 内对最新画布现算：merge 是纯函数，读-合-写因此原子，
        # 并发写入不会按过期投影建出重复资产。
        plan = merge_asset_tables(studio_api._existing_assets_projection(graph), tables)
        counts.update(studio_api._apply_asset_sync_plan(graph, plan))
        return True

    await mutate_canvas_with_retry(studio_api._registry.canvas_repository, canvas, mutate)
    created = counts.get("created_assets", 0)
    looks = counts.get("created_looks", 0)
    added = created + looks
    message = (
        f"已同步「{label}」到资产库：新增资产 {created}、新增形象 {looks}；已有资产未改动"
        if added
        else f"「{label}」与资产库已一致，无需新增"
    )
    return {"message": message, "added": added}
