"""LangGraph 版子 Agent 委派：子图（subgraph）+ 独立 thread + 取消级联 + 增量归因。

对照主线：
  · core/subagent.py     —— 子 Agent 独立会话 + asyncio.Task 取消级联
  · core/usage.py        —— previousEntries 偏移，只汇总本次新增
  · demo/cancel_cascade.py / demo/usage_experiment.py

这个脚本回答两个具体问题：
  Q1 框架有没有替你解决"父中止 → 子中止"？   （答案在输出里）
  Q2 框架能不能做"增量 token 归因"？         （用 checkpoint history 数步数）

跑法：
    python langgraph_branch/lg_subagent.py
"""

from __future__ import annotations

import asyncio
import json
import operator
import os
import sys
import time
import uuid
from typing import Annotated, Literal, TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from common import SYSTEM, TOOLS, call_llm, make_client, run_tool
from lg_minimal_agent import add_usage

if os.name == "nt":
    sys.stdout.reconfigure(encoding="utf-8")

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkpoints.sqlite")
ROOT_TASK = "用 subagent 工具查清 sample.py 里有哪些函数，然后用一句话汇总结果"
CHILD_TASK = "读 sample.py，列出其中所有函数名（只列名字）"

SUBAGENT_TOOL = {
    "type": "function",
    "function": {
        "name": "subagent",
        "description": "派生一个子 Agent 执行独立任务（它有自己独立的会话与上下文），完成后拿到它的结论",
        "parameters": {
            "type": "object",
            "properties": {"task": {"type": "string", "description": "交给子 Agent 的任务"}},
            "required": ["task"],
            "additionalProperties": False,
        },
    },
}


class AgentState(TypedDict):
    messages: Annotated[list[dict], operator.add]
    usage: Annotated[dict[str, int], add_usage]


# ---------------------------------------------------------------------------
# 子 Agent：一个可独立调用的 graph（对应"子会话"）
# ---------------------------------------------------------------------------
async def child_call_model(state: AgentState) -> dict:
    msg = await call_llm(make_client(), [{"role": "system", "content": SYSTEM}, *state["messages"]])
    usage = msg.pop("_usage")
    return {"messages": [msg], "usage": usage}


async def child_tools(state: AgentState) -> dict:
    results = []
    for call in state["messages"][-1].get("tool_calls", []):
        name = call["function"]["name"]
        args = json.loads(call["function"]["arguments"] or "{}")
        print(f"      [子] {name}({args})")
        results.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": await run_tool(name, args)})
    return {"messages": results}


def child_route(state: AgentState) -> Literal["tools", "__end__"]:
    return "tools" if state["messages"][-1].get("tool_calls") else "__end__"


def build_child(checkpointer):
    g = StateGraph(AgentState)
    g.add_node("call_model", child_call_model)
    g.add_node("tools", child_tools)
    g.add_edge(START, "call_model")
    g.add_conditional_edges("call_model", child_route, {"tools": "tools", "__end__": END})
    g.add_edge("tools", "call_model")
    return g.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# 父 Agent：tools 节点里遇到 subagent 调用就 await 子图（父→子通道）
# ---------------------------------------------------------------------------
def build_parent(child_graph, checkpointer, on_child_start):
    parent_tools = [*TOOLS, SUBAGENT_TOOL]

    async def call_model(state: AgentState) -> dict:
        msg = await call_llm(make_client(), [{"role": "system", "content": SYSTEM}, *state["messages"]], tools=parent_tools)
        usage = msg.pop("_usage")
        return {"messages": [msg], "usage": usage}

    async def tools_node(state: AgentState) -> dict:
        results: list[dict] = []
        for call in state["messages"][-1].get("tool_calls", []):
            name = call["function"]["name"]
            args = json.loads(call["function"]["arguments"] or "{}")
            if name == "subagent":
                thread = on_child_start()
                print(f"    [父] 委派子 Agent → thread={thread}")
                started = time.perf_counter()
                before = await snapshot(child_graph, thread)
                sub = await child_graph.ainvoke(
                    {"messages": [{"role": "user", "content": args["task"]}], "usage": {}},
                    config={"configurable": {"thread_id": thread}, "recursion_limit": 12},
                )
                after = await snapshot(child_graph, thread)
                child_usage = usage_delta(after["usage"], before["usage"])
                elapsed = time.perf_counter() - started
                # 子 → 父的"上报"：结论 + 它自己的用量（对应 report 工具要传的东西）
                content = json.dumps(
                    {
                        "report": sub["messages"][-1]["content"],
                        "child_usage": child_usage,
                        "child_steps": after["steps"] - before["steps"],
                        "elapsed_s": round(elapsed, 2),
                    },
                    ensure_ascii=False,
                )
            else:
                content = await run_tool(name, args)
            results.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": content})
        return {"messages": results}

    def route(state: AgentState) -> Literal["tools", "__end__"]:
        return "tools" if state["messages"][-1].get("tool_calls") else "__end__"

    g = StateGraph(AgentState)
    g.add_node("call_model", call_model)
    g.add_node("tools", tools_node)
    g.add_edge(START, "call_model")
    g.add_conditional_edges("call_model", route, {"tools": "tools", "__end__": END})
    g.add_edge("tools", "call_model")
    return g.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# 用 checkpoint history 做"增量归因"（对应手写的 previousEntries 偏移）
