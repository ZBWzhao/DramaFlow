"""上下文压缩 + 会话落盘：把阈值调到很小，强制触发压缩看效果。

跑法：
    python demo/compact_demo.py

看三件事：
  1. 超阈值 → 较早历史被换成摘要（保留最近 N 条）
  2. 切点不会落在 tool 结果中间（否则会出现孤儿 tool 消息）
  3. 压缩前的原始对话仍在 JSONL 文件里（.agent/sessions/<id>.jsonl）
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# 故意把阈值压到很小，让第二轮请求前必然触发压缩
os.environ.setdefault("COMPACT_THRESHOLD_CHARS", "1500")
os.environ.setdefault("COMPACT_KEEP_RECENT", "2")

from core.agent import Agent, build_system_prompt  # noqa: E402
from core.config import Settings  # noqa: E402
from core.events import EventBus  # noqa: E402
from core.llm import LLM  # noqa: E402
from core.loader import create_agent_tools  # noqa: E402
from core.session import SESSIONS_DIR, Session  # noqa: E402

WORKSPACE = ROOT / "workspace"
QUESTIONS = [
    "workspace 里有哪些文件？",
    "把 notes.txt 读一遍，告诉我里面讲了哪几件事",
    "再统计一下 workspace 下 *.py 的规模",
]


async def main() -> None:
    tools, _, ctx = await create_agent_tools(cwd=str(WORKSPACE))
    llm = LLM(Settings.load())
    bus = EventBus()
    demo_file = SESSIONS_DIR / "compact-demo.jsonl"
    demo_file.unlink(missing_ok=True)  # 每次跑都是干净的会话文件
    session = Session(path=demo_file)
    session.system_prompt = build_system_prompt(str(WORKSPACE), tools)
    agent = Agent(
        llm=llm,
        session=session,
        tools=tools,
        context=ctx,
        bus=bus,
        compact_threshold=int(os.environ["COMPACT_THRESHOLD_CHARS"]),
    )

    seen: list[str] = []
    q = bus.subscribe()

    async def watcher() -> None:
        while True:
            event = await q.get()
            if event.type.startswith("compaction"):
                seen.append(f"{event.type} {event.data}")

    task = asyncio.create_task(watcher())

    for question in QUESTIONS:
        print(f"\n{'=' * 60}\n>>> {question}")
        before = session.size_chars()
        await agent.run(question)
        after = session.size_chars()
        print(f"    上下文 {before} → {after} 字符｜压缩次数 {session.compact_count}")

    task.cancel()

    print(f"\n{'=' * 60}\n压缩事件：")
    for line in seen:
        print("  " + line)

    entries = session.entries()
    kinds: dict[str, int] = {}
    for entry in entries:
        kinds[entry.get("type", "?")] = kinds.get(entry.get("type", "?"), 0) + 1
    print(f"\n落盘文件：{session.path}")
    print(f"条目统计：{kinds}")
    print("注意：compaction 条目只记录摘要，被压掉的原始消息仍以 message 条目留在文件里 —— 可回放、可审计。")
    print(f"最终用量：{session.usage_total().as_dict()}（含压缩请求本身）")


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
