"""结构化问答通道：Agent 可暂停等待用户，回答后从原工具调用继续。"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, Field

from .context import ToolContext
from .events import EventBus
from .plugin import ToolDefinition, define_tool, tool_result
from .question_store import InMemoryQuestionStateStore, QuestionState, QuestionStateStore
from .session import Session

CUSTOM_INPUT = "inputRequired"


class AskUserArgs(BaseModel):
    title: str = Field(default="需要你的选择", max_length=80)
    question: str = Field(description="一个具体、可直接回答的问题", min_length=1, max_length=1000)
    options: list[str] = Field(default_factory=list, description="可选的 2-4 个简短选项；允许为空")
    allow_custom: bool = Field(default=True, description="是否允许用户输入自定义答案")


@dataclass
class PendingQuestion:
    id: str
    title: str
    question: str
    options: list[str]
    allow_custom: bool
    agent: str
    future: asyncio.Future[str]


class QuestionBroker:
    def __init__(
        self,
        session: Session,
        bus: EventBus,
        on_status: Callable[[str, bool, dict[str, Any]], Any] | None = None,
        store: QuestionStateStore | None = None,
    ) -> None:
        self.session = session
        self.bus = bus
        self.on_status = on_status
        self.store = store or InMemoryQuestionStateStore()
        self.pending: dict[str, PendingQuestion] = {}

    def snapshot(self) -> list[dict[str, Any]]:
        return [self._data(item, "pending") for item in self.pending.values()]

    async def ask(
        self,
        call_id: str,
        title: str,
        question: str,
        options: list[str] | None = None,
        *,
        agent: str = "main",
        allow_custom: bool = True,
    ) -> str:
        question_id = f"q_{uuid.uuid4().hex[:10]}"
        choices = [str(item).strip()[:120] for item in (options or []) if str(item).strip()][:4]
        loop = asyncio.get_running_loop()
        pending = PendingQuestion(
            id=question_id,
            title=title.strip()[:80] or "需要你的选择",
            question=question.strip()[:1000],
            options=choices,
            allow_custom=allow_custom,
            agent=agent,
            future=loop.create_future(),
        )
        self.pending[question_id] = pending
        data = self._data(pending, "pending", call_id=call_id)
        await self.store.create(
            QuestionState(
                question_id=question_id,
                session_id=self.session.id,
                run_id=self.bus.run_id or "",
                call_id=call_id,
                title=pending.title,
                question=pending.question,
                options=pending.options,
                allow_custom=pending.allow_custom,
                agent=pending.agent,
            )
        )
        self.session.append_custom(CUSTOM_INPUT, data, display=True)
        self.bus.emit("input_required", session_id=self.session.id, sub_agent=None if agent == "main" else agent, **data)
        await self._notify(agent, True, data)
        try:
            while True:
                try:
                    return await asyncio.wait_for(asyncio.shield(pending.future), timeout=0.25)
                except asyncio.TimeoutError:
                    shared = await self.store.get(question_id)
                    if shared and shared.status == "answered" and shared.answer is not None:
                        await self._resolve_local(pending, shared)
                    elif shared and shared.status == "cancelled":
                        raise asyncio.CancelledError
        except asyncio.CancelledError:
            shared = await self.store.cancel(self.session.id, question_id)
            if shared is not None:
                cancelled = self._data(pending, "cancelled")
                self.session.append_custom(CUSTOM_INPUT, cancelled, display=True)
                self.bus.emit(
                    "input_cancelled",
                    session_id=self.session.id,
                    sub_agent=None if agent == "main" else agent,
                    **cancelled,
                )
                await self._notify(agent, False, cancelled)
            raise
        finally:
            self.pending.pop(question_id, None)

    async def answer(self, question_id: str, answer: str) -> dict[str, Any]:
        value = str(answer or "").strip()
        if not value:
            raise ValueError("回答不能为空")
        shared = await self.store.answer(self.session.id, question_id, value[:2000])
        pending = self.pending.get(question_id)
        if pending is not None:
            await self._resolve_local(pending, shared)
        return shared.to_public()

    async def _resolve_local(self, pending: PendingQuestion, shared: QuestionState) -> None:
        if pending.future.done():
            return
        value = shared.answer or ""
        data = {**self._data(pending, "answered"), "answer": value}
        self.session.append_custom(CUSTOM_INPUT, data, display=True)
        self.bus.emit(
            "input_resolved",
            session_id=self.session.id,
            sub_agent=None if pending.agent == "main" else pending.agent,
            **data,
        )
        await self._notify(pending.agent, False, data)
        if not pending.future.done():
            pending.future.set_result(value)

    async def _notify(self, agent: str, waiting: bool, data: dict[str, Any]) -> None:
        if self.on_status is None or agent == "main":
            return
        result = self.on_status(agent, waiting, data)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _data(pending: PendingQuestion, status: str, **extra: Any) -> dict[str, Any]:
        return {
            "question_id": pending.id,
            "title": pending.title,
            "question": pending.question,
            "options": pending.options,
            "allow_custom": pending.allow_custom,
            "agent": pending.agent,
            "status": status,
            **extra,
        }


def build_question_tool() -> ToolDefinition:
    async def _execute(args: AskUserArgs, ctx: ToolContext) -> Any:
        broker = ctx.require_question()
        answer = await broker.ask(
            uuid.uuid4().hex[:10],
            args.title,
            args.question,
            args.options,
            agent=str(ctx.config.get("agent_name") or "main"),
            allow_custom=args.allow_custom,
        )
        return tool_result(f"用户回答：{answer}", answer=answer)

    return define_tool(
        name="ask_user",
        description="当缺失信息会显著改变短剧结果时，用结构化问题暂停并等待用户回答。",
        args_model=AskUserArgs,
        execute=_execute,
        prompt_guidelines=["一次集中询问一个关键问题，优先给出 2-4 个互斥选项", "非关键细节采用合理默认值，不要反复提问"],
    )


def pending_questions(session: Session) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for item in session.customs_of(CUSTOM_INPUT):
        question_id = item.get("question_id")
        if question_id:
            latest[question_id] = item
    return [item for item in latest.values() if item.get("status") == "pending"]
