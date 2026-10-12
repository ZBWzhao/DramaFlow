"""阶段 0 冒烟：验证 openai SDK 的流式 + tool calling + usage 三件事能跑通。

这一步故意不引入任何架构 —— 就是"最小可用"：
  · 一个工具（list_dir）
  · 一个最小循环（问 → 调工具 → 再问 → 回答）
  · 结束时打印 usage

跑法（在 toonflow-py-lab/ 目录下）：
    python demo/smoke.py
    python demo/smoke.py "你的问题"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Settings  # noqa: E402
from core.llm import LLM, Usage  # noqa: E402


def list_dir(path: str = ".") -> str:
    p = Path(path)
    if not p.exists():
        return f"Error: 路径不存在 {path}"
    entries = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())
    return "\n".join(entries[:50]) or "(空目录)"


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出目录下的文件和子目录",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "目录路径"}},
            },
        },
    }
]


async def main() -> None:
    question = " ".join(sys.argv[1:]) or "当前目录里有哪些文件和文件夹？"
    llm = LLM(Settings.load())
    messages: list[dict] = [{"role": "user", "content": question}]
    total = Usage()

    for turn in range(1, 9):
        print(f"\n--- 第 {turn} 轮 ---")
        msg = await llm.chat(messages, TOOLS, on_text=lambda s: print(s, end="", flush=True))
        total.add(msg.usage)
        messages.append(msg.to_history_item())

        if not msg.tool_calls:
            print("\n(没有工具调用了，结束)")
            break

        for call in msg.tool_calls:
            args = call.parsed_args()
            print(f"\n[工具] {call.name}({args})")
            result = list_dir(**args) if call.name == "list_dir" else f"Error: 没有工具 {call.name}"
            print(result)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

    print(f"\n=== usage: {total.as_dict()} ===")


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
