"""自研工具插件：asset_tables —— 聊天里改资产表 / 触发解析 pipeline。

对照设计 docs/superpowers/specs/2026-10-21-script-asset-pipeline-design.md §4：
表是草稿区，聊天是「自由修改」入口——模型逐条改表（每次一个 op，schema 校验），
批量抽取交给按钮或 run_pipeline（确定性链），模型绝不手写整张表。
行匹配一律走 normalize_name：模型写「林小满」、表里存「林 小满」也能命中。
"""
from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.asset_tables import AssetTables, normalize_name
from core.context import ToolContext
from core.pipelines import spawn
from core.plugin import ToolResult, define_tool, tool_result

TOOL_META = {
    "name": "asset_tables",
    "display_name": "资产表",
    "description": "读取/编辑项目的资产三表（角色/场景/道具草稿），并可靠地触发剧本解析",
    "version": "1.0.0",
    "author": "toonflow-py-lab",
    "prompt": (
        "改资产表一律用 update_asset_tables，一次改一条（op 指明动作），改完复述改动；"
        "用户要「解析出资产表 / 重新解析 / 分析剧本」时用 run_pipeline 触发后台解析——"
        "那比你自己逐条写表又快又稳。表格改完后前端会自动刷新，不要在回复里整表复述。"
    ),
}


