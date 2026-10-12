"""同一任务、同一模型，跑「手写 agent loop」vs「LangGraph StateGraph」。

产出的是**可复现的数字**，不是观点：
  · 两边 tokens / 步骤数 / 墙钟
  · 编排层代码行数 vs 引入的依赖体积

跑法：
    python langgraph_branch/compare.py
"""

from __future__ import annotations

import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # langgraph_branch/compare.py → 项目根
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "langgraph_branch"))

if os.name == "nt":
    sys.stdout.reconfigure(encoding="utf-8")

import asyncio  # noqa: E402

from common import SYSTEM, TASK, WORKSPACE, call_llm, make_client  # noqa: E402

from core.agent import Agent, build_system_prompt  # noqa: E402
from core.config import Settings  # noqa: E402
from core.events import EventBus  # noqa: E402
from core.llm import LLM  # noqa: E402
from core.loader import TOOLS_ROOT, create_agent_tools  # noqa: E402
from core.session import Session  # noqa: E402

import json  # noqa: E402
import operator  # noqa: E402
from typing import Annotated  # noqa: E402

from langgraph.graph import END, START, StateGraph  # noqa: E402
from lg_minimal_agent import AgentState, add_usage, call_model, route, run_tools  # noqa: E402

ROUNDS = 2  # 跑两轮、交换顺序，避免缓存/预热带来的假差异


def mean(values: list[float]) -> float:
    return statistics.mean(values)


def build_langgraph() -> StateGraph:
    g = StateGraph(AgentState)
    g.add_node("call_model", call_model)
    g.add_node("tools", run_tools)
    g.add_edge(START, "call_model")
    g.add_conditional_edges("call_model", route, {"tools": "tools", "__end__": END})
    g.add_edge("tools", "call_model")
    return g.compile()


async def run_hand_written() -> dict:
    tools, _, ctx = await create_agent_tools(cwd=str(WORKSPACE), root=TOOLS_ROOT)
    llm = LLM(Settings.load())
    session = Session(system_prompt=build_system_prompt(str(WORKSPACE), tools))
    agent = Agent(llm=llm, session=session, tools=tools, context=ctx, bus=EventBus())
    start = time.perf_counter()
    answer = await agent.run(TASK)
    elapsed = time.perf_counter() - start
    usage = session.usage_total().as_dict()
    return {
        "answer": answer,
        "steps": len([m for m in session.messages if m["role"] in ("user", "assistant", "tool")]),
        "elapsed": elapsed,
        "usage": usage,
    }


