"""LLM 层：openai SDK 的薄封装（流式 + tool calling + usage）。

对照 Toonflow/Pi：
  · 这个文件只负责"一次请求"，循环逻辑在 core/agent.py（对应 Pi 的 agent-loop.ts）
  · usage 是本模块最关键的产出 —— 模块二的"增量 token 计量"完全依赖它
  · 流式：逐块拼接文本与 tool_calls；include_usage 让最后一个 chunk 带回用量

为什么用 openai SDK 而不是 urllib：SDK 自带流式增量解析与 tool_calls 拼装，
手写容易漏边界情况；而 DeepSeek 是 OpenAI 兼容协议，换 base_url 即可。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from openai import AsyncOpenAI

from .config import Settings


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # 原始 JSON 字符串（保持和模型输出一致）

    def parsed_args(self) -> dict[str, Any]:
        try:
            return json.loads(self.arguments or "{}")
        except json.JSONDecodeError:
            return {}


@dataclass
class Usage:
    """一次请求的用量。对应 Pi 的 Usage / Toonflow 的 addUsage。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: "Usage") -> "Usage":
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_tokens += other.cached_tokens
        return self

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            # 缓存命中要一起报：它是这个项目里最直接的成本指标
            # （轨迹是"第 N 轮输入 ≈ N × 单轮增量"，命中率直接决定账单）
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class AssistantMessage:
    content: str = ""
    reasoning: str = ""  # 推理模型的 reasoning_content（要原样回传，模型才能接着推理）
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None

    def to_history_item(self) -> dict[str, Any]:
        """转成要写进会话历史的 assistant 消息。

        注意 content 为 None 的情况：模型只调工具时会返回空 content，
        这里保留空字符串比 None 更安全（部分 provider 不接受 null content）。
        """
        item: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.reasoning:
            item["reasoning_content"] = self.reasoning
        if self.tool_calls:
            item["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return item


class LLM:
    """一次请求的封装。取消由调用方的 asyncio.Task.cancel() 触发（传播到 httpx 连接）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.load()
        self.client = AsyncOpenAI(
            api_key=self.settings.api_key,
            base_url=self.settings.base_url,
            timeout=180,
        )
        self.model = self.settings.model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> AssistantMessage:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": self.settings.max_output_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        # 用 AsyncOpenAI：取消（asyncio.Task.cancel）能直接传播到底层 HTTP 连接，
        # 对应 Toonflow 里 AbortSignal 一路传到模型请求的做法。
        stream: AsyncIterator[Any] = await self.client.chat.completions.create(**kwargs)

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage = Usage()
        finish_reason: str | None = None

        async for chunk in stream:
            if getattr(chunk, "usage", None):
                u = chunk.usage
                usage.input_tokens = getattr(u, "prompt_tokens", 0) or 0
                usage.output_tokens = getattr(u, "completion_tokens", 0) or 0
                cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0)
                usage.cached_tokens = cached or 0
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta
            if getattr(delta, "content", None):
                text_parts.append(delta.content)
                if on_text:
                    on_text(delta.content)
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                reasoning_parts.append(reasoning)
                if on_reasoning:
                    # 推理内容也要能流出去 —— 之前只攒在内存里，前端拿不到"思考过程"，
                    # 于是界面上只剩下一堆"什么时候都没有文字"的空档。
                    on_reasoning(reasoning)
            for piece in getattr(delta, "tool_calls", None) or []:
                slot = calls.setdefault(piece.index, {"id": "", "name": "", "args": ""})
                if piece.id:
                    slot["id"] += piece.id
                fn = getattr(piece, "function", None)
                if fn:
                    if getattr(fn, "name", None):
                        slot["name"] += fn.name
                    if getattr(fn, "arguments", None):
                        slot["args"] += fn.arguments

        return AssistantMessage(
            content="".join(text_parts),
            reasoning="".join(reasoning_parts),
            tool_calls=[ToolCall(id=v["id"], name=v["name"], arguments=v["args"]) for _, v in sorted(calls.items())],
            usage=usage,
            finish_reason=finish_reason,
        )

    async def simple(self, messages: list[dict[str, Any]]) -> str:
        """只要文本结果（给上下文压缩用）。"""
        msg = await self.chat(messages, tools=None)
        return msg.content



