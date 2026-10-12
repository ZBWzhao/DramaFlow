"""会话级不变量测试（纯本地，不调模型、不花 API 费用）。

背景：审计后端 Agent 代码时发现的问题都属同一类 —— **状态被留下、错误被藏起来**：
  · 取消信号设了不清 → 会话"停一次就永久死"
  · 同一会话可以并发跑两次 → 事件互相串、历史交错
  · 单次请求的开关（use_sub_agent）**就地改**了会话的工具集 → 之后的请求也少了工具

这三条都不是"跑一次能不能出结果"的问题，而是"用久了会不会莫名坏掉"的问题，
所以用不变量（invariant）来表达：无论前面发生过什么，这些性质都必须成立。

跑法：python demo/session_invariants.py
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import Settings  # noqa: E402
from core.llm import LLM  # noqa: E402
from server.registry import SessionRegistry  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, extra: str = "") -> None:
    print(f"{'✓' if ok else '✗'} {label}{f'　{extra}' if extra else ''}")
    if not ok:
        failures.append(label)


async def main() -> None:
    registry = SessionRegistry(llm=LLM(Settings.load()), workspace=str(ROOT / "workspace"))
    await registry.ainit()

    # ---------- 1. 取消是"这一次运行"的事，不能污染下一次 ----------
    live = registry.get_or_create("inv-cancel")
    was = registry.cancel("inv-cancel")
    check("取消一个存在的会话会成功", was is True)
    check("取消后信号是置位的", live.cancel.is_set() is True)

    again = registry.get_or_create("inv-cancel")
    registry.begin_run(again)  # 新的一次运行开始（服务端每次请求都会走这一步）
    check(
        "★ 取消后新的一次运行必须可跑（begin_run 要清掉取消信号）",
        again.cancel.is_set() is False,
        f"cancel.is_set()={again.cancel.is_set()} ← True 时这次运行会在第一个检查点被中止",
    )

    # 更贴近真实后果：直接跑一下运行循环里的第一个检查点
    from core.agent import AgentAborted  # noqa: E402

    aborted_at_checkpoint = False
    try:
        again.agent._check_abort()
    except AgentAborted:
        aborted_at_checkpoint = True
    check("★ 取消之后，agent.py 的第一个检查点不会中止", not aborted_at_checkpoint)

    # ---------- 2. 同一会话不允许并发两次运行 ----------
    live2 = registry.get_or_create("inv-busy")
    has_begin = hasattr(registry, "begin_run")
    check("存在 begin_run（每次运行的入口守卫）", has_begin, "缺这个守卫时两次运行会往同一个事件总线里交错发事件")
    if has_begin:
        live2.task = asyncio.create_task(asyncio.sleep(30))  # 模拟"还有一次运行没结束"
        busy_rejected = False
        try:
            registry.begin_run(live2)
        except RuntimeError as exc:
            busy_rejected = "already" in str(exc).lower() or "运行中" in str(exc)
        check("★ 会话上已有运行时，第二次 begin_run 被拒", busy_rejected)
        live2.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await live2.task
        live2.task = None
        registry.begin_run(live2)
        check("上一次结束后可以再次开始", live2.cancel.is_set() is False)

    # ---------- 3. 单次请求的开关不能永久改变会话的工具集 ----------
    live3 = registry.get_or_create("inv-tools")
    has_tools_for = hasattr(live3, "tools_for")
    check("LiveSession 有 tools_for(include_sub_agent=…)", has_tools_for, "就地改 agent.tools 会让开关粘住")
    if has_tools_for:
        with_sub = {t.name for t in live3.tools_for(True)}
        without = {t.name for t in live3.tools_for(False)}
        restored = {t.name for t in live3.tools_for(True)}
        check("关掉 subagent 时确实少了它", "subagent" in with_sub and "subagent" not in without)
        check("★ 再打开时必须恢复（不残留上一次的选择）", restored == with_sub, f"恢复后差集={with_sub ^ restored}")
        check("两种形态都不为空", bool(with_sub) and bool(without))

    # ---------- 4. 统计信息要能反映会话真实状态 ----------
    stats = registry.stats("inv-tools")
    check("stats 能报出工具集（便于诊断工具变少这类问题）", isinstance(stats.get("tools"), list) and bool(stats["tools"]))

    # ---------- 5/6. 落盘再恢复，必须与内存一致 ----------
    # 这条不变量很关键：文件是唯一的真相来源，但**内存里的"活动历史"更小**
    # （压缩过 / 分叉过）。恢复时不还原这两件事，重启后上下文就会膨胀回去，
    # 甚至把被废弃分支的消息一起带回来。
    tmp = ROOT / "workspace" / "_invariant_sessions"
    tmp.mkdir(parents=True, exist_ok=True)

    def fingerprint(session) -> list[tuple]:
        return [(m.get("role"), str(m.get("content"))[:24]) for m in session.messages]

    # 5) 压缩后恢复
    from core.session import Session  # noqa: E402

    compact_path = tmp / "compact.jsonl"
    compact_path.unlink(missing_ok=True)
    s = Session(session_id="compact", path=compact_path, system_prompt="SYS")
    for index in range(8):
        s.append({"role": "user" if index % 2 == 0 else "assistant", "content": f"第{index}条消息"})
    before_chars = s.size_chars()
    s.replace_with_summary("这是摘要", 2)
    after_chars = s.size_chars()
    rel = Session.load(compact_path)
    check("压缩确实让上下文变小", after_chars < before_chars, f"{before_chars} → {after_chars}")
    check(
        "★ 重启恢复后仍是压缩后的历史（不是把原始历史装回来）",
        fingerprint(rel) == fingerprint(s),
        f"恢复 {len(rel.messages)} 条 vs 内存 {len(s.messages)} 条",
    )
    check("恢复后没有把摘要重复两遍", sum(1 for m in rel.messages if "之前的对话摘要" in str(m.get("content"))) == 1)

    # 6) 分叉后恢复
    branch_path = tmp / "branch.jsonl"
    branch_path.unlink(missing_ok=True)
    b = Session(session_id="branch", path=branch_path, system_prompt="SYS")
    first = b.append({"role": "user", "content": "第一条"})
    b.append({"role": "assistant", "content": "被废弃的回复"})
    b.branch_from(first)  # 截断到第一条
    b.append({"role": "assistant", "content": "新分支的回复"})
    rb = Session.load(branch_path)
    check(
        "★ 重启恢复后不带上被废弃分支的消息",
        fingerprint(rb) == fingerprint(b),
        f"恢复 {[c for _, c in fingerprint(rb)]} vs 内存 {[c for _, c in fingerprint(b)]}",
    )

    # 清理测试用会话文件
    for item in tmp.glob("*.jsonl"):
        item.unlink(missing_ok=True)
    tmp.rmdir()

    print()
    if failures:
        print(f"✗ {len(failures)} 项未通过：")
        for item in failures:
            print(f"   · {item}")
    else:
        print("✓ 全部通过")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
