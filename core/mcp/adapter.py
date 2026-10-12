"""Adapt discovered MCP tools to DramaFlow ToolDefinition objects."""

from __future__ import annotations

import re
from typing import Any

from jsonschema import ValidationError, validators

from core.context import ToolContext
from core.plugin import ToolDefinition, ToolResult

from .results import normalize_result


def public_tool_name(server: str, remote_name: str) -> str:
    safe_server = re.sub(r"[^A-Za-z0-9_]", "_", server).lower()
    safe_tool = re.sub(r"[^A-Za-z0-9_]", "_", remote_name)
    name = f"mcp_{safe_server}_{safe_tool}"[:96]
    if not re.match(r"^[a-z]", name):
        name = f"mcp_{name}"[:96]
    return name


def adapt_tool(manager: Any, server: str, config: Any, remote: Any) -> ToolDefinition:
    schema = dict(remote.input_schema or {"type": "object", "properties": {}})
    schema.setdefault("type", "object")
    validator_cls = validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)
    name = public_tool_name(server, remote.name)

    async def execute(raw: dict[str, Any], _: ToolContext) -> ToolResult:
        try:
            validator.validate(raw)
        except ValidationError as exc:
            location = ".".join(str(part) for part in exc.absolute_path) or "参数"
            return ToolResult(
                content=f"MCP 参数校验失败（{location}）：{exc.message}",
                is_error=True,
                details={"source": "mcp", "mcp_server": server, "mcp_tool": remote.name},
            )
        result = await manager.call_tool(server, remote.name, raw)
        return normalize_result(
            result,
            server=server,
            tool=remote.name,
            max_chars=config.max_result_chars,
        )

    description = (remote.description or remote.title or remote.name).strip()
    annotations = getattr(remote, "annotations", None)
    # MCP 的 readOnlyHint 是提示而不是鉴权边界；这里采用保守策略：只有明确声明只读
    # 才跳过 invocation 防重，未声明或写工具都按有副作用处理。
    side_effect = getattr(annotations, "read_only_hint", None) is not True
    return ToolDefinition(
        name=name,
        description=f"[MCP: {server}/{remote.name}] {description}"[:2000],
        parameters=schema,
        execute=execute,
        args_model=None,
        prompt_guidelines=[f"工具 {name} 来自 MCP 服务 {server}，只在确有需要时调用。"],
        side_effect=side_effect,
    )
