"""压缩基准：量化"会话历史压缩"到底省多少（一次便宜的 LLM 摘要调用）。

为什么值得单独量：
  团队/委派场景的成本大头不是工具 schema，而是**累积历史** ——
  第 N 轮的输入 ≈ N × 单轮增量，所以 15 轮会话的实际输入远大于"15 × 单轮"。

跑法：
    python demo/compact_bench.py            # 用子 Agent 阈值(8000)
    python demo/compact_bench.py 12000      # 指定阈值
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.compaction import KEEP_RECENT, maybe_compact  # noqa: E402
from core.config import Settings  # noqa: E402
from core.llm import LLM  # noqa: E402
from core.session import Session  # noqa: E402
from core.subagent import SUB_AGENT_COMPACT_TOKENS  # noqa: E402

TURNS = 24


def build_session() -> Session:
    session = Session()
    session.system_prompt = "你是短剧助手，负责把创意拆成分镜与提示词。"
    for index in range(TURNS):
        session.append({"role": "user", "content": f"第{index}轮：补充设定与要求。" * 30})
        session.append({"role": "assistant", "content": "收到，已记录。" + "处理说明。" * 20})
    return session


async def main() -> None:
    threshold = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else min(8000, SUB_AGENT_COMPACT_TOKENS)
    print(f"阈值：{threshold} tokens（子 Agent 默认 {SUB_AGENT_COMPACT_TOKENS}）")
    print(f"构造 {TURNS} 轮对话（{TURNS * 2} 条消息）…\n")

    session = build_session()
    before_chars = session.size_chars()
    before_msgs = len(session.messages)
    print(f"压缩前：{before_chars} 字符 / {before_msgs} 条消息")
    print(f"  若按 4 字符≈1 token 粗算，单轮上下文约 {before_chars // 4} tokens")

    settings = Settings.load()
    summary = await maybe_compact(
        session,
        LLM(settings),
        token_limit=threshold,
        context_window=settings.context_window,
        max_output_tokens=settings.max_output_tokens,
    )
    if not summary:
        print("未触发压缩（当前大小低于阈值）")
        return

    after_chars = session.size_chars()
    after_msgs = len(session.messages)
    print(f"\n压缩后：{after_chars} 字符 / {after_msgs} 条消息（保留最近 {KEEP_RECENT} 条）")
    print(f"降幅：{(1 - after_chars / before_chars) * 100:.0f}%")
    print(f"摘要长度：{len(summary)} 字")

    # 成本直觉：假设还要再跑 10 轮，压缩后的每轮上下文都更小
    rounds = 10
    saved = (before_chars - after_chars) * rounds // 4
    print(
        f"\n若后续还要跑 {rounds} 轮：仅历史部分就少发约 {saved} tokens"
        f"（压缩本身花一次摘要调用，输出 {len(summary)} 字）"
    )
    print(f"\n摘要开头：\n{summary[:200]}…")


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
