"""LangGraph 版最小 Agent：StateGraph + 条件边，跑同样的工具/同样的模型。

对照 core/agent.py 的手写循环 —— 两者的**拓扑完全相同**：

    START ──▶ call_model ──(有工具调用?)──▶ run_tools ──▶ call_model
                          └───── 无 ──────────────────▶ END

手写版把这个圈写在 `async def Agent.run()` 的 while 里；
LangGraph 版把它写成节点 + 条件边，由 Runtime 调度。

跑法：
    python langgraph_branch/lg_minimal_agent.py
"""

from __future__ import annotations

import asyncio
import json
import operator
import os
import sys
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from common import SYSTEM, TASK, call_llm, make_client, run_tool

if os.name == "nt":
    sys.stdout.reconfigure(encoding="utf-8")


def add_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    """自定义 reducer：用量跨 step 累加（LangGraph 里 state 字段都要声明合并方式）。

    防御非数字值：DeepSeek 的 usage.model_dump() 里 prompt_tokens_details 这类字段是 None，
    直接 `+=` 会炸——reducer 必须自己兜住 provider 的差异。
    """
    out = dict(left)
    for k, v in right.items():
        if isinstance(v, int):
            out[k] = out.get(k, 0) + v
    return out


class AgentState(TypedDict):
    messages: Annotated[list[dict], operator.add]  # 追加式合并
    usage: Annotated[dict[str, int], add_usage]


async def call_model(state: AgentState) -> dict:
    client = make_client()
    msg = await call_llm(client, [{"role": "system", "content": SYSTEM}, *state["messages"]])
    usage = msg.pop("_usage")
    return {"messages": [msg], "usage": usage or {}}


async def run_tools(state: AgentState) -> dict:
    results = []
    for call in state["messages"][-1].get("tool_calls", []):
        name = call["function"]["name"]
        args = json.loads(call["function"]["arguments"] or "{}")
        content = await run_tool(name, args)
        print(f"  · {name}({args}) → {content[:80]}")
        results.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": content})
    return {"messages": results}


def route(state: AgentState) -> Literal["tools", "__end__"]:
    """条件边：等价于手写循环里的 `if not msg.tool_calls: break`。"""
    last = state["messages"][-1]
    return "tools" if last.get("tool_calls") else "__end__"


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("call_model", call_model)
    g.add_node("tools", run_tools)
    g.add_edge(START, "call_model")
    g.add_conditional_edges("call_model", route, {"tools": "tools", "__end__": END})
    g.add_edge("tools", "call_model")
    return g.compile()


async def main() -> None:
    print(f"任务：{TASK}")
    result = await build_graph().ainvoke(
        {"messages": [{"role": "user", "content": TASK}], "usage": {}},
        config={"recursion_limit": 12},
    )
    print(f"\n消息数：{len(result['messages'])}｜用量：{result['usage']}")
    print("\n回答：", result["messages"][-1]["content"])


if __name__ == "__main__":
    asyncio.run(main())
