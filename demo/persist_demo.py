"""会话持久化：子 Agent 状态与上报落盘，进程重启后能恢复（含"上次运行已中断"）。

对照 Toonflow：
  · runtime/sessions.ts 的 updateSubAgent()        → 状态写进**父会话**的 custom entry
  · runtime/sessions.ts 的 updateSubAgentReport()  → 上报同样落盘（customType: subAgentReport）
  · 读会话时 status=running 且进程里没有活会话    → 标记 cancelled「上次运行已中断」

跑法：
    python demo/persist_demo.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from core.agent import Agent  # noqa: E402
from core.config import Settings  # noqa: E402
from core.events import EventBus  # noqa: E402
from core.llm import LLM  # noqa: E402
from core.loader import create_agent_tools  # noqa: E402
from core.session import Session  # noqa: E402
from core.subagent import (  # noqa: E402
    CUSTOM_REPORT,
    CUSTOM_SUBAGENT,
    DelegationManager,
    restore_subagents,
)

WORKSPACE = ROOT / "workspace"
SESSIONS = ROOT / ".agent" / "sessions"


def show(title: str, rows: list[str]) -> None:
    print(f"\n--- {title} ---")
    for row in rows:
        print(f"  {row}")


async def main() -> None:
    sid = f"persist-{uuid.uuid4().hex[:6]}"
    parent_path = SESSIONS / f"{sid}.jsonl"

    tools, _, ctx = await create_agent_tools(cwd=str(WORKSPACE))
    llm = LLM(Settings.load())
    bus = EventBus()
    parent_session = Session(session_id=sid, path=parent_path)
    parent = Agent(llm=llm, session=parent_session, tools=[], context=ctx, bus=bus)
    manager = DelegationManager(parent=parent, llm=llm, cwd=str(WORKSPACE))
    manager.parent_tool(tools)

    print(f"父会话：{parent_path.name}")

    # ---------- 1. 正常委派一次 ----------
    result = await manager.delegate(
        name="researcher",
        task="用 word_count 统计 workspace/sample.py，然后 report 上报一行结论，最后用一句话总结",
    )
    child_path = SESSIONS / f"{sid}.researcher.jsonl"
    show(
        "1. 落盘结果",
        [
            f"父会话文件：{parent_path.name}（{len(parent_session.entries())} 条 entry）",
            f"子会话文件：{child_path.name}（存在={child_path.exists()}，独立落盘）",
            f"委派结论（{result.status}）：{result.answer[:60]}…",
        ],
    )

    # ---------- 2. 看父会话里的 custom entry ----------
    customs = [e for e in parent_session.entries() if e.get("type") == "custom"]
    states = [c["data"] for c in customs if c.get("custom_type") == CUSTOM_SUBAGENT]
    reports = [c["data"] for c in customs if c.get("custom_type") == CUSTOM_REPORT]
    show(
        "2. 父会话里的 custom entry（不进上下文，只给人/前端/恢复逻辑看）",
        [
            f"{CUSTOM_SUBAGENT} × {len(states)}：状态流转 {[s['status'] for s in states]}",
            f"终态快照：{json.dumps(states[-1], ensure_ascii=False)[:150]}…",
            f"{CUSTOM_REPORT} × {len(reports)}：{[r['content'][:24] for r in reports]}",
            f"对照：父会话的 messages 只有 {len(parent_session.messages)} 条（custom 不污染上下文）",
        ],
    )

    # ---------- 3. 模拟进程重启：只用文件恢复 ----------
    fresh_parent = Session.load(parent_path)
    restored = restore_subagents(fresh_parent)
    show(
        "3. 模拟重启后恢复（不依赖内存 registry）",
        [f"{r['name']}：status={r['status']}｜usage={r.get('usage', {}).get('total_tokens')} tokens｜file={r.get('file')}" for r in restored],
    )

    # ---------- 4. 中断标记：文件里写着 running，但进程里没有它 ----------
    fresh_parent.append_custom(
        CUSTOM_SUBAGENT,
        {"name": "ghost", "status": "running", "task": "（模拟：进程被打断时它还在跑）", "file": None},
    )
    restored2 = restore_subagents(Session.load(parent_path))
    ghost = next(r for r in restored2 if r["name"] == "ghost")
    show(
        "4. 孤儿子 Agent（对应 Toonflow 的「上次运行已中断」）",
        [f"{ghost['name']}：status={ghost['status']}｜interrupted={ghost.get('interrupted')}｜note={ghost.get('note')}"],
    )

    # ---------- 5. 同名复用：子会话历史从文件加载 ----------
    offset_before = manager.registry["researcher"].session.usage_offset()
    await manager.delegate(name="researcher", task="再确认一次：sample.py 里定义了哪两个函数？一句话")
    record = manager.registry["researcher"]
    show(
        "5. 同名子 Agent 复用（重启后也能接着聊）",
        [
            f"复用前 usage 记录数（= 起始偏移）：{offset_before}（>0 说明历史是从文件加载的）",
            f"复用后累计：{record.session.usage_total().as_dict()}",
            f"本次增量：{record.last_usage.as_dict()}（只算新增，没有把历史重复计费）",
        ],
    )

    print("\n结论：子 Agent 的状态/上报落在**父会话的 custom entry**，会话本身独立成文件；")
    print("      重启后能恢复子 Agent 列表，未跑完的会被标成 interrupted 而不是假装成功。")


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
