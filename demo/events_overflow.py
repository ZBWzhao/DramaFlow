"""事件总线的"不许丢生命周期事件"回归（纯本地，不花 API 费用、毫秒级）。

为什么值得单独测：总线在队列满时如果**什么都丢**，丢掉 `tool_execution_end` 会让
前端那张工具卡永远停在"调用中"，而且不会有任何后续事件把它补回来 ——
现象就是"没完成、也不显示思考、也不显示结束"。

规则：队列满时**只能丢流式增量**（`*_update`）；生命周期事件必须送达。

跑法：python demo/events_overflow.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.events import EventBus  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, extra: str = "") -> None:
    print(f"{'✓' if ok else '✗'} {label}{f'　{extra}' if extra else ''}")
    if not ok:
        failures.append(label)


def main() -> None:
    bus = EventBus()
    queue = bus.subscribe(maxsize=8)

    # 先塞满队列：全是流式增量（这些丢了无害）
    for index in range(8):
        bus.emit("message_update", delta=f"字{index}")
    check("队列已被增量事件填满", queue.full(), f"dropped={bus.dropped}")

    # 关键：此时再来一条生命周期事件，它**必须**挤进去
    bus.emit("tool_execution_end", tool="get_canvas", tool_call_id="call_x", isError=False)

    drained = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    types = [e.type for e in drained]
    check("生命周期事件没有丢", types.count("tool_execution_end") == 1, f"队列内容={types}")
    check("丢掉的只是流式增量", bus.dropped >= 1, f"dropped={bus.dropped}")
    check("结束事件排在最后（顺序不乱）", types[-1] == "tool_execution_end")

    # 极端情况：队列里全是生命周期事件（不该发生），也不能把新事件吞掉
    bus2 = EventBus()
    q2 = bus2.subscribe(maxsize=4)
    for index in range(4):
        bus2.emit("sub_agent_report", sub_agent=f"a{index}", summary="x")
    bus2.emit("done", stats={})
    remaining = []
    while not q2.empty():
        remaining.append(q2.get_nowait().type)
    check("全是生命周期事件时也不吞新事件", remaining[-1] == "done", f"队列内容={remaining}")

    # 慢消费者的通道不应拖垮 emit（同步、非阻塞）
    bus3 = EventBus()
    q3 = bus3.subscribe(maxsize=1)
    for _ in range(500):
        bus3.emit("message_update", delta="x")
    check("海量增量不会阻塞 emit", True, f"dropped={bus3.dropped}")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
    print(f"\n{'✗ ' + '、'.join(failures) if failures else '✓ 全部通过'}")
    sys.exit(1 if failures else 0)
