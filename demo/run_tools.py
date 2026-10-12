"""模块一验收：插件化工具系统 + ToolContext 端到端。

跑法：
    python demo/run_tools.py
    python demo/run_tools.py "你的问题"

它依次做四件事：
  1. 静态解析元数据（list_tools）—— 不执行工具代码就能拿到清单
  2. 动态装载 + 配置校验（create_agent_tools）
  3. 沙箱自测：越界 / 符号链接必须被拦住（纯本地，不花钱）
  4. 让模型真的调用一次自研工具 word_count
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
from core.context import CapabilityUnavailable, SandboxError, ToolContext  # noqa: E402
from core.events import EventBus  # noqa: E402
from core.llm import LLM  # noqa: E402
from core.loader import create_agent_tools, list_tools  # noqa: E402
from core.session import Session  # noqa: E402

WORKSPACE = ROOT / "workspace"


def section(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def show_metadata() -> None:
    section("1. 元数据静态解析（list_tools）")
    for entry in list_tools():
        if entry.load_error:
            print(f"  ✗ {entry.name}: {entry.load_error}")
            continue
        meta = entry.metadata
        print(f"  ✓ {meta.name:<12} v{meta.version}  {meta.display_name}")
        print(f"      description: {meta.description[:60]}")
        if meta.prompt:
            print(f"      prompt: {meta.prompt[:60]}")


def sandbox_self_test() -> None:
    section("2. 路径沙箱自测（不调模型）")
    ctx = ToolContext(cwd=str(WORKSPACE))

    cases = [
        ("正常相对路径", "README.md", True),
        ("目录穿越", "../requirements.txt", False),
        ("跳到系统目录", str(Path.home()), False),
        ("工作区根自身允许", ".", True),
    ]
    for label, path, ok in cases:
        try:
            resolved = ctx.resolve_path(path)
            print(f"  {'✓' if ok else '✗ 没拦住!'} {label:<16} → {resolved}")
        except SandboxError as exc:
            print(f"  {'✗ 不该拦' if ok else '✓ 已拦截'} {label:<16} → {exc}")

    # 符号链接逃逸：造一个指向工作区外的链接（Windows 上若没权限就跳过）
    link = WORKSPACE / "_escape_link.txt"
    try:
        if link.exists() or link.is_symlink():
            link.unlink()
        os.symlink(ROOT / "requirements.txt", link)
    except (OSError, NotImplementedError) as exc:
        print(f"  - 跳过符号链接测试（无法创建：{exc}）")
        return
    try:
        ctx.resolve_path("_escape_link.txt")
        print("  ✗ 没拦住! 符号链接逃逸")
    except SandboxError as exc:
        print(f"  ✓ 已拦截 符号链接逃逸 → {exc}")
    finally:
        if link.is_symlink():
            link.unlink()


def capability_demo() -> None:
    """能力位：默认未接入 → 注入一个最小实现后立即可用（证明"可扩展"不是口号）。"""
    section("2.5 ToolContext 能力位（对应 Toonflow 的 media/question/canvas）")
    ctx = ToolContext(cwd=str(WORKSPACE))
    print(f"  默认：{ctx.capabilities()}")
    try:
        ctx.require_media()
    except CapabilityUnavailable as exc:
        print(f"  未接入时调用 → 明确报错：{str(exc)[:60]}…")

    class DemoMedia:  # 只是演示能力位可插拔，不产生任何真实媒体
        def list_models(self) -> list[dict]:
            return [{"id": "demo-model", "kind": "image"}]

        def generate(self, kind: str, request: dict) -> dict:
            return {"kind": kind, "ok": True, "echo": request}

    ctx.media = DemoMedia()
    print(f"  注入后：{ctx.capabilities()}")
    print(f"  require_media().list_models() → {ctx.require_media().list_models()}")


async def printer(bus: EventBus) -> None:
    """把事件打印出来（真实系统里这是 HTTP 流 / 前端渲染）。"""
    q = bus.subscribe()
    try:
        while True:
            event = await q.get()
            if event.type == "message_update":
                print(event.data.get("delta", ""), end="", flush=True)
            elif event.type == "tool_execution_start":
                print(f"\n  [工具] {event.data['tool']}({event.data.get('args', '')[:80]})")
            elif event.type == "tool_execution_end":
                print(f"  [结果] isError={event.data.get('isError')}")
            elif event.type == "usage":
                print(f"  [用量] +{event.data.get('total_tokens')} tokens")
    except asyncio.CancelledError:
        return


async def main() -> None:
    question = " ".join(sys.argv[1:]) or (
        "用 word_count 统计 workspace 下所有 .md 和 .txt 文件的规模，"
        "告诉我总行数、总字符数和出现最多的几个词"
    )

    show_metadata()
    sandbox_self_test()
    capability_demo()

    section("3. 动态装载工具插件")
    tools, entries, ctx = await create_agent_tools(cwd=str(WORKSPACE), config={"word_count": {"max_files": 100}})
    print(f"  装载成功：{[t.name for t in tools]}")
    for entry in entries:
        print(f"    - {entry.name}: {'启用' if entry.enabled else '禁用'}")

    section(f"4. 跑 agent：{question}")
    bus = EventBus()
    session = Session(system_prompt=build_system_prompt(str(WORKSPACE), tools))
    agent = Agent(llm=LLM(Settings.load()), session=session, tools=tools, context=ctx, bus=bus)

    task = asyncio.create_task(printer(bus))
    try:
        answer = await agent.run(question)
    finally:
        await asyncio.sleep(0.05)  # 让队列里剩下的事件打完
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    section("结果")
    print(answer)
    print(f"\n本次用量：{session.usage_total().as_dict()}")


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