# ---------------------------------------------------------------------------
def usage_delta(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
    """增量 = 后偏移 − 前偏移（对应手写里的 previousEntries 技巧）。"""
    keys = set(after) | set(before)
    return {k: after.get(k, 0) - before.get(k, 0) for k in keys}


async def snapshot(graph, thread_id) -> dict:
    """读某个 thread 当前的累计用量与步数 —— 就是"起始偏移"的取法。"""
    state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    values = state.values or {}
    return {
        "usage": values.get("usage", {}),
        "steps": (state.metadata or {}).get("step", 0),
    }


class ThreadNames:
    """**thread_id 必须每次运行唯一**。

    踩过的坑：复用固定 thread_id 时，LangGraph 会从 checkpoint 恢复上一次的历史，
    新一轮的 user 消息被追加在旧消息后面 —— 旧 assistant(tool_calls) 找不到对应 tool 消息，
    provider 直接 400。所以生产里 thread_id 要带会话/运行标识，不能写死。
    """

    def __init__(self) -> None:
        self.run = uuid.uuid4().hex[:6]
        self.parent = ""
        self.n = 0

    def parent_tid(self, tag: str) -> str:
        self.parent = f"parent-{tag}-{self.run}"
        self.n = 0
        return self.parent

    def child_tid(self) -> str:
        self.n += 1
        return f"{self.parent}/sub-{self.n}"


async def main() -> None:
    names = ThreadNames()
    child_threads: list[str] = []

    def alloc_child() -> str:
        tid = names.child_tid()
        child_threads.append(tid)
        return tid

    async with AsyncSqliteSaver.from_conn_string(DB) as checkpointer:
        await checkpointer.setup()
        child_graph = build_child(checkpointer)
        parent_graph = build_parent(child_graph, checkpointer, alloc_child)
        tid = names.parent_tid("main")
        config = {"configurable": {"thread_id": tid}, "recursion_limit": 12}

        # ---------- 场景一：正常委派 + 增量归因 ----------
        print(f"=== 场景一：正常委派（thread={tid}）===")
        before = await snapshot(parent_graph, tid)
        started = time.perf_counter()
        result = await parent_graph.ainvoke(
            {"messages": [{"role": "user", "content": ROOT_TASK}], "usage": {}}, config
        )
        elapsed = time.perf_counter() - started
        after = await snapshot(parent_graph, tid)

        delta_parent = usage_delta(after["usage"], before["usage"])
        child_state = await snapshot(child_graph, child_threads[-1])
        print(f"  父线程累计（若直接上报）: {after['usage']}")
        print(f"  父线程本次新增（增量）  : {delta_parent}")
        print(f"  子线程累计（本次会话全部）: {child_state['usage']}｜step={child_state['steps']}")
        print(f"  墙钟 {elapsed:.2f}s｜父图 step={after['steps']}")
        print("  最终回答：", result["messages"][-1]["content"][:120].replace("\n", " "))

        # ---------- 场景二：父中止 → 子中止 ----------
        print("\n=== 场景二：委派进行到一半，取消父 Task ===")
        cancel_tid = names.parent_tid("cancel")
        slow = "读 sample.py 和 core/agent.py，各自用一句话概括"
        task = asyncio.create_task(
            parent_graph.ainvoke(
                {"messages": [{"role": "user", "content": f"用 subagent 工具执行：{slow}"}], "usage": {}},
                {"configurable": {"thread_id": cancel_tid}, "recursion_limit": 12},
            )
        )
        # 关键：必须等"委派真的开始"再掐，否则测的是"取消一个还没委派的父图"
        baseline = len(child_threads)
        for _ in range(80):
            if len(child_threads) > baseline:
                break
            await asyncio.sleep(0.25)
        else:
            print("  ! 等待委派超时")
        print(f"  委派已开始（{child_threads[-1]}），子 Agent 执行中…")
        await asyncio.sleep(1.2)  # 让子图确实停在某个 await 上
        print("  ✂ 父 Task.cancel()")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            print("  父图因取消退出（CancelledError）")
        except Exception as exc:  # noqa: BLE001
            print(f"  父图退出：{type(exc).__name__}: {exc}")

        sub_tid = child_threads[-1]
        child_after = await snapshot(child_graph, sub_tid)
        child_msgs = (await child_graph.aget_state({"configurable": {"thread_id": sub_tid}})).values.get(
            "messages", []
        )
        print(f"  子线程 {sub_tid}：累计用量 {child_after['usage']}｜step={child_after['steps']}")
        print(f"  子线程消息序列：{[m['role'] for m in child_msgs]}（没有最终 assistant 回答 = 被掐断）")


if __name__ == "__main__":
    asyncio.run(main())
