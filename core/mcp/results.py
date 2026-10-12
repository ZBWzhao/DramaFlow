"""Convert MCP content blocks to the runtime's text + UI-details result shape."""

from __future__ import annotations

import json
from typing import Any

from core.plugin import ToolResult


def _block_text(block: Any) -> str:
    block_type = getattr(block, "type", "")
    if block_type == "text":
        return str(getattr(block, "text", ""))
    if block_type in {"image", "audio"}:
        mime = getattr(block, "mime_type", None) or getattr(block, "mimeType", "")
        return f"[{block_type} content: {mime or 'unknown mime'}]"
    if block_type == "resource_link":
        return f"[resource: {getattr(block, 'name', '')} {getattr(block, 'uri', '')}]".strip()
    if block_type == "resource":
        resource = getattr(block, "resource", None)
        text = getattr(resource, "text", None)
        return str(text) if text is not None else f"[embedded resource: {getattr(resource, 'uri', '')}]"
    try:
        return json.dumps(block.model_dump(by_alias=True), ensure_ascii=False)
    except Exception:  # noqa: BLE001 - foreign MCP content may use custom block types
        return str(block)


def normalize_result(result: Any, *, server: str, tool: str, max_chars: int) -> ToolResult:
    parts = [value for value in (_block_text(block) for block in (result.content or [])) if value]
    if not parts and result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False, default=str))
    content = "\n".join(parts) or "MCP 工具执行完成（无文本输出）"
    original_chars = len(content)
    truncated = original_chars > max_chars
    if truncated:
        content = content[:max_chars] + f"\n…[MCP 结果已截断，原始 {original_chars} 字符]"
    return ToolResult(
        content=content,
        is_error=bool(result.is_error),
        details={
            "source": "mcp",
            "mcp_server": server,
            "mcp_tool": tool,
            "result_type": getattr(result, "result_type", "complete"),
            "structured_content": result.structured_content,
            "truncated": truncated,
            "original_chars": original_chars,
        },
    )
