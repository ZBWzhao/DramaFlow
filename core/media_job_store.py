"""长耗时媒体任务的队列、租约与 Worker。

任务终态语义（前端与运维都依赖这四个状态，改动前先看这里）：

- ``succeeded``：执行完成，且结果**全部**写回了画布；
- ``failed``：执行完成，但有节点的生成/写回真的失败（可重试、可换 provider）；
- ``discarded``：执行完成，但产物被新一代任务取代而整份丢弃（Provider 已扣费，
  不能算成功，也不该让用户重试同一计划）；
- ``unknown``：Worker 失联/超时，远端是否执行不确定，禁止自动重试。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from .postgres import PostgresDatabase


class MediaJobUncertain(RuntimeError):
    """Provider 可能已经执行，但本地无法确认最终状态。"""


@dataclass
class MediaJob:
    job_id: str
    idempotency_key: str
    approval_id: str
    canvas: str
    payload_json: str
    status: str = "queued"
    attempt: int = 0
    worker_id: str = ""
    lease_until: float = 0.0
    result_json: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def public(self) -> dict[str, Any]:
        result = json.loads(self.result_json) if self.result_json else None
        payload = json.loads(self.payload_json) if self.payload_json else {}
        jobs = payload.get("jobs") if isinstance(payload, dict) else []
        jobs = jobs if isinstance(jobs, list) else []
        return {
            "job_id": self.job_id,
            "canvas": self.canvas,
            "status": self.status,
            "attempt": self.attempt,
            "result": result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total": len(jobs),
            "ready": sum(1 for item in jobs if isinstance(item, dict) and item.get("ready")),
        }


@runtime_checkable
class MediaJobStore(Protocol):
    async def start(self) -> None: ...
    async def enqueue(self, job: MediaJob) -> tuple[MediaJob, bool]: ...
    async def claim(self, worker_id: str, lease_seconds: int) -> MediaJob | None: ...
    async def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int) -> bool: ...
    async def complete(
        self, job_id: str, worker_id: str, result: dict[str, Any], *,
        failed: bool = False, discarded: bool = False,
    ) -> None: ...
    async def mark_unknown(self, job_id: str, worker_id: str, error: str) -> None: ...
    async def requeue_unknown(self, job_id: str) -> MediaJob: ...
    async def get(self, job_id: str) -> MediaJob | None: ...
    async def close(self) -> None: ...


class InMemoryMediaJobStore:
    def __init__(self) -> None:
        self._items: dict[str, MediaJob] = {}
        self._keys: dict[str, str] = {}
        self._queue: deque[str] = deque()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        return None

    async def enqueue(self, job: MediaJob) -> tuple[MediaJob, bool]:
        async with self._lock:
            existing_id = self._keys.get(job.idempotency_key)
            if existing_id:
                return MediaJob(**asdict(self._items[existing_id])), False
            self._items[job.job_id] = job
            self._keys[job.idempotency_key] = job.job_id
            self._queue.append(job.job_id)
            return MediaJob(**asdict(job)), True

    async def claim(self, worker_id: str, lease_seconds: int) -> MediaJob | None:
        async with self._lock:
            now = time.time()
            for state in self._items.values():
                if state.status == "running" and state.lease_until < now:
                    state.status = "unknown"
                    state.error = "Worker 租约过期，远端执行结果不确定，禁止自动重试"
                    state.finished_at = state.updated_at = now
            while self._queue:
                job_id = self._queue.popleft()
                state = self._items.get(job_id)
                if state is None or state.status != "queued":
                    continue
                state.status = "running"
                state.worker_id = worker_id
                state.lease_until = now + lease_seconds
                state.attempt += 1
                state.started_at = state.started_at or now
                state.updated_at = now
                return MediaJob(**asdict(state))
            return None

    async def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        async with self._lock:
            state = self._items.get(job_id)
            if state is None or state.status != "running" or state.worker_id != worker_id:
                return False
            state.lease_until = time.time() + lease_seconds
            state.updated_at = time.time()
            return True

    async def complete(
        self, job_id: str, worker_id: str, result: dict[str, Any], *,
        failed: bool = False, discarded: bool = False,
    ) -> None:
        async with self._lock:
            state = self._items[job_id]
            if state.status != "running" or state.worker_id != worker_id:
                raise RuntimeError("媒体任务租约已失效，拒绝覆盖结果")
            if discarded:
                state.status = "discarded"
            elif failed:
                state.status = "failed"
            else:
                state.status = "succeeded"
            state.result_json = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
            state.error = str(result.get("error") or "")[:2000] if failed else None
            state.finished_at = state.updated_at = time.time()
            state.lease_until = 0.0

    async def mark_unknown(self, job_id: str, worker_id: str, error: str) -> None:
        async with self._lock:
            state = self._items.get(job_id)
            if state and state.status == "running" and state.worker_id == worker_id:
                state.status = "unknown"
                state.error = error[:2000]
                state.finished_at = state.updated_at = time.time()
                state.lease_until = 0.0

    async def get(self, job_id: str) -> MediaJob | None:
        async with self._lock:
            state = self._items.get(job_id)
            return MediaJob(**asdict(state)) if state else None

    async def requeue_unknown(self, job_id: str) -> MediaJob:
        async with self._lock:
            state = self._items[job_id]
            if state.status != "unknown":
                raise RuntimeError("只有 unknown 媒体任务可以人工恢复")
            state.status = "queued"
            state.worker_id = ""
            state.lease_until = 0.0
            state.error = None
            state.finished_at = None
            state.updated_at = time.time()
            self._queue.append(job_id)
            return MediaJob(**asdict(state))

    async def close(self) -> None:
        return None


class RedisMediaJobStore:
    _ENQUEUE = """
    local existing = redis.call('GET', KEYS[2])
    if existing then return {existing, '0'} end
    for i = 4, #ARGV, 2 do redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1]) end
    redis.call('EXPIRE', KEYS[1], ARGV[1])
    redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[1])
    redis.call('ZADD', KEYS[3], ARGV[3], ARGV[2])
    return {ARGV[2], '1'}
    """
    _CLAIM = """
    local stale = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', ARGV[1])
    for _, id in ipairs(stale) do
      local key = ARGV[4] .. id
      if redis.call('HGET', key, 'status') == 'running' then
        redis.call('HSET', key, 'status', 'unknown', 'error', ARGV[5], 'finished_at', ARGV[1], 'updated_at', ARGV[1], 'lease_until', '0')
      end
      redis.call('ZREM', KEYS[2], id)
    end
    local rows = redis.call('ZRANGE', KEYS[1], 0, 0)
    if #rows == 0 then return '' end
    local id = rows[1]
    redis.call('ZREM', KEYS[1], id)
    local key = ARGV[4] .. id
    if redis.call('HGET', key, 'status') ~= 'queued' then return '' end
    redis.call('HSET', key, 'status', 'running', 'worker_id', ARGV[2], 'lease_until', ARGV[3], 'updated_at', ARGV[1])
    redis.call('HINCRBY', key, 'attempt', 1)
    local started = redis.call('HGET', key, 'started_at')
    if not started or started == '' then redis.call('HSET', key, 'started_at', ARGV[1]) end
    redis.call('ZADD', KEYS[2], ARGV[3], id)
    return id
    """
    _HEARTBEAT = """
    if redis.call('HGET', KEYS[1], 'status') ~= 'running' or redis.call('HGET', KEYS[1], 'worker_id') ~= ARGV[1] then return 0 end
    redis.call('HSET', KEYS[1], 'lease_until', ARGV[2], 'updated_at', ARGV[3])
    redis.call('ZADD', KEYS[2], ARGV[2], ARGV[4])
    return 1
    """
    _FINISH = """
    if redis.call('HGET', KEYS[1], 'status') ~= 'running' or redis.call('HGET', KEYS[1], 'worker_id') ~= ARGV[1] then return 0 end
    redis.call('HSET', KEYS[1], 'status', ARGV[2], 'result_json', ARGV[3], 'error', ARGV[4], 'finished_at', ARGV[5], 'updated_at', ARGV[5], 'lease_until', '0')
    redis.call('ZREM', KEYS[2], ARGV[6])
    return 1
    """
    _REQUEUE = """
    if redis.call('HGET', KEYS[1], 'status') ~= 'unknown' then return 0 end
    redis.call('HSET', KEYS[1], 'status', 'queued', 'worker_id', '', 'lease_until', '0', 'error', '', 'finished_at', '', 'updated_at', ARGV[1])
    redis.call('ZADD', KEYS[2], ARGV[1], ARGV[2])
    return 1
    """

    def __init__(self, url: str, prefix: str = "dramaflow", ttl_seconds: int = 604800) -> None:
        self.url, self.prefix = url, prefix.rstrip(":")
        self.ttl_seconds = max(3600, ttl_seconds)
        self._redis: Any = None

    def _key(self, job_id: str) -> str:
        return f"{self.prefix}:media-job:{job_id}"

    @property
    def _ready(self) -> str:
        return f"{self.prefix}:media-jobs:ready"

    @property
    def _leases(self) -> str:
        return f"{self.prefix}:media-jobs:leases"

    async def start(self) -> None:
        from redis.asyncio import Redis
        self._redis = Redis.from_url(self.url, decode_responses=True)
        await self._redis.ping()

    def _client(self) -> Any:
        if self._redis is None:
            raise RuntimeError("Redis 媒体任务存储尚未初始化")
        return self._redis

    async def enqueue(self, job: MediaJob) -> tuple[MediaJob, bool]:
        mapping = _mapping(job)
        pairs = [value for item in mapping.items() for value in item]
        idem_key = f"{self.prefix}:media-job-idem:{job.idempotency_key}"
        returned, flag = await self._client().eval(
            self._ENQUEUE, 3, self._key(job.job_id), idem_key, self._ready,
            str(self.ttl_seconds), job.job_id, str(job.created_at), *pairs,
        )
        existing = await self.get(str(returned))
        if existing is None:
            raise RuntimeError("媒体任务幂等索引损坏")
        return existing, str(flag) == "1"

    async def claim(self, worker_id: str, lease_seconds: int) -> MediaJob | None:
        now = time.time()
        job_id = str(await self._client().eval(
            self._CLAIM, 2, self._ready, self._leases,
            str(now), worker_id, str(now + lease_seconds), f"{self.prefix}:media-job:",
            "Worker 租约过期，远端执行结果不确定，禁止自动重试",
        ))
        return await self.get(job_id) if job_id else None

    async def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        now = time.time()
        code = await self._client().eval(
            self._HEARTBEAT, 2, self._key(job_id), self._leases,
            worker_id, str(now + lease_seconds), str(now), job_id,
        )
        return bool(int(code))

    async def complete(
        self, job_id: str, worker_id: str, result: dict[str, Any], *,
        failed: bool = False, discarded: bool = False,
    ) -> None:
        now = time.time()
        status = "discarded" if discarded else ("failed" if failed else "succeeded")
        error = str(result.get("error") or "")[:2000] if failed else ""
        code = await self._client().eval(
            self._FINISH, 2, self._key(job_id), self._leases,
            worker_id, status,
            json.dumps(result, ensure_ascii=False, sort_keys=True, default=str), error, str(now), job_id,
        )
        if not int(code):
            raise RuntimeError("媒体任务租约已失效，拒绝覆盖结果")

    async def mark_unknown(self, job_id: str, worker_id: str, error: str) -> None:
        await self.complete(job_id, worker_id, {"error": error}, failed=True)
        await self._client().hset(self._key(job_id), mapping={"status": "unknown"})

    async def get(self, job_id: str) -> MediaJob | None:
        if not job_id:
            return None
        values = await self._client().hgetall(self._key(job_id))
        return _from_mapping(values) if values else None

    async def requeue_unknown(self, job_id: str) -> MediaJob:
        code = await self._client().eval(
            self._REQUEUE, 2, self._key(job_id), self._ready, str(time.time()), job_id
        )
        if not int(code):
            raise RuntimeError("只有 unknown 媒体任务可以人工恢复")
        state = await self.get(job_id)
        if state is None:
            raise KeyError("媒体任务不存在")
        return state

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


class PostgresMediaJobStore:
    """Durable queue using row locks; safe for multiple API/worker processes."""

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    async def start(self) -> None:
        await self.database.start()

    async def enqueue(self, job: MediaJob) -> tuple[MediaJob, bool]:
        pool = self.database.client()
        created_id = await pool.fetchval(
            """INSERT INTO media_jobs(
                   job_id,idempotency_key,approval_id,canvas,payload_json,status,attempt,
                   worker_id,lease_until,created_at,updated_at)
               VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,$11)
               ON CONFLICT (idempotency_key) DO NOTHING RETURNING job_id""",
            job.job_id, job.idempotency_key, job.approval_id, job.canvas, job.payload_json,
            job.status, job.attempt, job.worker_id, job.lease_until, job.created_at, job.updated_at,
        )
        state = await self._get_by_key(job.idempotency_key)
        if state is None:
            raise RuntimeError("媒体任务幂等索引损坏")
        return state, created_id is not None

    async def claim(self, worker_id: str, lease_seconds: int) -> MediaJob | None:
        now = time.time()
        pool = self.database.client()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """UPDATE media_jobs SET status='unknown',
                           error='Worker 租约过期，远端执行结果不确定，禁止自动重试',
                           finished_at=$1,updated_at=$1,lease_until=0
                       WHERE status='running' AND lease_until < $1""",
                    now,
                )
                row = await connection.fetchrow(
                    """SELECT job_id FROM media_jobs WHERE status='queued'
                       ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"""
                )
                if row is None:
                    return None
                result = await connection.fetchrow(
                    """UPDATE media_jobs SET status='running',worker_id=$2,lease_until=$3,
                              attempt=attempt+1,started_at=COALESCE(started_at,$4),updated_at=$4
                       WHERE job_id=$1 RETURNING job_id,idempotency_key,approval_id,canvas,
                              payload_json::text AS payload_json,status,attempt,worker_id,lease_until,
                              result_json::text AS result_json,error,created_at,updated_at,started_at,finished_at""",
                    row["job_id"], worker_id, now + lease_seconds, now,
                )
                return _from_mapping(dict(result))

    async def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        now = time.time()
        status = await self.database.client().execute(
            """UPDATE media_jobs SET lease_until=$3,updated_at=$4
               WHERE job_id=$1 AND worker_id=$2 AND status='running'""",
            job_id, worker_id, now + lease_seconds, now,
        )
        return status != "UPDATE 0"

    async def complete(
        self, job_id: str, worker_id: str, result: dict[str, Any], *,
        failed: bool = False, discarded: bool = False,
    ) -> None:
        now = time.time()
        status = "discarded" if discarded else ("failed" if failed else "succeeded")
        updated = await self.database.client().execute(
            """UPDATE media_jobs SET status=$3,result_json=$4::jsonb,error=$5,
                      finished_at=$6,updated_at=$6,lease_until=0
               WHERE job_id=$1 AND worker_id=$2 AND status='running'""",
            job_id, worker_id, status,
            json.dumps(result, ensure_ascii=False, sort_keys=True, default=str),
            str(result.get("error") or "")[:2000] if failed else None, now,
        )
        if updated == "UPDATE 0":
            raise RuntimeError("媒体任务租约已失效，拒绝覆盖结果")

    async def mark_unknown(self, job_id: str, worker_id: str, error: str) -> None:
        await self.database.client().execute(
            """UPDATE media_jobs SET status='unknown',error=$3,finished_at=$4,updated_at=$4,lease_until=0
               WHERE job_id=$1 AND worker_id=$2 AND status='running'""",
            job_id, worker_id, error[:2000], time.time(),
        )

    async def requeue_unknown(self, job_id: str) -> MediaJob:
        row = await self.database.client().fetchrow(
            """UPDATE media_jobs SET status='queued',worker_id='',lease_until=0,error=NULL,
                      finished_at=NULL,updated_at=$2 WHERE job_id=$1 AND status='unknown'
               RETURNING job_id,idempotency_key,approval_id,canvas,payload_json::text AS payload_json,
                         status,attempt,worker_id,lease_until,result_json::text AS result_json,error,
                         created_at,updated_at,started_at,finished_at""",
            job_id, time.time(),
        )
        if row is None:
            raise RuntimeError("只有 unknown 媒体任务可以人工恢复")
        return _from_mapping(dict(row))

    async def get(self, job_id: str) -> MediaJob | None:
        row = await self.database.client().fetchrow(
            """SELECT job_id,idempotency_key,approval_id,canvas,payload_json::text AS payload_json,
                      status,attempt,worker_id,lease_until,result_json::text AS result_json,error,
                      created_at,updated_at,started_at,finished_at FROM media_jobs WHERE job_id=$1""",
            job_id,
        )
        return _from_mapping(dict(row)) if row else None

    async def _get_by_key(self, idempotency_key: str) -> MediaJob | None:
        row = await self.database.client().fetchrow(
            """SELECT job_id,idempotency_key,approval_id,canvas,payload_json::text AS payload_json,
                      status,attempt,worker_id,lease_until,result_json::text AS result_json,error,
                      created_at,updated_at,started_at,finished_at
               FROM media_jobs WHERE idempotency_key=$1""",
            idempotency_key,
        )
        return _from_mapping(dict(row)) if row else None

    async def close(self) -> None:
        await self.database.close()


class MediaJobWorker:
    def __init__(
        self,
        store: MediaJobStore,
        execute: Callable[[MediaJob], Awaitable[dict[str, Any]]],
        *,
        lease_seconds: int = 30,
        poll_seconds: float = 0.5,
    ) -> None:
        self.store, self.execute = store, execute
        self.lease_seconds = max(10, lease_seconds)
        self.poll_seconds = max(0.1, poll_seconds)
        self.worker_id = f"worker-{uuid.uuid4().hex[:12]}"
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"media-worker:{self.worker_id}")

    async def close(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _heartbeat(self, job_id: str) -> None:
        while True:
            await asyncio.sleep(max(1.0, self.lease_seconds / 3))
            if not await self.store.heartbeat(job_id, self.worker_id, self.lease_seconds):
                return

    async def _run(self) -> None:
        current: MediaJob | None = None
        while not self._stopping.is_set():
            current = await self.store.claim(self.worker_id, self.lease_seconds)
            if current is None:
                await asyncio.sleep(self.poll_seconds)
                continue
            heartbeat = asyncio.create_task(self._heartbeat(current.job_id))
            try:
                result = await self.execute(current)
                failed, discarded = _classify_result(result)
                if failed and isinstance(result, dict) and not result.get("error"):
                    # 批次里各节点的真实失败原因要能被 /api/media/jobs 读到，否则前端只能显示"未完成"。
                    reasons = "；".join(
                        str(item.get("error"))
                        for item in result.get("results") or []
                        if isinstance(item, dict) and not item.get("ok")
                    )
                    result["error"] = f"有节点未完成：{reasons}" if reasons else "媒体任务未全部完成"
                await self.store.complete(
                    current.job_id, self.worker_id, result,
                    failed=failed, discarded=discarded,
                )
            except asyncio.CancelledError:
                await self.store.mark_unknown(current.job_id, self.worker_id, "Worker 关闭时任务仍在执行，结果不确定")
                raise
            except MediaJobUncertain as exc:
                await self.store.mark_unknown(current.job_id, self.worker_id, str(exc))
            except Exception as exc:  # noqa: BLE001
                await self.store.complete(
                    current.job_id, self.worker_id,
                    {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, failed=True,
                )
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                current = None


def _classify_result(result: dict[str, Any]) -> tuple[bool, bool]:
    """把执行结果翻译成 ``(failed, discarded)`` 两个终态位。

    规则（宁可少报成功，也不要把"没写回"记成成功）：

    - ``ok=True`` → 成功；
    - 所有非成功条目都带 ``stale_discarded`` → ``discarded``（产物被新一代取代，钱已花）；
    - 其余（真的有节点失败）→ ``failed``。
    """
    if not isinstance(result, dict):
        return False, False
    if result.get("ok"):
        return False, False
    entries = [item for item in result.get("results") or [] if isinstance(item, dict)]
    if entries and all(item.get("ok") or item.get("stale_discarded") for item in entries):
        return False, True
    return True, False


def _mapping(job: MediaJob) -> dict[str, str]:
    return {key: "" if value is None else str(value) for key, value in asdict(job).items()}


def _from_mapping(v: dict[str, str]) -> MediaJob:
    return MediaJob(
        job_id=v["job_id"], idempotency_key=v["idempotency_key"], approval_id=v["approval_id"],
        canvas=v["canvas"], payload_json=v["payload_json"], status=v.get("status", "queued"),
        attempt=int(v.get("attempt") or 0), worker_id=v.get("worker_id", ""),
        lease_until=float(v.get("lease_until") or 0), result_json=v.get("result_json") or None,
        error=v.get("error") or None, created_at=float(v.get("created_at") or time.time()),
        updated_at=float(v.get("updated_at") or time.time()),
        started_at=float(v["started_at"]) if v.get("started_at") else None,
        finished_at=float(v["finished_at"]) if v.get("finished_at") else None,
    )


def create_media_job_store() -> MediaJobStore:
    backend = os.environ.get("MEDIA_JOB_STORE_BACKEND", "").strip().lower()
    backend = backend or os.environ.get("INVOCATION_STORE_BACKEND", "").strip().lower()
    backend = backend or os.environ.get("EVENT_STORE_BACKEND", "memory").strip().lower() or "memory"
    if backend == "memory":
        return InMemoryMediaJobStore()
    if backend == "redis":
        url = os.environ.get("REDIS_URL", "").strip()
        if not url:
            raise RuntimeError("MEDIA_JOB_STORE_BACKEND=redis 时必须配置 REDIS_URL")
        return RedisMediaJobStore(
            url,
            os.environ.get("RUN_STATE_PREFIX", "dramaflow"),
            int(os.environ.get("MEDIA_JOB_TTL_SECONDS", "604800")),
        )
    if backend in {"postgres", "postgresql"}:
        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError("MEDIA_JOB_STORE_BACKEND=postgres 时必须配置 DATABASE_URL")
        return PostgresMediaJobStore(PostgresDatabase(
            url,
            min_size=int(os.environ.get("POSTGRES_POOL_MIN_SIZE", "1")),
            max_size=int(os.environ.get("POSTGRES_POOL_MAX_SIZE", "10")),
        ))
    raise RuntimeError(f"未知 MEDIA_JOB_STORE_BACKEND：{backend}")
