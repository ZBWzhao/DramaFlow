"""运行状态与事件持久化抽象。

默认实现完全在内存中，保持开发环境零依赖；显式设置
``EVENT_STORE_BACKEND=redis`` 后，事件写入 Redis Streams，用 Stream ID
作为可续传的递增事件游标。Redis 模式连接失败时直接阻止服务启动，避免多实例
部署时悄悄退回各自内存、形成状态分裂。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class RunState:
    run_id: str
    session_id: str
    kind: str
    status: str = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    start_event_id: str = "0-0"
    error: str | None = None
    cancel_requested: bool = False
    heartbeat_at: float = field(default_factory=time.time)


@runtime_checkable
class RunStateStore(Protocol):
    async def start(self) -> None: ...
    async def put(self, state: RunState) -> None: ...
    async def claim(self, state: RunState) -> bool: ...
    async def get(self, run_id: str) -> RunState | None: ...
    async def active_run(self, session_id: str) -> RunState | None: ...
    async def request_cancel(self, run_id: str) -> bool: ...
    async def is_cancelled(self, run_id: str) -> bool: ...
    async def heartbeat(self, run_id: str) -> bool: ...
    async def enqueue_followup(self, run_id: str, text: str) -> int: ...
    async def drain_followups(self, run_id: str) -> list[str]: ...
    async def finish(self, run_id: str, status: str, error: str | None = None) -> None: ...
    async def close(self) -> None: ...


class InMemoryRunStateStore:
    def __init__(self, limit: int = 1000) -> None:
        self._states: dict[str, RunState] = {}
        self._order: deque[str] = deque()
        self._active: dict[str, str] = {}
        self._followups: dict[str, deque[str]] = defaultdict(deque)
        self._limit = max(10, limit)
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        return None

    async def put(self, state: RunState) -> None:
        async with self._lock:
            if state.run_id not in self._states:
                self._order.append(state.run_id)
            self._states[state.run_id] = state
            while len(self._order) > self._limit:
                self._states.pop(self._order.popleft(), None)

    async def claim(self, state: RunState) -> bool:
        async with self._lock:
            active_id = self._active.get(state.session_id)
            active = self._states.get(active_id or "")
            if active and active.status == "running":
                return False
            if state.run_id not in self._states:
                self._order.append(state.run_id)
            self._states[state.run_id] = state
            self._active[state.session_id] = state.run_id
            return True

    async def get(self, run_id: str) -> RunState | None:
        async with self._lock:
            state = self._states.get(run_id)
            return RunState(**asdict(state)) if state else None

    async def active_run(self, session_id: str) -> RunState | None:
        async with self._lock:
            state = self._states.get(self._active.get(session_id, ""))
            return RunState(**asdict(state)) if state and state.status == "running" else None

    async def request_cancel(self, run_id: str) -> bool:
        async with self._lock:
            state = self._states.get(run_id)
            if not state or state.status != "running":
                return False
            state.cancel_requested = True
            return True

    async def is_cancelled(self, run_id: str) -> bool:
        async with self._lock:
            state = self._states.get(run_id)
            return bool(state and state.cancel_requested)

    async def heartbeat(self, run_id: str) -> bool:
        async with self._lock:
            state = self._states.get(run_id)
            if state and state.status == "running":
                state.heartbeat_at = time.time()
                return True
            return False

    async def enqueue_followup(self, run_id: str, text: str) -> int:
        async with self._lock:
            state = self._states.get(run_id)
            if not state or state.status != "running" or state.kind != "agent" or state.cancel_requested:
                raise RuntimeError("当前运行不接受补充要求")
            self._followups[run_id].append(text)
            return len(self._followups[run_id])

    async def drain_followups(self, run_id: str) -> list[str]:
        async with self._lock:
            values = list(self._followups.pop(run_id, ()))
            return values

    async def finish(self, run_id: str, status: str, error: str | None = None) -> None:
        async with self._lock:
            state = self._states.get(run_id)
            if state:
                state.status = status
                state.error = error
                state.finished_at = time.time()
                if self._active.get(state.session_id) == run_id:
                    self._active.pop(state.session_id, None)
                self._followups.pop(run_id, None)

    async def close(self) -> None:
        return None


class RedisRunStateStore:
    """Redis Hash + List 实现的运行状态、取消标志与 Follow-up 队列。"""

    _CLAIM = """
    local active = redis.call('GET', KEYS[2])
    if active then
      local status = redis.call('HGET', ARGV[1] .. active, 'status')
      local heartbeat = tonumber(redis.call('HGET', ARGV[1] .. active, 'heartbeat_at') or '0')
      if status == 'running' and heartbeat >= tonumber(ARGV[9]) then return 0 end
      if status == 'running' then
        redis.call('HSET', ARGV[1] .. active, 'status', 'interrupted', 'finished_at', ARGV[5], 'error', 'stale heartbeat')
      end
    end
    redis.call('HSET', KEYS[1],
      'run_id', ARGV[2], 'session_id', ARGV[3], 'kind', ARGV[4],
      'status', 'running', 'started_at', ARGV[5], 'finished_at', '',
      'start_event_id', ARGV[6], 'error', '', 'cancel_requested', '0', 'heartbeat_at', ARGV[5])
    redis.call('EXPIRE', KEYS[1], ARGV[7])
    redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[8])
    return 1
    """
    _FINISH = """
    if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
    redis.call('HSET', KEYS[1], 'status', ARGV[1], 'finished_at', ARGV[2], 'error', ARGV[3])
    redis.call('EXPIRE', KEYS[1], ARGV[4])
    redis.call('DEL', KEYS[3])
    if redis.call('GET', KEYS[2]) == ARGV[5] then redis.call('DEL', KEYS[2]) end
    return 1
    """
    _ENQUEUE = """
    if redis.call('HGET', KEYS[1], 'status') ~= 'running' then return -1 end
    if redis.call('HGET', KEYS[1], 'kind') ~= 'agent' then return -2 end
    if redis.call('HGET', KEYS[1], 'cancel_requested') == '1' then return -3 end
    redis.call('RPUSH', KEYS[2], ARGV[1])
    redis.call('EXPIRE', KEYS[2], ARGV[2])
    return redis.call('LLEN', KEYS[2])
    """
    _DRAIN = """
    local values = redis.call('LRANGE', KEYS[1], 0, -1)
    redis.call('DEL', KEYS[1])
    return values
    """
    _HEARTBEAT = """
    if redis.call('HGET', KEYS[1], 'status') ~= 'running' then return 0 end
    local session_id = redis.call('HGET', KEYS[1], 'session_id')
    if not session_id then return 0 end
    local active_key = ARGV[1] .. session_id
    if redis.call('GET', active_key) ~= ARGV[2] then return 0 end
    redis.call('EXPIRE', KEYS[1], ARGV[3])
    redis.call('EXPIRE', active_key, ARGV[4])
    redis.call('HSET', KEYS[1], 'heartbeat_at', ARGV[5])
    return 1
    """

    def __init__(
        self,
        url: str,
        prefix: str = "dramaflow",
        retention_seconds: int = 86400,
        active_ttl_seconds: int = 7200,
    ) -> None:
        self.url = url
        self.prefix = prefix.rstrip(":")
        self.retention_seconds = max(300, retention_seconds)
        self.active_ttl_seconds = max(60, active_ttl_seconds)
        self._redis: Any = None

    def _run_key(self, run_id: str) -> str:
        return f"{self.prefix}:run:{run_id}"

    def _active_key(self, session_id: str) -> str:
        return f"{self.prefix}:active:{session_id}"

    def _followup_key(self, run_id: str) -> str:
        return f"{self.prefix}:followups:{run_id}"

    async def start(self) -> None:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Redis 运行状态存储已启用，但未安装 redis 包") from exc
        self._redis = Redis.from_url(self.url, decode_responses=True)
        try:
            await self._redis.ping()
        except Exception as exc:  # noqa: BLE001
            await self._redis.aclose()
            self._redis = None
            raise RuntimeError(f"Redis 运行状态存储连接失败：{exc}") from exc

    def _client(self) -> Any:
        if self._redis is None:
            raise RuntimeError("Redis 运行状态存储尚未初始化")
        return self._redis

    async def put(self, state: RunState) -> None:
        mapping = _run_mapping(state)
        await self._client().hset(self._run_key(state.run_id), mapping=mapping)
        await self._client().expire(self._run_key(state.run_id), self.retention_seconds)

    async def claim(self, state: RunState) -> bool:
        result = await self._client().eval(
            self._CLAIM,
            2,
            self._run_key(state.run_id),
            self._active_key(state.session_id),
            f"{self.prefix}:run:",
            state.run_id,
            state.session_id,
            state.kind,
            str(state.started_at),
            state.start_event_id,
            str(self.retention_seconds),
            str(self.active_ttl_seconds),
            str(time.time() - float(os.environ.get("RUN_STALE_SECONDS", "20"))),
        )
        return bool(result)

    async def get(self, run_id: str) -> RunState | None:
        values = await self._client().hgetall(self._run_key(run_id))
        return _run_from_mapping(values) if values else None

    async def active_run(self, session_id: str) -> RunState | None:
        run_id = await self._client().get(self._active_key(session_id))
        if not run_id:
            return None
        state = await self.get(str(run_id))
        return state if state and state.status == "running" else None

    async def request_cancel(self, run_id: str) -> bool:
        key = self._run_key(run_id)
        if await self._client().hget(key, "status") != "running":
            return False
        await self._client().hset(key, "cancel_requested", "1")
        return True

    async def is_cancelled(self, run_id: str) -> bool:
        return await self._client().hget(self._run_key(run_id), "cancel_requested") == "1"

    async def heartbeat(self, run_id: str) -> bool:
        result = await self._client().eval(
            self._HEARTBEAT,
            1,
            self._run_key(run_id),
            f"{self.prefix}:active:",
            run_id,
            str(self.retention_seconds),
            str(self.active_ttl_seconds),
            str(time.time()),
        )
        return bool(result)

    async def enqueue_followup(self, run_id: str, text: str) -> int:
        result = int(
            await self._client().eval(
                self._ENQUEUE,
                2,
                self._run_key(run_id),
                self._followup_key(run_id),
                text,
                str(self.active_ttl_seconds),
            )
        )
        if result < 0:
            raise RuntimeError("当前运行不接受补充要求")
        return result

    async def drain_followups(self, run_id: str) -> list[str]:
        values = await self._client().eval(self._DRAIN, 1, self._followup_key(run_id))
        return [str(value) for value in values]

    async def finish(self, run_id: str, status: str, error: str | None = None) -> None:
        state = await self.get(run_id)
        if not state:
            return
        await self._client().eval(
            self._FINISH,
            3,
            self._run_key(run_id),
            self._active_key(state.session_id),
            self._followup_key(run_id),
            status,
            str(time.time()),
            error or "",
            str(self.retention_seconds),
            run_id,
        )

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


def _run_mapping(state: RunState) -> dict[str, str]:
    return {
        "run_id": state.run_id,
        "session_id": state.session_id,
        "kind": state.kind,
        "status": state.status,
        "started_at": str(state.started_at),
        "finished_at": "" if state.finished_at is None else str(state.finished_at),
        "start_event_id": state.start_event_id,
        "error": state.error or "",
        "cancel_requested": "1" if state.cancel_requested else "0",
        "heartbeat_at": str(state.heartbeat_at),
    }


def _run_from_mapping(values: dict[str, str]) -> RunState:
    return RunState(
        run_id=values["run_id"],
        session_id=values["session_id"],
        kind=values.get("kind", "agent"),
        status=values.get("status", "running"),
        started_at=float(values.get("started_at") or time.time()),
        finished_at=float(values["finished_at"]) if values.get("finished_at") else None,
        start_event_id=values.get("start_event_id") or "0-0",
        error=values.get("error") or None,
        cancel_requested=values.get("cancel_requested") == "1",
        heartbeat_at=float(values.get("heartbeat_at") or values.get("started_at") or time.time()),
    )


@dataclass(frozen=True)
class StoredEvent:
    event_id: str
    payload: dict[str, Any]


@runtime_checkable
class EventStore(Protocol):
    async def start(self) -> None: ...
    async def append(self, session_id: str, payload: dict[str, Any]) -> str: ...
    async def read_after(self, session_id: str, event_id: str, limit: int = 1000) -> list[StoredEvent]: ...
    async def latest_id(self, session_id: str) -> str | None: ...
    async def oldest_id(self, session_id: str) -> str | None: ...
    async def close(self) -> None: ...


class InMemoryEventStore:
    """单进程实现；ID 采用与 Redis Stream 相同的 ``毫秒-序号`` 形式。"""

    def __init__(self, maxlen: int = 5000) -> None:
        self.maxlen = max(100, maxlen)
        self._streams: dict[str, deque[StoredEvent]] = defaultdict(lambda: deque(maxlen=self.maxlen))
        self._last: dict[str, tuple[int, int]] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        return None

    async def append(self, session_id: str, payload: dict[str, Any]) -> str:
        async with self._lock:
            now = int(time.time() * 1000)
            last_ms, last_seq = self._last.get(session_id, (0, -1))
            if now <= last_ms:
                now, seq = last_ms, last_seq + 1
            else:
                seq = 0
            event_id = f"{now}-{seq}"
            self._last[session_id] = (now, seq)
            self._streams[session_id].append(
                StoredEvent(event_id=event_id, payload=json.loads(json.dumps(payload, ensure_ascii=False, default=str)))
            )
            return event_id

    async def read_after(self, session_id: str, event_id: str, limit: int = 1000) -> list[StoredEvent]:
        cursor = _parse_event_id(event_id)
        async with self._lock:
            return [item for item in self._streams.get(session_id, ()) if _parse_event_id(item.event_id) > cursor][:limit]

    async def latest_id(self, session_id: str) -> str | None:
        async with self._lock:
            stream = self._streams.get(session_id)
            return stream[-1].event_id if stream else None

    async def oldest_id(self, session_id: str) -> str | None:
        async with self._lock:
            stream = self._streams.get(session_id)
            return stream[0].event_id if stream else None

    async def close(self) -> None:
        return None


class RedisStreamEventStore:
    """Redis Streams 实现；每个会话一条 Stream，ID 由 Redis 原子生成。"""

    def __init__(self, url: str, prefix: str = "dramaflow:events", maxlen: int = 5000) -> None:
        self.url = url
        self.prefix = prefix.rstrip(":")
        self.maxlen = max(100, maxlen)
        self._redis: Any = None

    def _key(self, session_id: str) -> str:
        return f"{self.prefix}:{session_id}"

    async def start(self) -> None:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - 取决于部署依赖
            raise RuntimeError("Redis 事件存储已启用，但未安装 redis 包；请执行 pip install -r requirements.txt") from exc
        self._redis = Redis.from_url(self.url, decode_responses=True)
        try:
            await self._redis.ping()
        except Exception as exc:  # noqa: BLE001
            await self._redis.aclose()
            self._redis = None
            raise RuntimeError(f"Redis 事件存储连接失败：{exc}") from exc

    def _client(self) -> Any:
        if self._redis is None:
            raise RuntimeError("Redis 事件存储尚未初始化")
        return self._redis

    async def append(self, session_id: str, payload: dict[str, Any]) -> str:
        value = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        event_id = await self._client().xadd(
            self._key(session_id), {"event": value}, maxlen=self.maxlen, approximate=True
        )
        return str(event_id)

    async def read_after(self, session_id: str, event_id: str, limit: int = 1000) -> list[StoredEvent]:
        _parse_event_id(event_id)
        rows = await self._client().xrange(self._key(session_id), min=f"({event_id}", max="+", count=limit)
        out: list[StoredEvent] = []
        for row_id, fields in rows:
            try:
                payload = json.loads(fields["event"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            out.append(StoredEvent(event_id=str(row_id), payload=payload))
        return out

    async def latest_id(self, session_id: str) -> str | None:
        rows = await self._client().xrevrange(self._key(session_id), count=1)
        return str(rows[0][0]) if rows else None

    async def oldest_id(self, session_id: str) -> str | None:
        rows = await self._client().xrange(self._key(session_id), count=1)
        return str(rows[0][0]) if rows else None

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


def _parse_event_id(value: str | None) -> tuple[int, int]:
    if value in (None, "", "0"):
        return 0, 0
    parts = str(value).split("-", 1)
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError("事件 ID 无效")
    return int(parts[0]), int(parts[1])


def validate_event_id(value: str | None) -> str:
    _parse_event_id(value)
    return value or "0-0"


def event_id_after(left: str, right: str) -> bool:
    return _parse_event_id(left) > _parse_event_id(right)


def create_event_store() -> EventStore:
    backend = os.environ.get("EVENT_STORE_BACKEND", "memory").strip().lower()
    maxlen = int(os.environ.get("EVENT_STREAM_MAXLEN", "5000"))
    if backend == "memory":
        return InMemoryEventStore(maxlen=maxlen)
    if backend == "redis":
        url = os.environ.get("REDIS_URL", "").strip()
        if not url:
            raise RuntimeError("EVENT_STORE_BACKEND=redis 时必须配置 REDIS_URL")
        return RedisStreamEventStore(
            url=url,
            prefix=os.environ.get("EVENT_STREAM_PREFIX", "dramaflow:events"),
            maxlen=maxlen,
        )
    raise RuntimeError(f"未知 EVENT_STORE_BACKEND：{backend}")


def create_run_state_store() -> RunStateStore:
    configured = os.environ.get("RUN_STATE_STORE_BACKEND", "").strip().lower()
    backend = configured or os.environ.get("EVENT_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryRunStateStore(limit=int(os.environ.get("RUN_STATE_LIMIT", "1000")))
    if backend == "redis":
        url = os.environ.get("REDIS_URL", "").strip()
        if not url:
            raise RuntimeError("RUN_STATE_STORE_BACKEND=redis 时必须配置 REDIS_URL")
        return RedisRunStateStore(
            url=url,
            prefix=os.environ.get("RUN_STATE_PREFIX", "dramaflow"),
            retention_seconds=int(os.environ.get("RUN_STATE_RETENTION_SECONDS", "86400")),
            active_ttl_seconds=int(os.environ.get("RUN_ACTIVE_TTL_SECONDS", "7200")),
        )
    raise RuntimeError(f"未知 RUN_STATE_STORE_BACKEND：{backend}")
