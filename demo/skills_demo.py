"""技能系统：清单常驻上下文，正文按需读。

跑法：
    python demo/skills_demo.py

看三件事：
  1. 提示词里只有 name/description/location，正文不在上下文里
  2. 模型判断相关后**先调 read_skill 读正文**，再按正文执行
  3. 量化：清单段字符数 vs 全部正文字符数（省了多少上下文）
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
from core.skills import SKILLS_ROOT, build_skill_tools, discover_skills, read_skill_file, skills_prompt_section  # noqa: E402

WORKSPACE = ROOT / "workspace"
TASK = "给 workspace/sample.py 写一段 15 秒、2 个镜头的文字分镜，画面要具体"


async def main() -> None:
    entries, errors = discover_skills(SKILLS_ROOT)
    print(f"装载技能 {len(entries)} 个｜错误 {len(errors)}")
    for entry in entries:
        print(f"  - {entry.name}: {entry.description}")
        print(f"    位置 {entry.location}｜正文 {entry.body_chars} 字")
    for err in errors:
        print(f"  ! {err}")

    section = skills_prompt_section(entries)
    bodies = sum(read_skill_file(e.name, "SKILL.md", SKILLS_ROOT).__len__() for e in entries)
    print("\n--- 上下文开销对比 ---")
    print(f"清单段（常驻每轮请求）      : {len(section)} 字")
    print(f"全部正文（若一次性塞进去）  : {bodies} 字")
    print(f"常驻开销只占 {len(section) / max(bodies, 1):.1%}")

    tools, _, ctx = await create_agent_tools(cwd=str(WORKSPACE))
    tools += build_skill_tools(SKILLS_ROOT)
    llm = LLM(Settings.load())
    bus = EventBus()
    session = Session(system_prompt=build_system_prompt(str(WORKSPACE), tools, entries))
    agent = Agent(llm=llm, session=session, tools=tools, context=ctx, bus=bus)

    calls: list[str] = []

    async def watcher() -> None:
        q = bus.subscribe()
        try:
            while True:
                event = await q.get()
                if event.type == "tool_execution_start":
                    name = event.data.get("tool", "?")
                    args = event.data.get("args", "")
                    calls.append(name)
                    print(f"  · 调用 {name}({args})")
        finally:
            bus.unsubscribe(q)

    watch_task = asyncio.create_task(watcher())
    print(f"\n--- 任务：{TASK}")
    answer = await agent.run(TASK)
    await asyncio.sleep(0.05)
    watch_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await watch_task

    print(f"\n工具调用顺序：{calls}")
    print(f"是否先读正文：{'是' if 'read_skill' in calls else '否（错误行为）'}")
    print(f"本次用量：{session.usage_total().as_dict()}")
    print("\n回答：\n" + answer)


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
