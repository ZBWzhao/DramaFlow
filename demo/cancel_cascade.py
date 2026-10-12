"""取消级联：父 Agent 被中止 → 子 Agent 必须一起停。

对应 Toonflow：AbortSignal.any([父信号, 子信号]) + 工具执行前 throwIfAborted()。
Python 侧两条路都要堵：
  1. Event（逻辑取消）：子 Agent 在自己的检查点主动退出
  2. task.cancel()（硬取消）：子 Agent 正在 await 模型时被立刻打断

跑法：
    python demo/cancel_cascade.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
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
from core.subagent import DelegationManager  # noqa: E402

WORKSPACE = ROOT / "workspace"
CANCEL_AFTER = 3.0  # 秒


async def main() -> None:
    tools, _, ctx = await create_agent_tools(cwd=str(WORKSPACE))
    llm = LLM(Settings.load())
    bus = EventBus()

    parent_cancel = asyncio.Event()
    parent = Agent(llm=llm, session=Session(), tools=[], context=ctx, bus=bus, cancel=parent_cancel)
    manager = DelegationManager(parent=parent, llm=llm, cwd=str(WORKSPACE))
    manager.parent_tool(tools)

    seen: list[str] = []

    async def watcher() -> None:
        q = bus.subscribe()
        try:
            while True:
                event = await q.get()
                if event.sub_agent and event.type in ("tool_execution_start", "sub_agent_report"):
                    seen.append(f"{event.sub_agent}/{event.type}")
        finally:
            bus.unsubscribe(q)

    watch_task = asyncio.create_task(watcher())
    task = asyncio.create_task(
        manager.delegate(
            name="slowpoker",
            task="把 workspace 里每个文件都 read_file 读一遍，每读一个就用 report 上报你看到了什么，最后总结",
        )
    )

    async def stopper() -> None:
        await asyncio.sleep(CANCEL_AFTER)
        print(f"\n>>> {CANCEL_AFTER}s 后：用户点了停止 → 父 Agent 取消")
        parent_cancel.set()

    started = time.time()
    await asyncio.gather(task, stopper(), return_exceptions=True)
    elapsed = time.time() - started

    print(f">>> 用时 {elapsed:.1f}s（未取消时会跑完全部文件）")
    print(f">>> 子 Agent 执行过的动作：{seen or '（还没来得及执行任何工具）'}")
    record = manager.registry["slowpoker"]
    watch_task.cancel()
    print(f">>> 子会话状态：{record.status}｜aborted={record.status == 'aborted'}")
    # 取消路径的用量结算：子会话在被打断前已经烧掉的 token，必须回收（对照 Toonflow catch 里的补统计）
    print(f">>> 子会话累计消耗：{record.session.usage_total().as_dict()}")
    print(f">>> 本次回收的增量：{record.last_usage.as_dict()}")
    if record.last_usage.total_tokens == 0 < record.session.usage_total().total_tokens:
        print(">>> ⚠️ 未回收：取消路径跳过了用量结算（Toonflow 在 catch 里会补统计）")
    print("结论：父的取消通过 Event + task.cancel 两条路传到子，子会话状态被标记为 aborted。")

    # ---------- 场景二：硬取消（客户端断开 → asyncio task.cancel()）----------
    print("\n=== 场景二：硬取消（task.cancel()，对应客户端断开连接）===")
    parent_cancel.clear()  # 场景一的信号还留着，不清掉子 Agent 会一启动就退（测不到真取消）
    hard = asyncio.create_task(
        manager.delegate(
            name="hardstop",
            task="把 workspace 里每个文件读一遍，每个文件都要写一段不少于 150 字的总结并 report 上报，最后再汇总",
        )
    )
    # 轮询到「已产生消耗 且 仍在运行」再掐：早了测不到（0 消耗），晚了它已完成（测的是成功路径）
    probe = manager.registry.get("hardstop")
    for _ in range(200):
        await asyncio.sleep(0.25)
        probe = manager.registry.get("hardstop")
        if probe and probe.status == "running" and probe.session.usage_total().total_tokens > 0:
            break
    print(
        f">>> 此刻状态={probe.status if probe else '?'}、已消耗 "
        f"{probe.session.usage_total().total_tokens if probe else 0} tokens → task.cancel()"
    )
    hard.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await hard
    record2 = manager.registry["hardstop"]
    total2 = record2.session.usage_total().total_tokens
    print(f">>> 子会话状态：{record2.status}")
    print(f">>> 子会话累计消耗：{record2.session.usage_total().as_dict()}")
    print(f">>> 本次回收的增量：{record2.last_usage.as_dict()}")
    if total2 == 0:
        print(">>> （子会话尚未产生消耗，本次无法判定结算路径）")
    elif record2.last_usage.total_tokens == 0:
        print(f">>> ⚠️ 花了 {total2} tokens 但回收为 0 —— 硬取消路径跳过了用量结算")
    else:
        print(f">>> ✅ 硬取消路径也结算了：回收 {record2.last_usage.total_tokens} tokens（累计 {total2}）")


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
