"""会话树分支：从某条消息分叉，两个分支都在文件里，内存里只有活动分支。

对照 Pi 的会话树 / Toonflow 的 resendFrom：
  · 每条消息有 id 与 parent，分支 = 同一个 parent 下有多个孩子
  · JSONL 只追加不删 —— 被切掉的分支仍在文件里，可回放、可对比
  · 活动分支（内存里的 messages）只有一条链

跑法：
    python demo/branch_demo.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from core.agent import Agent, build_system_prompt  # noqa: E402
from core.config import Settings  # noqa: E402
from core.events import EventBus  # noqa: E402
from core.llm import LLM  # noqa: E402
from core.loader import create_agent_tools  # noqa: E402
from core.session import SESSIONS_DIR, Session  # noqa: E402

WORKSPACE = ROOT / "workspace"


def show_tree(session: Session, title: str) -> None:
    print(f"\n{title}")
    for i, (mid, msg) in enumerate(zip(session.message_ids, session.messages), 1):
        role = msg.get("role", "?")
        text = (msg.get("content") or "").replace("\n", " ")[:42]
        if msg.get("tool_calls"):
            text = "调用工具 " + ",".join(c["function"]["name"] for c in msg["tool_calls"])
        print(f"  {i}. [{mid}] {role:<9} {text}")


async def main() -> None:
    tools, _, ctx = await create_agent_tools(cwd=str(WORKSPACE))
    llm = LLM(Settings.load())
    bus = EventBus()

    file = SESSIONS_DIR / "branch-demo.jsonl"
    file.unlink(missing_ok=True)
    session = Session(path=file)
    session.system_prompt = build_system_prompt(str(WORKSPACE), tools)
    agent = Agent(llm=llm, session=session, tools=tools, context=ctx, bus=bus)

    await agent.run("workspace 里有哪些文件？一句话回答")
    fork_id = session.entry_id_at(0)  # 从第一条（用户提问）分叉
    show_tree(session, "① 第一轮之后的活动分支：")

    await agent.run("把 notes.txt 读一遍，讲三件事")
    show_tree(session, "② 第二轮之后：")

    session.branch_from(fork_id)
    show_tree(session, f"③ 从 {fork_id} 分叉后（后续消息已从活动分支移除）：")

    await agent.run("用 word_count 统计 *.py 的规模，一句话回答")
    show_tree(session, "④ 新分支跑完：")

    entries = session.entries()
    kinds: dict[str, int] = {}
    for entry in entries:
        kinds[entry.get("type", "?")] = kinds.get(entry.get("type", "?"), 0) + 1
    parents: dict[str | None, int] = {}
    for entry in entries:
        if entry.get("type") != "message":
            continue
        parents[entry.get("parent")] = parents.get(entry.get("parent"), 0) + 1

    print(f"\n落盘文件：{file}")
    print(f"条目统计：{kinds}")
    fork_points = {p: c for p, c in parents.items() if c > 1}
    print(f"分叉点（同一个 parent 有多个孩子）：{fork_points or '无'}")
    print("结论：活动分支只有一条链，但文件里两个分支都在 —— 这就是 JSONL 会话树。")


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
