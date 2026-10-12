"""声明式多 Agent 团队：读 team.json → 按 entry/delegates 编排成员。

跑法：
    python demo/run_team.py "给 workspace 里的 Agent 笔记做一份 30 秒分镜"

看点：
  · 成员是子 Agent：独立会话、工具白名单、增量用量，全部复用模块二
  · 工具白名单真的生效：reviewer 只有读权限，拿不到 word_count
  · 每个成员的用量单独计量，最后汇总（顺带给出全量口径做对照）
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
from core.team import TeamRunner, load_team  # noqa: E402

WORKSPACE = ROOT / "workspace"
DEFAULT_TASK = "基于 workspace/notes.txt 的内容，做一份 30 秒、3 个镜头的文字分镜"


async def printer(bus: EventBus) -> None:
    q = bus.subscribe()
    try:
        while True:
            event = await q.get()
            data = event.data
            if event.type == "team_step_start":
                print(f"\n[{data['member']} · {data['role']}] 工具白名单 {data['tools']}")
            elif event.type == "team_step_end":
                print(f"[{data['member']}] 输出 {data['chars']} 字")
            elif event.type == "sub_agent_usage":
                print(f"   用量：+{data['total_tokens']} tokens（增量）")
    except asyncio.CancelledError:
        return


async def main() -> None:
    argv = sys.argv[1:]
    team_name = "storyboardTeam"
    if "--team" in argv:
        index = argv.index("--team")
        team_name = argv[index + 1]
        argv = argv[:index] + argv[index + 2 :]
    task = " ".join(argv) or DEFAULT_TASK
    manifest, path = load_team(team_name)
    print(f"团队：{manifest.display_name}（{manifest.name} v{manifest.version}）")
    print(f"入口：{manifest.entry}｜成员：{list(manifest.members)}")
    for name, member in manifest.members.items():
        print(f"  - {name}: tools={member.tools} delegates={member.delegates}")
    print(f"\n任务：{task}\n" + "=" * 60)

    tools, _, ctx = await create_agent_tools(cwd=str(WORKSPACE))
    llm = LLM(Settings.load())
    bus = EventBus()
    session = Session(system_prompt=build_system_prompt(str(WORKSPACE), tools))
    agent = Agent(llm=llm, session=session, tools=tools, context=ctx, bus=bus)
    manager = DelegationManager(parent=agent, llm=llm, cwd=str(WORKSPACE))
    manager.parent_tool(tools)

    runner = TeamRunner.create(team_name, llm, tools, manager, bus)

    task_printer = asyncio.create_task(printer(bus))
    result = await runner.run(task)
    await asyncio.sleep(0.05)
    task_printer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task_printer

    print("\n" + "=" * 60)
    for step in result.steps:
        print(f"{step.member:<10} {step.role:<8} 本次 {step.usage.total_tokens:>6} tokens｜累计 {step.usage_all.total_tokens:>6}")
    print("-" * 60)
    print(f"团队合计（增量）{result.usage.total_tokens} tokens｜全量口径 {result.usage_all.total_tokens} tokens")
    print(f"重复计入：{result.usage_all.total_tokens - result.usage.total_tokens} tokens")
    print("\n最终方案：\n" + result.final)


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
