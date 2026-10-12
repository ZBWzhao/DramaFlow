"""第二阶段回归：Follow-up、结构化问答、分支恢复、会话管理。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server.registry as registry_module
from core.agent import Agent
from core.context import ToolContext
from core.events import EventBus
from core.llm import AssistantMessage, Usage
from core.questions import QuestionBroker
from core.question_store import InMemoryQuestionStateStore
from core.runtime_store import InMemoryRunStateStore, RunState
from core.session import Session
from server.registry import SessionRegistry


class FakeLLM:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(context_window=128000, max_output_tokens=8192)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None, on_text=None, on_reasoning=None):
        self.calls.append(messages)
        if len(self.calls) == 1:
            self.started.set()
            await self.release.wait()
            text = "第一段完成"
        else:
            assert any(
                item.get("role") == "user" and item.get("content") == "把结尾改成开放式"
                for item in messages
            )
            text = "已按补充要求继续"
        if on_text:
            on_text(text)
        return AssistantMessage(content=text, usage=Usage(10, 3), finish_reason="stop")

    async def simple(self, messages):
        return "summary"


async def main() -> None:
    llm = FakeLLM()
    session = Session()
    agent = Agent(llm=llm, session=session, tools=[], context=ToolContext(cwd="."))
    running = asyncio.create_task(agent.run("写一个结尾"))
    await llm.started.wait()
    assert agent.queue_followup("把结尾改成开放式") == 1
    llm.release.set()
    assert await running == "已按补充要求继续"
    assert len(llm.calls) == 2
    print("PASS Follow-up safe-point queue")

    shared_llm = FakeLLM()
    shared_agent = Agent(llm=shared_llm, session=Session(), tools=[], context=ToolContext(cwd="."))
    shared_store = InMemoryRunStateStore()
    shared_run = RunState(run_id="shared-run", session_id="shared-session", kind="agent")
    assert await shared_store.claim(shared_run)
    shared_agent.external_followup_loader = lambda: shared_store.drain_followups(shared_run.run_id)
    shared_running = asyncio.create_task(shared_agent.run("写另一个结尾"))
    await shared_llm.started.wait()
    assert await shared_store.enqueue_followup(shared_run.run_id, "把结尾改成开放式") == 1
    shared_llm.release.set()
    assert await shared_running == "已按补充要求继续"
    assert len(shared_llm.calls) == 2
    await shared_store.finish(shared_run.run_id, "done")
    print("PASS shared Follow-up safe-point queue")

    broker = QuestionBroker(Session(), EventBus())
    asking = asyncio.create_task(broker.ask("c1", "画幅", "请选择画幅", ["16:9", "9:16"]))
    await asyncio.sleep(0)
    question = broker.snapshot()[0]
    await broker.answer(question["question_id"], "9:16")
    assert await asking == "9:16"
    assert broker.snapshot() == []
    print("PASS inputRequired pause/resume")

    shared_questions = InMemoryQuestionStateStore()
    asking_broker = QuestionBroker(Session(session_id="shared-q"), EventBus(), store=shared_questions)
    answering_broker = QuestionBroker(Session(session_id="shared-q"), EventBus(), store=shared_questions)
    cross_asking = asyncio.create_task(
        asking_broker.ask("c2", "画幅", "请选择画幅", ["16:9", "9:16"], allow_custom=False)
    )
    await asyncio.sleep(0)
    shared_pending = await shared_questions.list_pending("shared-q")
    assert len(shared_pending) == 1
    question_id = shared_pending[0].question_id
    invalid_rejected = False
    try:
        await answering_broker.answer(question_id, "4:3")
    except ValueError:
        invalid_rejected = True
    assert invalid_rejected
    await answering_broker.answer(question_id, "9:16")
    assert await cross_asking == "9:16"
    duplicate_rejected = False
    try:
        await answering_broker.answer(question_id, "16:9")
    except RuntimeError:
        duplicate_rejected = True
    assert duplicate_rejected
    assert await shared_questions.list_pending("shared-q") == []
    print("PASS cross-instance inputRequired and duplicate guard")

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "branchcase.jsonl"
        branch = Session(session_id="branchcase", path=path)
        first = branch.append({"role": "user", "content": "A"})
        branch.append({"role": "assistant", "content": "A1"})
        branch.checkout(None, reason="resend")
        branch.append({"role": "user", "content": "B"})
        branch.append({"role": "assistant", "content": "B1"})
        branch.checkout(first, reason="branch")
        restored = Session.load(path)
        assert [item["content"] for item in restored.messages] == ["A"]
        print("PASS append-only branch restore")

    with tempfile.TemporaryDirectory() as folder:
        original = registry_module.SESSIONS_DIR
        registry_module.SESSIONS_DIR = Path(folder)
        try:
            registry = SessionRegistry(FakeLLM(), folder)
            session_id = registry.create_session("main", "原名称")["id"]
            assert registry.rename_session(session_id, "新名称")["meta"]["title"] == "新名称"
            assert registry.delete_session(session_id)
            print("PASS session rename/delete")
        finally:
            registry_module.SESSIONS_DIR = original


if __name__ == "__main__":
    asyncio.run(main())
