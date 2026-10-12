"""LangGraph 分支公共部分：设置、模型、工具、实验任务。

刻意复用 core.config.Settings 的 key 读取逻辑，两个分支用同一个模型，
对比才有意义（唯一差异只剩：编排层手写 vs 框架）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # langgraph_branch/common.py → 项目根
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openai import AsyncOpenAI  # noqa: E402

from core.config import Settings  # noqa: E402
from core.context import ToolContext  # noqa: E402

WORKSPACE = ROOT / "workspace"

TASK = "读 sample.py，统计它的行数、字符数和 top3 高频词"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读工作区内的文本文件",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对路径"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "word_count",
            "description": "统计文本行数、单词数、字符数与高频词",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "待统计的文本"},
                    "top": {"type": "integer", "description": "返回前 N 个高频词"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    },
]

SYSTEM = (
    "你是编程助手，用中文简洁回答。"
    "需要文件内容时必须调用工具，不要猜测。"
    "路径一律相对工作区根目录给定（例如 sample.py，不要写成 workspace/sample.py）。"
    "拿到足够信息后直接给结论，不再调用工具。"
)


def make_client() -> AsyncOpenAI:
    s = Settings.load()
    return AsyncOpenAI(api_key=s.api_key, base_url=s.base_url)


def make_context() -> ToolContext:
    return ToolContext(cwd=str(WORKSPACE))


async def call_llm(client: AsyncOpenAI, messages: list[dict], tools: list[dict] | None = None) -> dict:
    """非流式，节点里够用；返回 assistant 消息 dict。

    tools 可覆盖（父 Agent 要把 subagent 工具也暴露出去）。
    """
    resp = await client.chat.completions.create(
        model=Settings.load().model,
        messages=messages,
        tools=tools if tools is not None else TOOLS,
        tool_choice="auto",
    )
    msg = resp.choices[0].message
    out: dict = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        out["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in msg.tool_calls
        ]
    # 只留整型字段：provider 的 usage 里常夹带 None（prompt_tokens_details 之类）
    raw = resp.usage.model_dump() if resp.usage else {}
    out["_usage"] = {k: v for k, v in raw.items() if isinstance(v, int)}
    return out


async def run_tool(name: str, args: dict) -> str:
    """工具执行走同一个 ToolContext（沙箱与手写分支一致）。

    失败不抛，而是**把错误文本当作工具结果喂回模型**让它自己改参数 ——
    这和手写分支 core/agent.py 的处理保持一致（沙箱拦截也是这个路径）。
    """
    import json
    import re
    from collections import Counter

    ctx = make_context()
    try:
        if name == "read_file":
            return ctx.files.read_text(args["path"])
        if name == "word_count":
            text = args["text"]
            words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text.lower())
            top = args.get("top", 5)
            return json.dumps(
                {
                    "lines": len(text.splitlines()),
                    "chars": len(text),
                    "words": len(words),
                    "top": Counter(words).most_common(top),
                },
                ensure_ascii=False,
            )
        return f"未知工具 {name}"
    except Exception as exc:  # noqa: BLE001
        return f"调用失败：{type(exc).__name__}: {exc}（检查参数后重试）"


def count_file(file: Path) -> dict[str, int]:
    text = file.read_text(encoding="utf-8")
    return {"lines": len(text.splitlines()), "chars": len(text), "size": file.stat().st_size}


if __name__ == "__main__":
    print(Settings.load().model, "|", Settings.load().base_url)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
