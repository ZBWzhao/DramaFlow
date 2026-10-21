"""自研工具插件：story_analysis —— 聊天里修改剧本理解四件套。

对照设计 docs/superpowers/specs/2026-10-21-script-asset-pipeline-design.md §3.4：
结构化产物走按钮触发的 pipeline（确定性链），自由修改走聊天。
本工具只改被指名的字段；写入前过 StoryAnalysis schema，校验不过不落盘——
不让聊天里的一句口误污染 pipeline 产物。
"""
from __future__ import annotations

import json
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.context import ToolContext
from core.plugin import ToolResult, define_tool, tool_result
from core.story_analysis import StoryAnalysis

TOOL_META = {
    "name": "story_analysis",
    "display_name": "剧本理解产物",
    "description": "读取/更新项目的剧本理解四件套（世界观/梗概/分集大纲/视听风格）",
    "version": "1.0.0",
    "author": "toonflow-py-lab",
    "prompt": (
        "update_story_analysis 只改被指名的字段，其余字段原样保留；"
        "episode_outline 与 audiovisual_style 传 JSON 字符串（或已解析的数组/对象）。"
        "改完复述改动点，不要声称重新生成了整个分析。"
    ),
}


class UpdateArgs(BaseModel):
    field: Literal["worldview", "synopsis", "episode_outline", "audiovisual_style"]
    value: str | list | dict = Field(description="worldview/synopsis 为字符串；其余为 JSON 数组或对象")


async def _update(args: UpdateArgs, context: ToolContext) -> ToolResult:
    repo = context.require_canvas_repository()
    canvas_name = str(context.config.get("default_canvas") or "main")
    canvas = await repo.load(canvas_name)
    meta = dict(canvas.meta or {})
    analysis = dict(meta.get("story_analysis") or {})

    value = args.value
    if isinstance(value, str) and args.field in {"episode_outline", "audiovisual_style"}:
        value = json.loads(value)  # 解析失败直接抛，agent 层把错误回给模型

    StoryAnalysis(**{**analysis, args.field: value})  # 过 schema 才许写
    analysis[args.field] = value
    analysis["updated_at"] = int(time.time())
    meta["story_analysis"] = analysis
    canvas.meta = meta
    await repo.save(canvas)
    return tool_result(f"已更新剧本理解「{args.field}」。", field=args.field, revision=canvas.revision)


def build_tools(_ctx: ToolContext) -> list[Any]:
    return [
        define_tool(
            name="update_story_analysis",
            description=(
                "更新当前项目的剧本理解产物（worldview/synopsis/episode_outline/audiovisual_style）。"
                "只改指名字段；episode_outline 与 audiovisual_style 需要合法 JSON。"
            ),
            args_model=UpdateArgs,
            execute=_update,
            prompt_guidelines=[
                "用户要求「把世界观改成…」这类结构性产物修改时用本工具，改完只复述改动点。",
                "不要用本工具重写与请求无关的字段。",
            ],
            side_effect=True,
        )
    ]


class Plugin:
    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return dict(config or {})

    def create_tools(self, context: ToolContext) -> list[Any]:
        return build_tools(context)
