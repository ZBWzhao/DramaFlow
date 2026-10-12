"""结构化问答状态存储：本地内存实现与 Redis 跨实例实现。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class QuestionState:
    question_id: str
    session_id: str
    run_id: str
    call_id: str
    title: str
    question: str
    options: list[str]
    allow_custom: bool
    agent: str
    status: str = "pending"
    answer: str | None = None
    created_at: float = field(default_factory=time.time)
    resolved_at: float | None = None

    def to_public(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "run_id": self.run_id,
            "call_id": self.call_id,
            "title": self.title,
            "question": self.question,
            "options": list(self.options),
            "allow_custom": self.allow_custom,
            "agent": self.agent,
            "status": self.status,
            **({"answer": self.answer} if self.answer is not None else {}),
        }


@runtime_checkable
class QuestionStateStore(Protocol):
    async def start(self) -> None: ...
    async def create(self, state: QuestionState) -> None: ...
    async def get(self, question_id: str) -> QuestionState | None: ...
    async def list_pending(self, session_id: str) -> list[QuestionState]: ...
    async def answer(self, session_id: str, question_id: str, answer: str) -> QuestionState: ...
    async def cancel(self, session_id: str, question_id: str) -> QuestionState | None: ...
    async def close(self) -> None: ...


class InMemoryQuestionStateStore:
    def __init__(self) -> None:
        self._items: dict[str, QuestionState] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        return None

    async def create(self, state: QuestionState) -> None:
        async with self._lock:
            if state.question_id in self._items:
                raise RuntimeError("问题 ID 已存在")
            self._items[state.question_id] = state

    async def get(self, question_id: str) -> QuestionState | None:
        async with self._lock:
            state = self._items.get(question_id)
            return QuestionState(**asdict(state)) if state else None

    async def list_pending(self, session_id: str) -> list[QuestionState]:
        async with self._lock:
            return [
                QuestionState(**asdict(item))
                for item in self._items.values()
                if item.session_id == session_id and item.status == "pending"
            ]

    async def answer(self, session_id: str, question_id: str, answer: str) -> QuestionState:
        async with self._lock:
            state = self._items.get(question_id)
            if state is None or state.session_id != session_id:
                raise KeyError("问题不存在")
            if state.status != "pending":
                raise RuntimeError("问题已经回答或取消")
            _validate_answer(state, answer)
            state.status = "answered"
            state.answer = answer
            state.resolved_at = time.time()
            return QuestionState(**asdict(state))

    async def cancel(self, session_id: str, question_id: str) -> QuestionState | None:
        async with self._lock:
            state = self._items.get(question_id)
            if state is None or state.session_id != session_id or state.status != "pending":
                return None
            state.status = "cancelled"
            state.resolved_at = time.time()
            return QuestionState(**asdict(state))

    async def close(self) -> None:
        return None


class RedisQuestionStateStore:
    _CREATE = """
    if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
    for i = 3, #ARGV, 2 do redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1]) end
    redis.call('EXPIRE', KEYS[1], ARGV[2])
    redis.call('SADD', KEYS[2], ARGV[1])
    redis.call('EXPIRE', KEYS[2], ARGV[2])
    return 1
    """
    _ANSWER = """
    if redis.call('EXISTS', KEYS[1]) == 0 then return -1 end
    if redis.call('HGET', KEYS[1], 'session_id') ~= ARGV[1] then return -1 end
    if redis.call('HGET', KEYS[1], 'status') ~= 'pending' then return -2 end
    redis.call('HSET', KEYS[1], 'status', 'answered', 'answer', ARGV[2], 'resolved_at', ARGV[3])
    redis.call('SREM', KEYS[2], ARGV[4])
    redis.call('EXPIRE', KEYS[1], ARGV[5])
    return 1
    """
    _CANCEL = """
    if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
    if redis.call('HGET', KEYS[1], 'session_id') ~= ARGV[1] then return 0 end
    if redis.call('HGET', KEYS[1], 'status') ~= 'pending' then return 0 end
    redis.call('HSET', KEYS[1], 'status', 'cancelled', 'resolved_at', ARGV[2])
    redis.call('SREM', KEYS[2], ARGV[3])
    redis.call('EXPIRE', KEYS[1], ARGV[4])
    return 1
    """

    def __init__(self, url: str, prefix: str = "dramaflow", ttl_seconds: int = 86400) -> None:
        self.url = url
        self.prefix = prefix.rstrip(":")
        self.ttl_seconds = max(300, ttl_seconds)
        self._redis: Any = None

    def _key(self, question_id: str) -> str:
        return f"{self.prefix}:question:{question_id}"

    def _pending_key(self, session_id: str) -> str:
        return f"{self.prefix}:questions:{session_id}:pending"

    async def start(self) -> None:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Redis 问答状态存储已启用，但未安装 redis 包") from exc
        self._redis = Redis.from_url(self.url, decode_responses=True)
        try:
            await self._redis.ping()
        except Exception as exc:  # noqa: BLE001
            await self._redis.aclose()
            self._redis = None
            raise RuntimeError(f"Redis 问答状态存储连接失败：{exc}") from exc

    def _client(self) -> Any:
        if self._redis is None:
            raise RuntimeError("Redis 问答状态存储尚未初始化")
        return self._redis

    async def create(self, state: QuestionState) -> None:
        key = self._key(state.question_id)
        mapping = _state_mapping(state)
        pairs = [value for item in mapping.items() for value in item]
        created = await self._client().eval(
            self._CREATE,
            2,
            key,
            self._pending_key(state.session_id),
            state.question_id,
            str(self.ttl_seconds),
            *pairs,
        )
        if not created:
            raise RuntimeError("问题 ID 已存在")

    async def get(self, question_id: str) -> QuestionState | None:
        values = await self._client().hgetall(self._key(question_id))
        return _state_from_mapping(values) if values else None

    async def list_pending(self, session_id: str) -> list[QuestionState]:
        ids = await self._client().smembers(self._pending_key(session_id))
        out: list[QuestionState] = []
        stale: list[str] = []
        for question_id in ids:
            state = await self.get(str(question_id))
            if state and state.status == "pending":
                out.append(state)
            else:
                stale.append(str(question_id))
        if stale:
            await self._client().srem(self._pending_key(session_id), *stale)
        return sorted(out, key=lambda item: item.created_at)

    async def answer(self, session_id: str, question_id: str, answer: str) -> QuestionState:
        current = await self.get(question_id)
        if current is None or current.session_id != session_id:
            raise KeyError("问题不存在")
        _validate_answer(current, answer)
        result = int(
            await self._client().eval(
                self._ANSWER,
                2,
                self._key(question_id),
                self._pending_key(session_id),
                session_id,
                answer,
                str(time.time()),
                question_id,
                str(self.ttl_seconds),
            )
        )
        if result == -1:
            raise KeyError("问题不存在")
        if result == -2:
            raise RuntimeError("问题已经回答或取消")
        return (await self.get(question_id))  # type: ignore[return-value]

    async def cancel(self, session_id: str, question_id: str) -> QuestionState | None:
        result = int(
            await self._client().eval(
                self._CANCEL,
                2,
                self._key(question_id),
                self._pending_key(session_id),
                session_id,
                str(time.time()),
                question_id,
                str(self.ttl_seconds),
            )
        )
        return await self.get(question_id) if result else None

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


def _validate_answer(state: QuestionState, answer: str) -> None:
    if not answer:
        raise ValueError("回答不能为空")
    if state.options and not state.allow_custom and answer not in state.options:
        raise ValueError("回答必须从给定选项中选择")


def _state_mapping(state: QuestionState) -> dict[str, str]:
    return {
        "question_id": state.question_id,
        "session_id": state.session_id,
        "run_id": state.run_id,
        "call_id": state.call_id,
        "title": state.title,
        "question": state.question,
        "options": json.dumps(state.options, ensure_ascii=False),
        "allow_custom": "1" if state.allow_custom else "0",
        "agent": state.agent,
        "status": state.status,
        "answer": state.answer or "",
        "created_at": str(state.created_at),
        "resolved_at": "" if state.resolved_at is None else str(state.resolved_at),
    }


def _state_from_mapping(values: dict[str, str]) -> QuestionState:
    return QuestionState(
        question_id=values["question_id"],
        session_id=values["session_id"],
        run_id=values.get("run_id", ""),
        call_id=values.get("call_id", ""),
        title=values.get("title", "需要你的选择"),
        question=values.get("question", ""),
        options=json.loads(values.get("options") or "[]"),
        allow_custom=values.get("allow_custom") != "0",
        agent=values.get("agent", "main"),
        status=values.get("status", "pending"),
        answer=values.get("answer") or None,
        created_at=float(values.get("created_at") or time.time()),
        resolved_at=float(values["resolved_at"]) if values.get("resolved_at") else None,
    )


def create_question_store() -> QuestionStateStore:
    configured = os.environ.get("QUESTION_STORE_BACKEND", "").strip().lower()
    run_backend = os.environ.get("RUN_STATE_STORE_BACKEND", "").strip().lower()
    event_backend = os.environ.get("EVENT_STORE_BACKEND", "memory").strip().lower()
    backend = configured or run_backend or event_backend or "memory"
    if backend == "memory":
        return InMemoryQuestionStateStore()
    if backend == "redis":
        url = os.environ.get("REDIS_URL", "").strip()
        if not url:
            raise RuntimeError("QUESTION_STORE_BACKEND=redis 时必须配置 REDIS_URL")
        return RedisQuestionStateStore(
            url=url,
            prefix=os.environ.get("RUN_STATE_PREFIX", "dramaflow"),
            ttl_seconds=int(os.environ.get("QUESTION_TTL_SECONDS", "86400")),
        )
    raise RuntimeError(f"未知 QUESTION_STORE_BACKEND：{backend}")
