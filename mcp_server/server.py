"""Expose selected DramaFlow canvas capabilities through MCP.

Default transport is stdio. Set DRAMAFLOW_MCP_TRANSPORT=streamable-http to
serve http://127.0.0.1:8765/mcp (host/port are configurable via environment).
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from core.canvas import NODE_SPEC, Canvas, CanvasError, canvas_path

ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = Path(os.getenv("DRAMAFLOW_WORKSPACE", str(ROOT / "workspace"))).resolve()
CANVAS_NAME = re.compile(r"^[A-Za-z0-9_-]{1,120}$")

mcp = MCPServer(
    "DramaFlow",
    description="短剧 Agent 工作台：检查画布、创建故事工程并导出生成提示词。",
    instructions=(
        "先用 list_canvases 查看工程，再用 inspect_canvas 读取结构。"
        "创建新故事前确认用户意图；媒体生成仍由 DramaFlow 主应用执行审批。"
    ),
)


def _safe_name(name: str) -> str:
    value = str(name or "main").strip()
    if not CANVAS_NAME.fullmatch(value):
        raise ValueError("画布名只能包含字母、数字、下划线和短横线，长度 1-120")
    path = canvas_path(WORKSPACE, value).resolve()
    if path.parent != WORKSPACE:
        raise ValueError("画布路径越界")
    return value


def _load(name: str) -> Canvas:
    safe = _safe_name(name)
    path = canvas_path(WORKSPACE, safe)
    if not path.exists():
        raise FileNotFoundError(f"画布不存在：{safe}")
    return Canvas.load(path, safe)


def _canvas_row(path: Path) -> dict[str, Any]:
    canvas = Canvas.load(path, path.name.removesuffix(".canvas.json"))
    return {
        "name": canvas.name,
        "title": canvas.meta.get("story_title") or canvas.name,
        "story_id": canvas.meta.get("story_id"),
        "nodes": len(canvas.nodes),
        "edges": len(canvas.edges),
        "problems": len(canvas.validate()),
        "updated_at": path.stat().st_mtime,
    }


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
)
def list_canvases() -> dict[str, Any]:
    """列出 DramaFlow 工作区的故事画布与健康状态。"""
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    rows = [_canvas_row(path) for path in sorted(WORKSPACE.glob("*.canvas.json")) if path.is_file()]
    return {"workspace": str(WORKSPACE), "count": len(rows), "canvases": rows}


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
)
def inspect_canvas(canvas: str = "main") -> dict[str, Any]:
    """读取指定画布的节点、连线、拓扑顺序和体检问题，不修改数据。"""
    item = _load(canvas)
    try:
        order = item.topo_order()
    except CanvasError:
        order = []
    return {
        "name": item.name,
        "meta": item.meta,
        "node_types": NODE_SPEC,
        "nodes": [node.to_dict() for node in item.nodes.values()],
        "edges": [edge.to_dict() for edge in item.edges],
        "topological_order": order,
        "problems": item.validate(),
    }


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
def create_story_canvas(title: str, summary: str = "", canvas: str = "") -> dict[str, Any]:
    """创建一个隔离的故事画布。canvas 留空时自动生成安全名称；不会覆盖已有画布。"""
    if not title.strip():
        raise ValueError("故事标题不能为空")
    name = _safe_name(canvas) if canvas.strip() else f"story_{uuid.uuid4().hex[:10]}"
    path = canvas_path(WORKSPACE, name)
    if path.exists():
        raise FileExistsError(f"画布已存在：{name}")
    item = Canvas(name, path)
    story_id = item.ensure_story(title.strip()[:120], summary.strip()[:2000])
    item.save()
    return {"created": True, "canvas": name, "story_id": story_id, "title": item.meta["story_title"]}


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def export_prompts(canvas: str = "main") -> dict[str, Any]:
    """按画布拓扑和片段顺序导出所有生成提示词为 Markdown 文件。"""
    item = _load(canvas)
    content = item.render_all()
    output_dir = (WORKSPACE / "assets" / "exports").resolve()
    if WORKSPACE not in output_dir.parents:
        raise ValueError("导出路径越界")
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{_safe_name(canvas)}-prompts.md"
    temporary = output.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, output)
    return {
        "exported": True,
        "canvas": item.name,
        "path": str(output.relative_to(WORKSPACE)).replace("\\", "/"),
        "characters": len(content),
    }


@mcp.resource(
    "dramaflow://canvas/{name}",
    name="DramaFlow canvas snapshot",
    description="指定故事画布的只读 JSON 快照。",
    mime_type="application/json",
)
def canvas_resource(name: str) -> str:
    return json.dumps(_load(name).to_dict(), ensure_ascii=False, indent=2)


@mcp.prompt(name="short_drama_workflow", description="把一句创意扩写成可执行的短剧画布工作流。")
def short_drama_workflow(title: str, duration_seconds: int = 15) -> str:
    duration = max(5, min(int(duration_seconds), 180))
    return (
        f"为《{title.strip()}》规划一条约 {duration} 秒的短剧生产流程。"
        "先确认风格、人物、场景、时长与是否自动生成媒体；再依次完成剧本、"
        "分镜、角色/场景资产、逐镜提示词。没有用户明确批准时，不执行付费媒体生成。"
    )


def main() -> None:
    transport = os.getenv("DRAMAFLOW_MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=os.getenv("DRAMAFLOW_MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("DRAMAFLOW_MCP_PORT", "8765")),
            streamable_http_path="/mcp",
            json_response=True,
        )
        return
    if transport != "stdio":
        raise ValueError("DRAMAFLOW_MCP_TRANSPORT 只能是 stdio 或 streamable-http")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
