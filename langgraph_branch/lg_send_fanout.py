"""Send 扇出：一条条件边同时派多个子任务，对比手写的并发委派。

对照 demo/subagent_bench.py
  · 手写：asyncio.gather + TaskGroup，且每个子 Agent 共享 workspace singleton…
  · LangGraph：Send 天然做 map-reduce 分发，各分支独立，结果汇聚到 state

跑法：
    python langgraph_branch/lg_send_fanout.py
"""

from __future__ import annotations

import asyncio
import operator
import os
import sys
import time
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from common import call_llm, make_client

if os.name == "nt":
    sys.stdout.reconfigure(encoding="utf-8")

FILES = ["core/agent.py", "core/session.py", "core/team.py"]


class FanoutState(TypedDict):
    targets: list[str]
    summaries: Annotated[list[dict], operator.add]  # 各分支结果聚合


async def plan(state: FanoutState) -> dict:
    """只决定要派哪些活；真正的分发在条件边里用 Send 完成。"""
    return {"targets": FILES}


def dispatch(state: FanoutState) -> list[Send]:
    """返回多个 Send —— 每个 Send 是一次独立的 worker 调用（LangGraph 并行执行）。"""
    return [Send("worker", {"path": p}) for p in state["targets"]]


async def worker(state: dict) -> dict:
    """每个 Send 分支拿到的 state 是它自己的入参，天然隔离。"""
    path = state["path"]
    start = time.perf_counter()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        text = open(os.path.join(root, path), encoding="utf-8").read()
        head = "\n".join(text.splitlines()[:12])
        question = f"下面是一段 Python 源码开头：\n{head}\n\n用一句话说明 {path} 的职责（不超过 30 字）。"
        msg = await call_llm(make_client(), [{"role": "user", "content": question}])
        summary = msg["content"]
    except Exception as exc:  # noqa: BLE001
        summary = f"读取失败: {exc}"
    elapsed = (time.perf_counter() - start) * 1000
    print(f"  · {path} → {summary[:50]}（{elapsed:.0f} ms）")
    return {"summaries": [{"path": path, "summary": summary, "ms": round(elapsed)}]}


def build_graph():
    g = StateGraph(FanoutState)
    g.add_node("plan", plan)
    g.add_node("worker", worker)
    g.add_edge(START, "plan")
    # 条件边返回 Send 列表 → 扇出；LangGraph 会并行调度这些任务
    g.add_conditional_edges("plan", dispatch, ["worker"])
    g.add_edge("worker", END)
    return g.compile()


async def main() -> None:
    print(f"扇出 {len(FILES)} 个子任务（Send 并行）")
    start = time.perf_counter()
    result = await build_graph().ainvoke({"targets": [], "summaries": []})
    total = (time.perf_counter() - start) * 1000
    serial = sum(s["ms"] for s in result["summaries"])
    print(f"\n汇总 {len(result['summaries'])} 条：")
    for item in result["summaries"]:
        print(f"  - {item['path']}: {item['summary']}")
    print(f"\n墙钟 {total:.0f} ms｜串行累加 {serial:.0f} ms｜加速比 {serial / total:.2f}x")


if __name__ == "__main__":
    asyncio.run(main())
