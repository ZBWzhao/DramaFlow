"""模块二验收：子 Agent 委派（独立会话 + 取消级联 + 增量用量 + 父子两条通道）。

跑法：
    python demo/run_delegation.py

看点：
  · 父 Agent 调用 subagent 工具 → 派生子会话（事件带 sub_agent 标记）
  · 子 Agent 用 report 工具把结论发回父侧（不是共享变量）
  · 委派结束后只回收**本次新增**的用量
"""

from __future__ import annotations

import asyncio
import contextlib
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
from core.session import Session  # noqa: E402
from core.subagent import DelegationManager  # noqa: E402

WORKSPACE = ROOT / "workspace"
DEFAULT_QUESTION = (
    "请派一个叫 researcher 的子 Agent，去统计 workspace 下 *.py 文件的规模（行数、字符数、高频词），"
    "让它先用 report 上报结论，然后你根据它的上报给我一句总结。"
)


def tag(event) -> str:
    return f"(sub:{event.sub_agent}) " if event.sub_agent else ""


async def printer(bus: EventBus) -> None:
    q = bus.subscribe()
    try:
        while True:
            event = await q.get()
            data = event.data
            if event.type == "message_update":
                print(data.get("delta", ""), end="", flush=True)
            elif event.type == "tool_execution_start":
                print(f"\n  {tag(event)}[工具] {data['tool']}({str(data.get('args', ''))[:70]})")
            elif event.type == "tool_execution_end":
                print(f"  {tag(event)}[结果] isError={data.get('isError')}")
            elif event.type in ("sub_agent_start", "sub_agent_end"):
                print(f"\n  >>> {event.type} {data}")
            elif event.type == "sub_agent_usage":
                print(f"  >>> 子 Agent 用量（增量，自第 {data['since']} 条起）：{data['total_tokens']} tokens")
            elif event.type == "sub_agent_report":
                print(f"\n  >>> 子 Agent 上报：{data['summary']}")
            elif event.type == "usage" and not event.sub_agent:
                print(f"  [父用量] +{data.get('total_tokens')} tokens")
    except asyncio.CancelledError:
        return


async def main() -> None:
    question = " ".join(sys.argv[1:]) or DEFAULT_QUESTION

    tools, _, ctx = await create_agent_tools(cwd=str(WORKSPACE))
    llm = LLM(Settings.load())
    bus = EventBus()
    session = Session()
    manager = DelegationManager(parent=None, llm=llm, cwd=str(WORKSPACE))  # type: ignore[arg-type]

    parent_tools = list(tools) + [manager.parent_tool(tools)]
    session.system_prompt = build_system_prompt(str(WORKSPACE), parent_tools)

    parent = Agent(llm=llm, session=session, tools=parent_tools, context=ctx, bus=bus)
    manager.parent = parent

    print(f"父工具：{[t.name for t in parent_tools]}\n问题：{question}\n" + "=" * 60)

    task = asyncio.create_task(printer(bus))
    try:
        answer = await parent.run(question)
    finally:
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    print("\n" + "=" * 60)
    print(answer)
    print(f"\n父会话用量（不含子会话）：{session.usage_total().as_dict()}")
    for name, record in manager.registry.items():
        print(f"子会话 {name}: 本次 {record.last_usage.as_dict()}｜累计 {record.session.usage_total().as_dict()}")
        for report in record.reports:
            print(f"  上报：{report}")


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
