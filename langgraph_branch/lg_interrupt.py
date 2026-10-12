"""框架白送的两样东西：Checkpoint（续跑 / 中断查看）与 interrupt（人工审批）。

手写版本要实现这两条得自己写：
  · Checkpoint ≈ core/session.py 的 JSONL 快照 + 分支恢复
  · interrupt   ≈ 工具执行前的 raise_if_aborted + 一个"等待外部指令"的握手

跑法：
    python langgraph_branch/lg_interrupt.py

看三件事：
  1. 同一个 thread_id 能接着上次对话问（不用重新喂历史）
  2. interrupt_before 让"危险工具执行前"停下来等人批准
  3. 恢复时用 Command(resume=...)
"""

from __future__ import annotations

import asyncio
import json
import operator
import os
import sys
from typing import Annotated, Literal, TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from common import SYSTEM, call_llm, make_client, run_tool

if os.name == "nt":
    sys.stdout.reconfigure(encoding="utf-8")

DB = os.path.join(os.path.dirname(__file__), "checkpoints.sqlite")


class AgentState(TypedDict):
    messages: Annotated[list[dict], operator.add]


async def call_model(state: AgentState) -> dict:
    msg = await call_llm(make_client(), [{"role": "system", "content": SYSTEM}, *state["messages"]])
    msg.pop("_usage", None)
    return {"messages": [msg]}


async def run_tools(state: AgentState) -> dict:
    results = []
    for call in state["messages"][-1].get("tool_calls", []):
        name = call["function"]["name"]
        args = json.loads(call["function"]["arguments"] or "{}")
        print(f"  ✔ 批准执行 {name}({args})")
        results.append(
            {"role": "tool", "tool_call_id": call["id"], "name": name, "content": await run_tool(name, args)}
        )
    return {"messages": results}


def route(state: AgentState) -> Literal["tools", "__end__"]:
    return "tools" if state["messages"][-1].get("tool_calls") else "__end__"


async def main() -> None:
    g = StateGraph(AgentState)
    g.add_node("call_model", call_model)
    g.add_node("tools", run_tools)
    g.add_edge(START, "call_model")
    g.add_conditional_edges("call_model", route, {"tools": "tools", "__end__": END})
    g.add_edge("tools", "call_model")

    async with AsyncSqliteSaver.from_conn_string(DB) as checkpointer:
        await checkpointer.setup()
        graph = g.compile(checkpointer=checkpointer, interrupt_before=["tools"])
        config = {"configurable": {"thread_id": "demo-1"}, "recursion_limit": 12}

        # --- 第一问：跑到工具执行前会停下 ---
        task = "读 sample.py，告诉我它有几个函数"
        print(f"任务：{task}")
        pending = None
        async for chunk in graph.astream(
            {"messages": [{"role": "user", "content": task}]}, config, stream_mode="updates"
        ):
            for node, value in chunk.items():
                if node == "__interrupt__" or "messages" not in value:
                    pending = value
                    print("  ⏸ 在 tools 节点前暂停（等人工批准）")
        state = await graph.aget_state(config)
        print(f"  暂停时的消息序列：{[m['role'] for m in state.values['messages']]}｜next={state.next}")
        result = await graph.ainvoke(Command(resume=True), config)
        print(f"  恢复后消息序列：{[m['role'] for m in result['messages']]}")
        answer = result["messages"][-1].get("content")
        if answer:
            print("回答：", answer)

        # --- 第二问：同一 thread_id 接着问，历史从 checkpoint 恢复 ---
        print("\n--- 同一个 thread_id 追问（历史由 checkpointer 提供）---")
        follow = "那这几个函数分别是干什么的？只用一句话概括"
        result = await graph.ainvoke({"messages": [{"role": "user", "content": follow}]}, config)
        print("回答：", result["messages"][-1]["content"])

        state = await graph.aget_state(config)
        print(f"\n线程 demo-1 此刻已有 {len(state.values['messages'])} 条消息，next={state.next}")


if __name__ == "__main__":
    asyncio.run(main())
