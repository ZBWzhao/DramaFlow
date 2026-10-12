"""工具插件契约：ToolPlugin / ToolMetadata / ToolDefinition。

对照 Toonflow：packages/toolScaffold/src/runtime.ts
  · TS 接口 ToolPlugin { validateConfig; createTools }  →  Python 的 typing.Protocol
  · Zod 的 toolMetadataSchema                            →  Pydantic v2 的 ToolMetadata
  · defineTool(...)                                      →  define_tool(...)（本文件底部）

一个工具 = 名字 + 描述 + 参数 schema + 执行函数，再加一层元数据：
  元数据（displayName/description/prompt/...）→ 给装载器和提示词用
  参数 schema                                 → 给模型用（Pydantic 模型自动转 JSON Schema）
  执行函数                                    → 只在本地跑
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .context import ToolContext


# ---------------------------------------------------------------------------
# 元数据：装载器在"不执行工具代码"的前提下读到的东西（对应文件头 /*! toonflowTool */）
# ---------------------------------------------------------------------------
class ToolMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-zA-Z0-9_]*$", max_length=96)
    display_name: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=2000)
    version: str = "0.1.0"
    author: str = ""
    prompt: str = ""  # promptGuidelines：额外注入系统提示的用法约束
    enabled: bool = True
    config_rules: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if len(value) > 96:
            raise ValueError("工具名过长")
        return value


# ---------------------------------------------------------------------------
# 工具结果：content 进上下文，details 只给界面（和 Pi 的 AgentToolResult 一致）
# ---------------------------------------------------------------------------
class ToolResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    content: str
    details: dict[str, Any] = Field(default_factory=dict)
    is_error: bool = False


class ToolDefinition(BaseModel):
    """一个可被模型调用的工具。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema（由 Pydantic 参数模型生成）
    execute: Callable[[dict[str, Any], ToolContext], Any]  # 可能返回协程
    args_model: type[BaseModel] | None = None
    prompt_guidelines: list[str] = Field(default_factory=list)
    side_effect: bool = False

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def validate_args(self, raw: dict[str, Any]) -> BaseModel | None:
        """Pydantic 校验模型传来的参数。失败时不抛异常，交给循环转成工具报错。"""
        if self.args_model is None:
            return None
        return self.args_model.model_validate(raw)


# ---------------------------------------------------------------------------
# 插件契约
# ---------------------------------------------------------------------------
@runtime_checkable
class ToolPlugin(Protocol):
    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """校验并返回规范化配置（对应 ToolPlugin.validateConfig）。"""
        ...

    def create_tools(self, context: ToolContext) -> list[ToolDefinition] | Awaitable[list[ToolDefinition]]:
        """按上下文造工具（对应 ToolPlugin.createTools）。"""
        ...


# ---------------------------------------------------------------------------
# define_tool：把「Pydantic 参数模型 + 执行函数」打包成 ToolDefinition
# ---------------------------------------------------------------------------
def define_tool(
    name: str,
    description: str,
    args_model: type[BaseModel],
    execute: Callable[[BaseModel, ToolContext], Any],
    prompt_guidelines: list[str] | None = None,
    side_effect: bool = False,
) -> ToolDefinition:
    """参数 schema 由 Pydantic 自动生成，不用手写 JSON Schema。

    additionalProperties 关掉：模型乱传字段时会被 Pydantic 挡住，
    报错信息再喂回模型让它自己改（这就是"参数校验"的实际价值）。
    """
    schema = args_model.model_json_schema()
    schema.pop("title", None)
    schema["additionalProperties"] = False

    def _execute(raw: dict[str, Any], ctx: ToolContext) -> Any:
        args = args_model.model_validate(raw)
        return execute(args, ctx)

    return ToolDefinition(
        name=name,
        description=description,
        parameters=schema,
        execute=_execute,
        args_model=args_model,
        prompt_guidelines=prompt_guidelines or [],
        side_effect=side_effect,
    )


def tool_result(content: str, is_error: bool = False, **details: Any) -> ToolResult:
    """构造工具结果。

    注意 is_error 必须是**显式关键字参数**：
    早期版本写成 `tool_result(content, **details)`，于是 `tool_result(..., is_error=True)`
    被当成 details 里的一个键 —— agent 层读的是 `result.is_error`，标记就静默失效了
    （工具明明失败了，循环却当成功处理，还把 "is_error" 塞进了发给前端的事件里）。
    """
    return ToolResult(content=content, details=details, is_error=is_error)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