class LookArg(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    prompt: str = Field(default="", max_length=2000)


class UpdateArgs(BaseModel):
    op: Literal[
        "set_character",   # 改角色 description/bio（field+value）
        "add_character",   # 新增角色（可带 looks）
        "remove_character",
        "add_look",        # 给角色加形象
        "remove_look",
        "set_scene",
        "add_scene",
        "remove_scene",
        "set_prop",
        "add_prop",
        "remove_prop",
    ]
    name: str = Field(min_length=1, max_length=60, description="行名（角色/场景/道具名），按归一化匹配")
    field: Literal["description", "bio"] | None = None
    value: str = Field(default="", max_length=2000, description="set_* 时的新文本")
    looks: list[LookArg] = Field(default_factory=list, description="add_character 时可带初始形象")
    look: LookArg | None = None


class PipelineArgs(BaseModel):
    pipeline: Literal["story_analysis", "asset_parse"]


async def _save_tables(context: ToolContext, canvas: Any, tables: dict) -> None:
    meta = dict(canvas.meta or {})
    meta["asset_tables"] = AssetTables(**tables).model_dump()
    canvas.meta = meta
    await context.require_canvas_repository().save(canvas)


def _find(rows: list[dict], name: str) -> dict | None:
    key = normalize_name(name)
    return next((r for r in rows if normalize_name(str(r.get("name") or "")) == key), None)


async def _update(args: UpdateArgs, context: ToolContext) -> ToolResult:
    repo = context.require_canvas_repository()
    canvas_name = str(context.config.get("default_canvas") or "main")
    canvas = await repo.load(canvas_name)
    meta = dict(canvas.meta or {})
    tables = dict(meta.get("asset_tables") or {})
    tables.setdefault("characters", [])
    tables.setdefault("scenes", [])
    tables.setdefault("props", [])

    if args.op.startswith("set_") and not args.value.strip():
        return tool_result(f"op={args.op} 需要提供 value（新文本）。", is_error=True)

    if args.op in ("set_character", "add_character"):
        rows = tables["characters"]
        row = _find(rows, args.name)
        if args.op == "add_character":
            if row is not None:
                return tool_result(f"角色「{args.name}」已存在；要改它请用 set_character。", is_error=True)
            row = {"name": args.name.strip(), "description": "", "bio": "", "looks": []}
            rows.append(row)
        if row is None:
            return tool_result(f"角色「{args.name}」不在表里；先用 add_character 创建。", is_error=True)
        if args.field and args.value.strip():
            row[args.field] = args.value.strip()
        if args.op == "add_character" and args.looks:
            row["looks"] = [{"name": lk.name, "prompt": lk.prompt} for lk in args.looks]

    elif args.op == "remove_character":
        rows = tables["characters"]
        row = _find(rows, args.name)
        if row is None:
            return tool_result(f"角色「{args.name}」不在表里。", is_error=True)
        rows.remove(row)

    elif args.op in ("add_look", "remove_look"):
        row = _find(tables["characters"], args.name)
        if row is None:
            return tool_result(f"角色「{args.name}」不在表里。", is_error=True)
        if args.op == "add_look":
            if args.look is None:
                return tool_result("add_look 需要提供 look（形象名）。", is_error=True)
            looks = row.setdefault("looks", [])
            if _find(looks, args.look.name) is not None:
                return tool_result(f"形象「{args.look.name}」已存在。", is_error=True)
            looks.append({"name": args.look.name.strip(), **({"prompt": args.look.prompt} if args.look.prompt else {})})
        else:
            target = args.look.name if args.look else ""
            looks = row.get("looks") or []
            hit = _find(looks, target)
            if hit is None:
                return tool_result(f"角色「{args.name}」没有形象「{target}」。", is_error=True)
            looks.remove(hit)

    else:  # scene / prop
        kind = "scenes" if "scene" in args.op else "props"
        rows = tables[kind]
        label = "场景" if kind == "scenes" else "道具"
        row = _find(rows, args.name)
        if args.op.startswith("add_"):
            if row is not None:
                return tool_result(f"{label}「{args.name}」已存在；要改它请用对应的 set_ 操作。", is_error=True)
            rows.append({"name": args.name.strip(), "description": args.value.strip()})
        elif args.op.startswith("remove_"):
            if row is None:
                return tool_result(f"{label}「{args.name}」不在表里。", is_error=True)
            rows.remove(row)
        else:
            if row is None:
                return tool_result(f"{label}「{args.name}」不在表里；先用 add_ 创建。", is_error=True)
            if args.value.strip():
                row["description"] = args.value.strip()

    tables["updated_at"] = int(time.time())
    await _save_tables(context, canvas, tables)
    return tool_result(f"已更新资产表（{args.op}：{args.name}）。", op=args.op, name=args.name)


async def _run(args: PipelineArgs, context: ToolContext) -> ToolResult:
    canvas_name = str(context.config.get("default_canvas") or "main")
    result = spawn(canvas_name, args.pipeline)
    if not result.get("ok"):
        return tool_result(result.get("detail", "触发失败"), is_error=True)
    where = "项目设定" if args.pipeline == "story_analysis" else "资产表"
    return tool_result(f"已开始后台解析，进度见「{where}」面板（完成后面板自动刷新）。")


def build_tools(_ctx: ToolContext) -> list[Any]:
    return [
        define_tool(
            name="update_asset_tables",
            description=(
                "编辑当前项目的资产表草稿（角色/场景/道具）：改描述小传、增删行、增删形象。"
                "一次一个操作；批量抽取请用 run_pipeline。"
            ),
            args_model=UpdateArgs,
            execute=_update,
            prompt_guidelines=[
                "用户点名要改某个角色/场景/道具时用本工具，改完只复述改动点。",
                "人物小传（bio）要写可画出的外形事实：年龄段体型/脸型发型/识别特征，不写「美丽」「沧桑」这类词。",
            ],
            side_effect=True,
        ),
        define_tool(
            name="run_pipeline",
            description="触发后台解析 pipeline：story_analysis（剧本理解四卡）或 asset_parse（从剧本抽取资产三表）",
            args_model=PipelineArgs,
            execute=_run,
            prompt_guidelines=[
                "用户说「解析出资产表」「重新解析」「分析剧本」时用本工具，不要自己逐条编表。",
                "触发成功后告诉用户去哪个面板看进度，不要等待或轮询结果。",
            ],
            side_effect=True,
        ),
    ]


class Plugin:
    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return dict(config or {})

    def create_tools(self, context: ToolContext) -> list[Any]:
        return build_tools(context)
