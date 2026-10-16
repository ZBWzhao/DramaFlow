"""Agent 终态、检查点与画布局部更新回归（不调用真实模型）。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.agent import Agent
from core.canvas import Canvas
from core.canvas_repository import FileCanvasRepository
from core.context import ToolContext
from core.events import EventBus
from core.llm import AssistantMessage, ToolCall, Usage
from core.plugin import define_tool, tool_result
from core.session import Session
from tools.canvas.tool import build_tools


class NoArgs(BaseModel):
    pass


class FakeLLM:
    def __init__(self, messages: list[AssistantMessage]) -> None:
        self.messages = list(messages)
        self.settings = SimpleNamespace(context_window=128000, max_output_tokens=8192)

    async def chat(self, messages, tools=None, on_text=None, on_reasoning=None):
        message = self.messages.pop(0)
        if message.content and on_text:
            on_text(message.content)
        return message

    async def simple(self, messages):
        return "summary"


def tool_message(index: int) -> AssistantMessage:
    return AssistantMessage(
        tool_calls=[ToolCall(id=f"call-{index}", name="noop", arguments="{}")],
        usage=Usage(10, 2),
        finish_reason="tool_calls",
    )


async def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        calls: list[int] = []

        def noop(args: NoArgs, context: ToolContext):
            calls.append(1)
            return tool_result("ok")

        tool = define_tool("noop", "测试工具", NoArgs, noop)
        session = Session(session_id="limited-case", path=root / "limited-case.jsonl")
        bus = EventBus()
        limited = Agent(
            llm=FakeLLM([tool_message(1), tool_message(2)]),
            session=session,
            tools=[tool],
            context=ToolContext(cwd=str(root)),
            bus=bus,
            max_turns=2,
        )
        await limited.run("执行长任务")
        assert limited.last_status == "limited"
        assert limited.last_finish_reason == "max_turns"
        assert limited.last_checkpoint and limited.last_checkpoint["tool_calls"] == 2
        assert session.customs_of("runCheckpoint")[-1]["status"] == "limited"
        assert [event.type for event in bus.log][-3:] == ["limited", "checkpoint", "agent_end"]
        assert calls == [1, 1]
        print("PASS max-turns becomes limited with persistent checkpoint")

        completed = Agent(
            llm=FakeLLM([tool_message(3), AssistantMessage(content="制作完成", finish_reason="stop")]),
            session=Session(session_id="complete-case", path=root / "complete-case.jsonl"),
            tools=[tool],
            context=ToolContext(cwd=str(root)),
            max_turns=2,
        )
        assert await completed.run("执行短任务") == "制作完成"
        assert completed.last_status == "completed"
        assert completed.last_checkpoint is None
        assert completed.session.customs_of("runCheckpoint")[-1]["status"] == "completed"
        print("PASS only a final deliverable response becomes completed")

        repository = FileCanvasRepository(root)
        await repository.start()
        canvas = Canvas("main", root / "main.canvas.json")
        asset = canvas.add_node("asset", "角色资产")
        await repository.save(canvas)
        context = ToolContext(cwd=str(root), canvas_repository=repository)
        update = next(item for item in build_tools(context) if item.name == "update_node")
        result = await update.execute(
            {"canvas": "main", "node_id": asset.id, "data": {"spec": "黄色卡通角色"}}, context
        )
        restored = await repository.load("main")
        assert not result.is_error
        assert restored.require(asset.id).status == "draft"
        assert restored.require(asset.id).data["spec"] == "黄色卡通角色"
        print("PASS update_node omits None fields and preserves node status")


if __name__ == "__main__":
    asyncio.run(main())