async def run_langgraph() -> dict:
    graph = build_langgraph()
    start = time.perf_counter()
    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": TASK}], "usage": {}},
        config={"recursion_limit": 12},
    )
    elapsed = time.perf_counter() - start
    usage = result["usage"]
    return {
        "answer": result["messages"][-1]["content"],
        "steps": len(result["messages"]),
        "elapsed": elapsed,
        "usage": {
            # LangGraph 侧 usage 是 provider 原始字段，换算成同一口径
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


def count_lines(*paths: Path) -> int:
    return sum(
        1 for p in paths for line in p.read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#")
    )


def lib_size(prefixes: tuple[str, ...]) -> tuple[int, int]:
    site = Path(sys.prefix) / "Lib" / "site-packages"
    total, count = 0, 0
    for child in sorted(site.iterdir()):
        if not child.name.lower().startswith(prefixes):
            continue
        if child.is_dir():
            total += sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
            count += 1
    return total, count


async def main() -> None:
    print(f"任务：{TASK}\n模型：{Settings.load().model}\n")

    # 两轮、交换执行顺序：第一轮 hand→lang，第二轮 lang→hand
    results: dict[str, list[dict]] = {"hand": [], "lang": []}
    for round_index in range(ROUNDS):
        order = ["hand", "lang"] if round_index % 2 == 0 else ["lang", "hand"]
        for who in order:
            print(f"第 {round_index + 1} 轮 · {who}…")
            if who == "hand":
                results["hand"].append(await run_hand_written())
            else:
                results["lang"].append(await run_langgraph())

    def aggregate(key: str, field: str) -> int:
        return round(mean([r[field] for r in results[key]]))

    hand_usage = {k: round(mean([r["usage"][k] for r in results["hand"]])) for k in ("input_tokens", "output_tokens", "total_tokens")}
    lang_usage = {k: round(mean([r["usage"][k] for r in results["lang"]])) for k in ("input_tokens", "output_tokens", "total_tokens")}
    hand = {"usage": hand_usage, "steps": aggregate("hand", "steps"), "elapsed": mean([r["elapsed"] for r in results["hand"]]),
            "answer": results["hand"][-1]["answer"]}
    lang = {"usage": lang_usage, "steps": aggregate("lang", "steps"), "elapsed": mean([r["elapsed"] for r in results["lang"]]),
            "answer": results["lang"][-1]["answer"]}

    print("\n=== 同一任务的实测 ===")
    rows = [
        ("步骤数（消息条数）", hand["steps"], lang["steps"]),
        ("input tokens", hand["usage"]["input_tokens"], lang["usage"]["input_tokens"]),
        ("output tokens", hand["usage"]["output_tokens"], lang["usage"]["output_tokens"]),
        ("total tokens", hand["usage"]["total_tokens"], lang["usage"]["total_tokens"]),
        ("墙钟 (s)", round(hand["elapsed"], 2), round(lang["elapsed"], 2)),
    ]
    print(f"{'指标':<22}{'手写':>14}{'LangGraph':>14}")
    for label, a, b in rows:
        print(f"{label:<22}{str(a):>14}{str(b):>14}")

    core_files = sorted((ROOT / "core").glob("*.py"))
    lg_files = [ROOT / "langgraph_branch" / "lg_minimal_agent.py", ROOT / "langgraph_branch" / "common.py"]
    core_lines = count_lines(*core_files)
    lg_lines = count_lines(*lg_files)
    lg_size, lg_count = lib_size(("langgraph", "langchain"))

    # 公平口径：只比"等价最小循环"这一层
    loop_lines = count_lines(ROOT / "core" / "agent.py")

    print("\n=== 成本 ===")
    print(f"{'指标':<22}{'手写 (core/*.py)':>20}{'LangGraph':>18}")
    print(f"{'整个运行时行数':<22}{core_lines:>20}{lg_lines:>18}")
    print(f"{'等价最小循环行数':<22}{loop_lines:>20}{count_lines(ROOT / 'langgraph_branch' / 'lg_minimal_agent.py'):>18}")
    print(f"{'运行时依赖':<22}{'openai+pydantic':>20}{f'langgraph 等 {lg_count} 个包':>18}")
    print(f"{'依赖体积':<22}{'-':>20}{f'{lg_size / 1024 / 1024:.1f} MB':>18}")

    print("\n=== 能不能拿到这些能力 ===")
    caps = [
        ("框架自带 checkpoint 续跑", "要自己写（core/session.py 快照）", "内置 SqliteSaver/Command 恢复"),
        ("工具前人工审批", "要自己写握手", "内置 interrupt_before + Command"),
        ("并发扇出分发", "asyncio.gather / TaskGroup", "Send"),
        ("Token 归因/分层聚合", "可分层归因（delegator/worker/expert）", "需自行在 state reducer 累加"),
    ]
    for cap, a, b in caps:
        print(f"· {cap}\n    手写：{a}\n    LangGraph：{b}")

    print("\n=== 回答对比 ===")
    print("手写：", hand["answer"][:200].replace("\n", " "))
    print("LangGraph：", lang["answer"][:200].replace("\n", " "))

    # 落盘，方便写进笔记
    out = {
        "task": TASK,
        "model": Settings.load().model,
        "hand": {k: v for k, v in hand.items() if k != "answer"},
        "langgraph": {k: v for k, v in lang.items() if k != "answer"},
        "core_lines": core_lines,
        "lg_lines": lg_lines,
        "lg_deps_mb": round(lg_size / 1024 / 1024, 1),
        "lg_dep_count": lg_count,
    }
    path = ROOT / "langgraph_branch" / "compare_result.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入 {path}")


if __name__ == "__main__":
    asyncio.run(main())
