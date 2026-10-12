"""事件契约与事件总线。

对照 Toonflow：apps/server/src/agent/runtime/types.ts 的 AgentEvent。
设计要点（和 Toonflow 一样）：
  · 事件是"给界面看的通知"，和"给模型看的消息"是两条线，互不相干
  · 事件边产生边推（后面 FastAPI 用 NDJSON 一行一个事件发给浏览器）
  · 子 Agent 的事件会被"打标"再转发（sub_agent 字段），对应 delegation.ts 的 subAgentEvent
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from core.runtime_store import EventStore

# 事件日志上限（只用于诊断，超出就丢最老的）
LOG_LIMIT = int(os.environ.get("EVENT_LOG_LIMIT", "4000"))


@dataclass
class AgentEvent:
    type: str
    event_id: str | None = None
    run_id: str | None = None
    session_id: str | None = None
    sub_agent: str | None = None  # 非空 = 这是子 Agent 的事件（对应 subAgentEvent 打标）
    ts: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "ts": self.ts}
        if self.event_id:
            out["event_id"] = self.event_id
        if self.run_id:
            out["run_id"] = self.run_id
        if self.session_id:
            out["session_id"] = self.session_id
        if self.sub_agent:
            out["sub_agent"] = self.sub_agent
        out.update(self.data)
        return out


class EventBus:
    """同步 emit + 异步队列广播。

    为什么 emit 是同步的：agent 循环里到处都要发事件，若都 await 会把代码污染成异步地狱；
    put_nowait 到队列即可，真正的 IO 由订阅方（HTTP 流 / 终端打印）自己承担。
    """

    def __init__(self, store: EventStore | None = None, session_id: str | None = None) -> None:
        self._queues: list[asyncio.Queue] = []
        self.store = store
        self.session_id = session_id
        self.run_id: str | None = None
        self._pending: asyncio.Queue[tuple[AgentEvent, asyncio.Future[AgentEvent] | None]] = asyncio.Queue()
        self._dispatcher: asyncio.Task | None = None
        # 事件日志只用于诊断，**必须有上限**：每个会话一个 bus 且都常驻，
        # 无上限的话长跑的进程内存会一直涨（一轮对话就能产生几百条事件）。
        self.log: deque[AgentEvent] = deque(maxlen=LOG_LIMIT)
        self.dropped = 0  # 被丢弃的事件数（只应该丢流式增量）

    def subscribe(self, maxsize: int = 256) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._queues.append(q)
        self._ensure_dispatcher()
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._queues:
            self._queues.remove(q)

    def emit(self, type_: str, **data: Any) -> AgentEvent:
        session_id = data.pop("session_id", None) or self.session_id
        sub_agent = data.pop("sub_agent", None)
        event = AgentEvent(
            type=type_,
            session_id=session_id,
            sub_agent=sub_agent,
            run_id=self.run_id,
            data=data,
        )
        self.log.append(event)
        if self.store is not None and session_id:
            self._ensure_dispatcher()
            self._pending.put_nowait((event, None))
            return event
        for q in self._queues:
            self._deliver(q, event)
        return event

    async def publish(self, type_: str, **data: Any) -> AgentEvent:
        """有确认语义的发布，用于 session/done 等运行边界事件。"""
        session_id = data.pop("session_id", None) or self.session_id
        sub_agent = data.pop("sub_agent", None)
        event = AgentEvent(
            type=type_,
            session_id=session_id,
            sub_agent=sub_agent,
            run_id=self.run_id,
            data=data,
        )
        self.log.append(event)
        if self.store is None or not session_id:
            for q in self._queues:
                self._deliver(q, event)
            return event
        self._ensure_dispatcher()
        ack: asyncio.Future[AgentEvent] = asyncio.get_running_loop().create_future()
        await self._pending.put((event, ack))
        return await ack

    def _ensure_dispatcher(self) -> None:
        if self.store is None or (self._dispatcher and not self._dispatcher.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._dispatcher = loop.create_task(self._dispatch(), name=f"events:{self.session_id or 'anonymous'}")

    async def _dispatch(self) -> None:
        while True:
            event, ack = await self._pending.get()
            try:
                event.event_id = await self.store.append(event.session_id or self.session_id or "", event.to_dict())
                for q in list(self._queues):
                    self._deliver(q, event)
                if ack and not ack.done():
                    ack.set_result(event)
            except asyncio.CancelledError:
                if ack and not ack.done():
                    ack.cancel()
                raise
            except Exception as exc:  # noqa: BLE001
                # 不静默回退到内存：明确把持久化故障送给当前连接，并让带 ack
                # 的边界事件抛错，便于运行编排进入 error 状态。
                event.data["event_store_error"] = f"{type(exc).__name__}: {exc}"
                for q in list(self._queues):
                    self._deliver(q, event)
                if ack and not ack.done():
                    ack.set_exception(exc)
            finally:
                self._pending.task_done()

    async def flush(self) -> None:
        if self.store is not None:
            self._ensure_dispatcher()
            await self._pending.join()

    async def close(self) -> None:
        await self.flush()
        if self._dispatcher and not self._dispatcher.done():
            self._dispatcher.cancel()
            await asyncio.gather(self._dispatcher, return_exceptions=True)

    def _deliver(self, q: asyncio.Queue, event: AgentEvent) -> None:
        """投递一个事件；队列满时**只丢流式增量**。

        为什么不能一满了就什么都丢（原来的做法）：`tool_execution_end` 被丢掉，
        界面上那张工具卡就永远停在"调用中"，而且**不会再有事件把它补回来** ——
        表现就是"没完成、也不显示思考、也不显示结束"。
        生命周期事件（*_end / done / error / aborted）是有状态转移副作用的，
        丢了就永久不一致；`*_update` 只是流式增量，丢了最多少几个字。
        """
        try:
            q.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass

        kept: list[AgentEvent] = []
        while not q.empty():
            try:
                pending = q.get_nowait()
            except asyncio.QueueEmpty:
                break
            if pending.type.endswith("_update"):
                self.dropped += 1
            else:
                kept.append(pending)
        for item in kept:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                break
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # 最后手段：挤掉最老的一条，确保**刚刚发生的事**一定进得去
            try:
                q.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self.dropped += 1

    def forward(self, event: AgentEvent, sub_agent: str) -> AgentEvent:
        """把子 Agent 的事件打标后再转发（对应 delegation.ts 的 subAgentEvent）。"""
        forwarded = AgentEvent(
            type=event.type,
            session_id=event.session_id,
            sub_agent=sub_agent,
            ts=event.ts,
            run_id=self.run_id,
            data=dict(event.data),
        )
        self.log.append(forwarded)
        if self.store is not None and (forwarded.session_id or self.session_id):
            forwarded.session_id = forwarded.session_id or self.session_id
            self._ensure_dispatcher()
            self._pending.put_nowait((forwarded, None))
            return forwarded
        for q in self._queues:
            self._deliver(q, forwarded)
        return forwarded

    async def stream(self) -> Any:
        """订阅并持续产出事件的 to_dict（给 FastAPI 的 NDJSON 用）。"""
        q = self.subscribe()
        try:
            while True:
                event = await q.get()
                yield event.to_dict()
        finally:
            self.unsubscribe(q)
