"""P0 验收：一句创意 → Agent 自主建出短剧生产图 → 打印图与可执行顺序。

跑法：
    python demo/drama_demo.py                 # 用默认创意
    python demo/drama_demo.py "你的创意"       # 换创意
    python demo/drama_demo.py --canvas 第1集   # 换画布名

验收点（对应 ROADMAP 的 P0 门禁）：
  1. Agent 自主建出 ≥6 个节点、≥5 条边
  2. 图通过环检测，能算出拓扑顺序
  3. 画布文件落盘在工作区（前端可直接读它渲染）
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from core.agent import Agent, build_system_prompt  # noqa: E402
from core.canvas import Canvas, CanvasError, canvas_path  # noqa: E402
from core.config import Settings  # noqa: E402
from core.events import EventBus  # noqa: E402
from core.llm import LLM  # noqa: E402
from core.loader import create_agent_tools  # noqa: E402
from core.session import Session  # noqa: E402
from core.skills import SKILLS_ROOT, build_skill_tools, discover_skills  # noqa: E402

WORKSPACE = ROOT / "workspace"
IDEA = "外卖员深夜送单到便利店，发现柜台后的店员是他失散多年的姐姐"
TASK = (
    f"把下面这个创意做成短剧生产图，总时长 60 秒：{IDEA}\n"
    "要求：先写剧本（3 场以内），再拆成 3 个片段的分镜（每镜 6 字段），"
    "给出现的角色和场景建资产节点，最后为每个片段生成一份提示词。"
    "只建图与写正文，不要生成图片或视频。做完用一段话说明你建了什么。"
)

# 修复模式：拿已有画布做"审校 + 自纠"，验证「校验发现问题 → 模型自己改」这条闭环
REPAIR_TASK = (
    "画布已经建好了，现在只做提示词的审校与修正（不要动图结构、不要加删节点）：\n"
    "1. 用 get_canvas 读出每个提示词节点的『可用引用』（形如 {{ref 1}}=角色·陈默）\n"
    "2. 读该节点的正文：如果里面的 {{ref N}} 与可用引用对不上（编号错、或引用了没连上本片段的资产），"
    "就用 set_text 重写正文修正编号\n"
    "3. 正文里不要自己写『【参考N】= ……』这类声明（导出时系统会依据资产节点自动生成）\n"
    "4. 全部改完后调用 export_prompt 导出，并报告还剩几个未展开编号（应为 0）"
)


async def main() -> None:
    canvas_name = "main"
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    repair = "--repair" in sys.argv
    if "--canvas" in sys.argv:
        canvas_name = sys.argv[sys.argv.index("--canvas") + 1]
    task_idea = args[0] if args else IDEA

    tools, _, ctx = await create_agent_tools(cwd=str(WORKSPACE))
    skills, skill_errors = discover_skills(SKILLS_ROOT)
    tools += build_skill_tools(SKILLS_ROOT)
    for err in skill_errors:
        print(f"! 技能装载告警：{err}")

    llm = LLM(Settings.load())
    bus = EventBus()
    session = Session(system_prompt=build_system_prompt(str(WORKSPACE), tools, skills))
    agent = Agent(llm=llm, session=session, tools=tools, context=ctx, bus=bus, max_turns=16)

    calls: list[str] = []

    async def watcher() -> None:
        q = bus.subscribe()
        try:
            while True:
                event = await q.get()
                if event.type == "tool_execution_start":
                    name = event.data.get("tool", "?")
                    calls.append(name)
                    detail = json.dumps(event.data.get("args", {}), ensure_ascii=False)
                    print(f"  · {name}({detail[:110]})")
                elif event.type == "tool_execution_end" and event.data.get("isError"):
                    print("    ⚠️ 工具报错（模型会看到并自我修正）")
        finally:
            bus.unsubscribe(q)

    async def run_agent() -> str:
        if repair:
            return await agent.run(REPAIR_TASK)
        return await agent.run(TASK if not args else f"把这个创意做成短剧生产图：{task_idea}")

    mode = "审校修正" if repair else "建图"
    print(f"模式：{mode}｜创意：{task_idea}\n画布：workspace/{canvas_name}.canvas.json\n")
    watch = asyncio.create_task(watcher())
    try:
        answer = await run_agent()
    finally:
        await asyncio.sleep(0.05)
        watch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch

    print(f"\n工具调用序列（{len(calls)} 次）：{calls}")

    path = canvas_path(WORKSPACE, canvas_name)
    print(f"\n{'=' * 60}\n画布落盘检查\n{'=' * 60}")
    if not path.exists():
        print(f"✗ 没有找到画布文件：{path}")
        return
    canvas = Canvas.load(path, canvas_name)
    print(f"文件：{path.name}（{path.stat().st_size} 字节）")
    print(f"节点 {len(canvas.nodes)} 个｜边 {len(canvas.edges)} 条")

    types: dict[str, int] = {}
    for node in canvas.nodes.values():
        types[node.type] = types.get(node.type, 0) + 1
    print(f"节点类型分布：{types}")

    print("\n--- 画布摘要（工具返回给模型的就是它）---")
    print(canvas.summary())

    print(f"\n{'=' * 60}\nP0 门禁\n{'=' * 60}")
    checks = [
        (f"节点 ≥ 6（实际 {len(canvas.nodes)}）", len(canvas.nodes) >= 6),
        (f"边 ≥ 5（实际 {len(canvas.edges)}）", len(canvas.edges) >= 5),
        ("四类节点齐全（text/storyboard/asset/prompt）", len(types) >= 4),
    ]
    try:
        order = canvas.topo_order()
        checks.append((f"通过环检测，拓扑序长度 {len(order)}", True))
    except CanvasError as exc:
        checks.append((f"环检测失败：{exc}", False))
    problems = canvas.validate()
    checks.append((f"体检无阻断问题（{len(problems)} 条）", not problems))
    checks.append(("正文已落盘 assets/", any("content_path" in n.data for n in canvas.nodes.values())))

    # 提示词展开质量：{{ref N}} 必须都能对应到连线上的资产
    prompts = [n for n in canvas.nodes.values() if n.type == "prompt"]
    if prompts:
        leftovers = {
            n.label: canvas.render_prompt(n.id)["leftovers"] for n in prompts if canvas.render_prompt(n.id)["leftovers"]
        }
        checks.append(
            (f"提示词编号全部可展开（{len(prompts)} 个片段，未展开 {sum(len(v) for v in leftovers.values())} 处）",
             not leftovers)
        )
        if leftovers:
            for label, items in leftovers.items():
                print(f"    · {label}: {items}")

    for label, ok in checks:
        print(f"  {'✓' if ok else '✗'} {label}")
    if problems:
        print("\n体检问题：")
        for problem in problems[:6]:
            print(f"  - {problem}")

    print(f"\n用量：{session.usage_total().as_dict()}｜工具调用 {len(calls)} 次")
    print(f"\nAgent 结论：\n{answer[:600]}")


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
