"""副作用工具幂等与人工审批参数绑定的无网络回归测试。"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.agent import Agent  # noqa: E402
from core.context import ToolContext  # noqa: E402
from core.invocation_store import (  # noqa: E402
    InMemoryApprovalStore,
    InMemoryToolInvocationStore,
    canonical_hash,
    new_approval,
)
from core.loader import create_agent_tools  # noqa: E402
from core.plugin import define_tool  # noqa: E402
from core.session import Session  # noqa: E402


class WriteArgs(BaseModel):
    value: str


async def main() -> None:
    invocations = InMemoryToolInvocationStore()
    approvals = InMemoryApprovalStore()
    await invocations.start()
    await approvals.start()

    loaded_tools, _, _ = await create_agent_tools(str(ROOT / "workspace"))
    side_effects = {item.name: item.side_effect for item in loaded_tools}
    assert side_effects["write_file"] and side_effects["add_node"] and side_effects["run_canvas"]
    assert not side_effects["read_file"], "只读工具不应进入副作用调用状态机"

    calls = 0

    def write_once(args: WriteArgs, _ctx: ToolContext) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"saved": args.value}

    tool = define_tool(
        "write_once",
        "测试副作用工具",
        WriteArgs,
        write_once,
        side_effect=True,
    )
    session = Session(session_id="invocation-test")
    agent = Agent(
        llm=None,  # type: ignore[arg-type]
        session=session,
        tools=[tool],
        context=ToolContext(cwd=str(ROOT / "workspace")),
        invocations=invocations,
    )

    first = await agent._execute_one(tool, {"value": "A"}, tool.name, "call-1")
    second = await agent._execute_one(tool, {"value": "A"}, tool.name, "call-1")
    assert calls == 1, "同一个 tool_call_id 不得重复产生副作用"
    assert first[2] is False and second[2] is False
    assert second[1]["idempotent_replay"] is True

    conflict = await agent._execute_one(tool, {"value": "B"}, tool.name, "call-1")
    assert conflict[2] is True and "参数不同" in conflict[0]
    assert calls == 1

    payload = {"canvas": "story-a", "provider": "mock", "jobs": [{"node_id": "v1"}]}
    approval = new_approval("canvas.run:story-a", payload, ttl_seconds=60)
    await approvals.create(approval)
    consumed = await approvals.consume(approval.approval_id, approval.scope, canonical_hash(payload))
    assert consumed.status == "consumed"
    replay = await approvals.consume(approval.approval_id, approval.scope, canonical_hash(payload))
    assert replay.status == "consumed", "完全相同的 HTTP 重试应继续命中幂等结果"
    try:
        await approvals.consume(approval.approval_id, approval.scope, canonical_hash({**payload, "provider": "real"}))
    except RuntimeError:
        pass
    else:
        raise AssertionError("已使用审批不得授权另一组参数")

    expired = new_approval("canvas.run:story-a", payload, ttl_seconds=1)
    expired.expires_at = time.time() - 1
    await approvals.create(expired)
    try:
        await approvals.consume(expired.approval_id, expired.scope, expired.args_hash)
    except RuntimeError as exc:
        assert "过期" in str(exc)
    else:
        raise AssertionError("过期审批必须拒绝")

    await invocations.close()
    await approvals.close()
    print("PASS: 副作用工具相同调用只执行一次")
    print("PASS: 插件加载后仍保留本地工具副作用元数据")
    print("PASS: 参数变化、过期审批均被拒绝，完全相同请求可安全重放")


if __name__ == "__main__":
    asyncio.run(main())
