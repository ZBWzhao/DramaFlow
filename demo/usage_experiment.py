"""对照实验：增量 token 计量 vs 全量计量。

这是简历上那个"实测避免重复计费 XX%"的数据来源。

场景：同一个子 Agent（researcher）被**连续委派两次**，它的会话是复用的
（第二次请求会带上第一次的全部历史 —— 这就是 Toonflow 里子会话复用的真实情况）。

  · 增量做法（正确）：每次委派前记 usage_offset()，结束后只求和新增
  · 全量做法（错误）：每次都把该子会话的 usage_total() 全算进去
                      → 第一次的量会被重复计一次，会话越长越离谱

跑法：
    python demo/usage_experiment.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from core.agent import Agent  # noqa: E402
from core.config import Settings  # noqa: E402
from core.events import EventBus  # noqa: E402
from core.llm import LLM, Usage  # noqa: E402
from core.loader import create_agent_tools  # noqa: E402
from core.session import Session  # noqa: E402
from core.subagent import DelegationManager  # noqa: E402

WORKSPACE = ROOT / "workspace"

TASKS = [
    "统计 workspace 下 *.md 文件的行数和字符数，用一句话回答",
    "再统计 workspace 下 *.txt 文件的行数和字符数，用一句话回答",
]


def table(rows: list[tuple[str, int, int, int]]) -> None:
    print(f"{'':<12}{'input':>10}{'output':>10}{'total':>10}")
    for label, i, o, t in rows:
        print(f"{label:<12}{i:>10}{o:>10}{t:>10}")


async def main() -> None:
    tools, _, ctx = await create_agent_tools(cwd=str(WORKSPACE))
    llm = LLM(Settings.load())
    bus = EventBus()
    parent = Agent(llm=llm, session=Session(), tools=[], context=ctx, bus=bus)
    manager = DelegationManager(parent=parent, llm=llm, cwd=str(WORKSPACE))
    manager.parent_tool(tools)  # 注册子 Agent 可用的工具白名单

    incremental = Usage()
    naive = Usage()
    print("=" * 60)
    for i, task in enumerate(TASKS, 1):
        result = await manager.delegate(name="researcher", task=task)
        incremental.add(result.usage)
        naive.add(result.usage_all)
        print(f"第 {i} 次委派  增量 {result.usage.total_tokens:>6} tokens｜"
              f"全量 {result.usage_all.total_tokens:>6} tokens（累计历史 {result.usage_all.total_tokens - result.usage.total_tokens}）")

    print("\n" + "=" * 60)
    print("两种算法对比（同一个子会话，委派 2 次）")
    table([
        ("增量（正确）", incremental.input_tokens, incremental.output_tokens, incremental.total_tokens),
        ("全量（错误）", naive.input_tokens, naive.output_tokens, naive.total_tokens),
    ])
    diff = naive.total_tokens - incremental.total_tokens
    pct = diff / incremental.total_tokens * 100 if incremental.total_tokens else 0.0
    print(f"\n重复计入：{diff} tokens（虚高 {pct:.1f}%）")
    print("结论：子会话复用时，全量统计会把上一轮的历史重复计费 —— 这就是 previousEntries 偏移存在的理由。")


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
