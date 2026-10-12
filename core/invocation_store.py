"""副作用工具调用与人工审批的可靠状态存储。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable

from .postgres import PostgresDatabase


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass
class InvocationState:
    invocation_id: str
    idempotency_key: str
    session_id: str
    run_id: str
    tool_name: str
    args_hash: str
    args_json: str
    status: str = "prepared"
    attempt: int = 0
    result_json: str | None = None
    error: str | None = None
    remote_operation_id: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@runtime_checkable
class ToolInvocationStore(Protocol):
    async def start(self) -> None: ...
    async def prepare(self, state: InvocationState) -> tuple[InvocationState, bool]: ...
    async def mark_executing(self, invocation_id: str) -> InvocationState: ...
    async def complete(self, invocation_id: str, result: dict[str, Any], *, failed: bool = False) -> None: ...
    async def set_remote_operation(self, invocation_id: str, operation_id: str) -> None: ...
    async def mark_unknown(self, invocation_id: str, error: str) -> None: ...
    async def get(self, invocation_id: str) -> InvocationState | None: ...
    async def close(self) -> None: ...


class InMemoryToolInvocationStore:
    def __init__(self) -> None:
        self._items: dict[str, InvocationState] = {}
        self._keys: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        return None

    async def prepare(self, state: InvocationState) -> tuple[InvocationState, bool]:
        async with self._lock:
            existing_id = self._keys.get(state.idempotency_key)
            if existing_id:
                return InvocationState(**asdict(self._items[existing_id])), False
            self._items[state.invocation_id] = state
            self._keys[state.idempotency_key] = state.invocation_id
            return InvocationState(**asdict(state)), True

    async def mark_executing(self, invocation_id: str) -> InvocationState:
        async with self._lock:
            state = self._items[invocation_id]
            if state.status != "prepared":
                raise RuntimeError(f"调用状态 {state.status} 不能进入 executing")
            state.status = "executing"
            state.attempt += 1
            state.updated_at = time.time()
            return InvocationState(**asdict(state))

    async def complete(self, invocation_id: str, result: dict[str, Any], *, failed: bool = False) -> None:
        async with self._lock:
            state = self._items[invocation_id]
            state.status = "failed" if failed else "succeeded"
            state.result_json = canonical_json(result)
            state.error = str(result.get("content") or "")[:2000] if failed else None
            state.updated_at = time.time()

    async def mark_unknown(self, invocation_id: str, error: str) -> None:
        async with self._lock:
            state = self._items.get(invocation_id)
            if state:
                state.status = "unknown"
                state.error = error[:2000]
                state.updated_at = time.time()

    async def set_remote_operation(self, invocation_id: str, operation_id: str) -> None:
        async with self._lock:
            state = self._items[invocation_id]
            state.remote_operation_id = operation_id[:500]
            state.updated_at = time.time()

    async def get(self, invocation_id: str) -> InvocationState | None:
        async with self._lock:
            state = self._items.get(invocation_id)
            return InvocationState(**asdict(state)) if state else None

    async def close(self) -> None:
        return None


@dataclass
class ApprovalState:
    approval_id: str
    scope: str
    args_hash: str
    payload_json: str
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 600)
    used_at: float | None = None


@runtime_checkable
class ApprovalStore(Protocol):
    async def start(self) -> None: ...
    async def create(self, state: ApprovalState) -> None: ...
    async def consume(self, approval_id: str, scope: str, args_hash: str) -> ApprovalState: ...
    async def close(self) -> None: ...


class InMemoryApprovalStore:
    def __init__(self) -> None:
        self._items: dict[str, ApprovalState] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        return None

    async def create(self, state: ApprovalState) -> None:
        async with self._lock:
            if state.approval_id in self._items:
                raise RuntimeError("审批 ID 已存在")
            self._items[state.approval_id] = state

    async def consume(self, approval_id: str, scope: str, args_hash: str) -> ApprovalState:
        async with self._lock:
            state = self._items.get(approval_id)
            if state is None:
                raise KeyError("审批不存在")
            if state.status == "consumed" and state.scope == scope and state.args_hash == args_hash:
                return ApprovalState(**asdict(state))
            if state.status != "pending":
                raise RuntimeError("审批已经使用或失效")
            if state.expires_at < time.time():
                state.status = "expired"
                raise RuntimeError("审批已过期")
            if state.scope != scope or state.args_hash != args_hash:
                raise ValueError("执行参数与用户审批的参数不一致")
            state.status = "consumed"
            state.used_at = time.time()
            return ApprovalState(**asdict(state))

    async def close(self) -> None:
        return None


class RedisToolInvocationStore:
    _PREPARE = """
    local existing = redis.call('GET', KEYS[2])
    if existing then return {existing, '0'} end
    for i = 3, #ARGV, 2 do redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1]) end
    redis.call('EXPIRE', KEYS[1], ARGV[1])
    redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[1])
    return {ARGV[2], '1'}
    """
    _MARK_EXECUTING = """
    if redis.call('EXISTS', KEYS[1]) == 0 then return -1 end
    if redis.call('HGET', KEYS[1], 'status') ~= 'prepared' then return 0 end
    redis.call('HSET', KEYS[1], 'status', 'executing', 'updated_at', ARGV[1])
    redis.call('HINCRBY', KEYS[1], 'attempt', 1)
    return 1
    """

    def __init__(self, url: str, prefix: str = "dramaflow", ttl_seconds: int = 604800) -> None:
        self.url, self.prefix = url, prefix.rstrip(":")
        self.ttl_seconds = max(3600, ttl_seconds)
        self._redis: Any = None

    def _key(self, invocation_id: str) -> str:
        return f"{self.prefix}:invocation:{invocation_id}"

    def _idem_key(self, value: str) -> str:
        return f"{self.prefix}:idempotency:{value}"

    async def start(self) -> None:
        from redis.asyncio import Redis
        self._redis = Redis.from_url(self.url, decode_responses=True)
        await self._redis.ping()

    def _client(self) -> Any:
        if self._redis is None:
            raise RuntimeError("Redis 调用存储尚未初始化")
        return self._redis

    async def prepare(self, state: InvocationState) -> tuple[InvocationState, bool]:
        mapping = _invocation_mapping(state)
        pairs = [value for item in mapping.items() for value in item]
        returned, created_flag = await self._client().eval(
            self._PREPARE, 2, self._key(state.invocation_id), self._idem_key(state.idempotency_key),
            str(self.ttl_seconds), state.invocation_id, *pairs,
        )
        returned = str(returned)
        created = str(created_flag) == "1"
        existing = await self.get(returned)
        if existing is None:
            raise RuntimeError("调用幂等索引损坏")
        return existing, created

    async def mark_executing(self, invocation_id: str) -> InvocationState:
        key = self._key(invocation_id)
        code = int(await self._client().eval(self._MARK_EXECUTING, 1, key, str(time.time())))
        if code == -1:
            raise KeyError("调用不存在")
        if code == 0:
            current = await self.get(invocation_id)
            raise RuntimeError(f"调用状态 {current.status if current else 'missing'} 不能进入 executing")
        state = await self.get(invocation_id)
        if state is None:
            raise KeyError("调用不存在")
        return state

    async def complete(self, invocation_id: str, result: dict[str, Any], *, failed: bool = False) -> None:
        await self._client().hset(self._key(invocation_id), mapping={
            "status": "failed" if failed else "succeeded",
            "result_json": canonical_json(result),
            "error": str(result.get("content") or "")[:2000] if failed else "",
            "updated_at": str(time.time()),
        })

    async def mark_unknown(self, invocation_id: str, error: str) -> None:
        await self._client().hset(self._key(invocation_id), mapping={
            "status": "unknown", "error": error[:2000], "updated_at": str(time.time())
        })

    async def set_remote_operation(self, invocation_id: str, operation_id: str) -> None:
        await self._client().hset(self._key(invocation_id), mapping={
            "remote_operation_id": operation_id[:500], "updated_at": str(time.time())
        })

    async def get(self, invocation_id: str) -> InvocationState | None:
        values = await self._client().hgetall(self._key(invocation_id))
        return _invocation_from_mapping(values) if values else None

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


class RedisApprovalStore:
    _CREATE = """
    if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
    for i = 2, #ARGV, 2 do redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1]) end
    redis.call('EXPIREAT', KEYS[1], ARGV[1])
    return 1
    """
    _CONSUME = """
    if redis.call('EXISTS', KEYS[1]) == 0 then return -1 end
    local status = redis.call('HGET', KEYS[1], 'status')
    if status == 'consumed' and redis.call('HGET', KEYS[1], 'scope') == ARGV[2] and redis.call('HGET', KEYS[1], 'args_hash') == ARGV[3] then return 2 end
    if status ~= 'pending' then return -2 end
    if tonumber(redis.call('HGET', KEYS[1], 'expires_at') or '0') < tonumber(ARGV[1]) then
      redis.call('HSET', KEYS[1], 'status', 'expired')
      return -3
    end
    if redis.call('HGET', KEYS[1], 'scope') ~= ARGV[2] or redis.call('HGET', KEYS[1], 'args_hash') ~= ARGV[3] then return -4 end
    redis.call('HSET', KEYS[1], 'status', 'consumed', 'used_at', ARGV[1])
    return 1
    """

    def __init__(self, url: str, prefix: str = "dramaflow") -> None:
        self.url, self.prefix, self._redis = url, prefix.rstrip(":"), None

    def _key(self, approval_id: str) -> str:
        return f"{self.prefix}:approval:{approval_id}"

    async def start(self) -> None:
        from redis.asyncio import Redis
        self._redis = Redis.from_url(self.url, decode_responses=True)
        await self._redis.ping()

    def _client(self) -> Any:
        if self._redis is None:
            raise RuntimeError("Redis 审批存储尚未初始化")
        return self._redis

    async def create(self, state: ApprovalState) -> None:
        key = self._key(state.approval_id)
        mapping = _approval_mapping(state)
        pairs = [value for item in mapping.items() for value in item]
        created = int(await self._client().eval(
            self._CREATE, 1, key, str(int(state.expires_at) + 3600), *pairs
        ))
        if not created:
            raise RuntimeError("审批 ID 已存在")

    async def consume(self, approval_id: str, scope: str, args_hash: str) -> ApprovalState:
        code = int(await self._client().eval(
            self._CONSUME, 1, self._key(approval_id), str(time.time()), scope, args_hash
        ))
        if code == -1:
            raise KeyError("审批不存在")
        if code == -2:
            raise RuntimeError("审批已经使用或失效")
        if code == -3:
            raise RuntimeError("审批已过期")
        if code == -4:
            raise ValueError("执行参数与用户审批的参数不一致")
        values = await self._client().hgetall(self._key(approval_id))
        return _approval_from_mapping(values)

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


class PostgresToolInvocationStore:
    """Durable invocation ledger. The unique idempotency key is the guard."""

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    async def start(self) -> None:
        await self.database.start()

    async def prepare(self, state: InvocationState) -> tuple[InvocationState, bool]:
        pool = self.database.client()
        created_id = await pool.fetchval(
            """INSERT INTO tool_invocations(
                   invocation_id,idempotency_key,session_id,run_id,tool_name,args_hash,args_json,
                   status,attempt,created_at,updated_at)
               VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11)
               ON CONFLICT (idempotency_key) DO NOTHING RETURNING invocation_id""",
            state.invocation_id, state.idempotency_key, state.session_id, state.run_id,
            state.tool_name, state.args_hash, state.args_json, state.status, state.attempt,
            state.created_at, state.updated_at,
        )
        row = await pool.fetchrow(
            """SELECT invocation_id,idempotency_key,session_id,run_id,tool_name,args_hash,
                      args_json::text AS args_json,status,attempt,result_json::text AS result_json,
                      error,remote_operation_id,created_at,updated_at
               FROM tool_invocations WHERE idempotency_key=$1""",
            state.idempotency_key,
        )
        if row is None:
            raise RuntimeError("调用幂等索引损坏")
        return _invocation_from_mapping(dict(row)), created_id is not None

    async def mark_executing(self, invocation_id: str) -> InvocationState:
        row = await self.database.client().fetchrow(
            """UPDATE tool_invocations
               SET status='executing',attempt=attempt+1,updated_at=$2
               WHERE invocation_id=$1 AND status='prepared'
               RETURNING invocation_id,idempotency_key,session_id,run_id,tool_name,args_hash,
                         args_json::text AS args_json,status,attempt,result_json::text AS result_json,
                         error,remote_operation_id,created_at,updated_at""",
            invocation_id, time.time(),
        )
        if row is None:
            current = await self.get(invocation_id)
            if current is None:
                raise KeyError("调用不存在")
            raise RuntimeError(f"调用状态 {current.status} 不能进入 executing")
        return _invocation_from_mapping(dict(row))

    async def complete(self, invocation_id: str, result: dict[str, Any], *, failed: bool = False) -> None:
        status = await self.database.client().execute(
            """UPDATE tool_invocations SET status=$2,result_json=$3::jsonb,error=$4,updated_at=$5
               WHERE invocation_id=$1""",
            invocation_id, "failed" if failed else "succeeded", canonical_json(result),
            str(result.get("content") or "")[:2000] if failed else None, time.time(),
        )
        if status == "UPDATE 0":
            raise KeyError("调用不存在")

    async def set_remote_operation(self, invocation_id: str, operation_id: str) -> None:
        status = await self.database.client().execute(
            "UPDATE tool_invocations SET remote_operation_id=$2,updated_at=$3 WHERE invocation_id=$1",
            invocation_id, operation_id[:500], time.time(),
        )
        if status == "UPDATE 0":
            raise KeyError("调用不存在")

    async def mark_unknown(self, invocation_id: str, error: str) -> None:
        await self.database.client().execute(
            "UPDATE tool_invocations SET status='unknown',error=$2,updated_at=$3 WHERE invocation_id=$1",
            invocation_id, error[:2000], time.time(),
        )

    async def get(self, invocation_id: str) -> InvocationState | None:
        row = await self.database.client().fetchrow(
            """SELECT invocation_id,idempotency_key,session_id,run_id,tool_name,args_hash,
                      args_json::text AS args_json,status,attempt,result_json::text AS result_json,
                      error,remote_operation_id,created_at,updated_at
               FROM tool_invocations WHERE invocation_id=$1""",
            invocation_id,
        )
        return _invocation_from_mapping(dict(row)) if row else None

    async def close(self) -> None:
        await self.database.close()


class PostgresApprovalStore:
    """Approval consumption is serialized with SELECT FOR UPDATE."""

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    async def start(self) -> None:
        await self.database.start()

    async def create(self, state: ApprovalState) -> None:
        try:
            await self.database.client().execute(
                """INSERT INTO approval_requests(
                       approval_id,scope,args_hash,payload_json,status,created_at,expires_at,used_at)
                   VALUES($1,$2,$3,$4::jsonb,$5,$6,$7,$8)""",
                state.approval_id, state.scope, state.args_hash, state.payload_json,
                state.status, state.created_at, state.expires_at, state.used_at,
            )
        except Exception as exc:
            # Avoid importing asyncpg at module import time in memory-only mode.
            if getattr(exc, "sqlstate", None) == "23505":
                raise RuntimeError("审批 ID 已存在") from exc
            raise

    async def consume(self, approval_id: str, scope: str, args_hash: str) -> ApprovalState:
        pool = self.database.client()
        expired = False
        state: ApprovalState | None = None
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """SELECT approval_id,scope,args_hash,payload_json::text AS payload_json,
                              status,created_at,expires_at,used_at
                       FROM approval_requests WHERE approval_id=$1 FOR UPDATE""",
                    approval_id,
                )
                if row is None:
                    raise KeyError("审批不存在")
                state = _approval_from_mapping(dict(row))
                if state.status == "consumed" and state.scope == scope and state.args_hash == args_hash:
                    return state
                if state.status != "pending":
                    raise RuntimeError("审批已经使用或失效")
                now = time.time()
                if state.expires_at < now:
                    await connection.execute(
                        "UPDATE approval_requests SET status='expired' WHERE approval_id=$1", approval_id
                    )
                    state.status = "expired"
                    expired = True
                elif state.scope != scope or state.args_hash != args_hash:
                    raise ValueError("执行参数与用户审批的参数不一致")
                else:
                    await connection.execute(
                        "UPDATE approval_requests SET status='consumed',used_at=$2 WHERE approval_id=$1",
                        approval_id, now,
                    )
                    state.status, state.used_at = "consumed", now
        if expired:
            raise RuntimeError("审批已过期")
        if state is None:
            raise KeyError("审批不存在")
        return state

    async def close(self) -> None:
        await self.database.close()


def _invocation_mapping(state: InvocationState) -> dict[str, str]:
    return {key: "" if value is None else str(value) for key, value in asdict(state).items()}


def _invocation_from_mapping(v: dict[str, str]) -> InvocationState:
    return InvocationState(
        invocation_id=v["invocation_id"], idempotency_key=v["idempotency_key"],
        session_id=v.get("session_id", ""), run_id=v.get("run_id", ""), tool_name=v["tool_name"],
        args_hash=v["args_hash"], args_json=v["args_json"], status=v.get("status", "prepared"),
        attempt=int(v.get("attempt") or 0), result_json=v.get("result_json") or None,
        error=v.get("error") or None, remote_operation_id=v.get("remote_operation_id") or None,
        created_at=float(v.get("created_at") or time.time()), updated_at=float(v.get("updated_at") or time.time()),
    )


def _approval_mapping(state: ApprovalState) -> dict[str, str]:
    return {key: "" if value is None else str(value) for key, value in asdict(state).items()}


def _approval_from_mapping(v: dict[str, str]) -> ApprovalState:
    return ApprovalState(
        approval_id=v["approval_id"], scope=v["scope"], args_hash=v["args_hash"],
        payload_json=v["payload_json"], status=v.get("status", "pending"),
        created_at=float(v.get("created_at") or time.time()), expires_at=float(v["expires_at"]),
        used_at=float(v["used_at"]) if v.get("used_at") else None,
    )


def create_invocation_stores() -> tuple[ToolInvocationStore, ApprovalStore]:
    backend = os.environ.get("INVOCATION_STORE_BACKEND", "").strip().lower()
    backend = backend or os.environ.get("RUN_STATE_STORE_BACKEND", "").strip().lower()
    backend = backend or os.environ.get("EVENT_STORE_BACKEND", "memory").strip().lower() or "memory"
    if backend == "memory":
        return InMemoryToolInvocationStore(), InMemoryApprovalStore()
    if backend == "redis":
        url = os.environ.get("REDIS_URL", "").strip()
        if not url:
            raise RuntimeError("INVOCATION_STORE_BACKEND=redis 时必须配置 REDIS_URL")
        prefix = os.environ.get("RUN_STATE_PREFIX", "dramaflow")
        return (
            RedisToolInvocationStore(url, prefix, int(os.environ.get("INVOCATION_TTL_SECONDS", "604800"))),
            RedisApprovalStore(url, prefix),
        )
    if backend in {"postgres", "postgresql"}:
        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError("INVOCATION_STORE_BACKEND=postgres 时必须配置 DATABASE_URL")
        database = PostgresDatabase(
            url,
            min_size=int(os.environ.get("POSTGRES_POOL_MIN_SIZE", "1")),
            max_size=int(os.environ.get("POSTGRES_POOL_MAX_SIZE", "10")),
        )
        return PostgresToolInvocationStore(database), PostgresApprovalStore(database)
    raise RuntimeError(f"未知 INVOCATION_STORE_BACKEND：{backend}")


def new_approval(scope: str, payload: Any, ttl_seconds: int = 600) -> ApprovalState:
    now = time.time()
    return ApprovalState(
        approval_id=f"apr_{uuid.uuid4().hex}", scope=scope,
        args_hash=canonical_hash(payload), payload_json=canonical_json(payload),
        created_at=now, expires_at=now + ttl_seconds,
    )
