"""PostgreSQL transactional Outbox -> Redis Streams publisher."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from typing import Any

from .postgres import PostgresDatabase


@dataclass
class OutboxRecord:
    outbox_id: int
    event_key: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: dict[str, Any]
    attempts: int


class PostgresRedisOutboxPublisher:
    """At-least-once DB polling with Redis-side event-key deduplication."""

    _PUBLISH_LUA = """
    local existing = redis.call('GET', KEYS[2])
    if existing then return existing end
    local stream_id = redis.call('XADD', KEYS[1], 'MAXLEN', '~', ARGV[1], '*',
      'event_key', ARGV[2], 'aggregate_type', ARGV[3], 'aggregate_id', ARGV[4],
      'event_type', ARGV[5], 'payload', ARGV[6])
    redis.call('SET', KEYS[2], stream_id, 'EX', ARGV[7])
    return stream_id
    """

    def __init__(
        self,
        database: PostgresDatabase,
        redis_url: str,
        *,
        stream: str = "dramaflow:outbox",
        batch_size: int = 50,
        lease_seconds: int = 30,
        maxlen: int = 20000,
    ) -> None:
        self.database = database
        self.redis_url = redis_url
        self.stream = stream
        self.batch_size = max(1, batch_size)
        self.lease_seconds = max(5, lease_seconds)
        self.maxlen = max(100, maxlen)
        self.owner = f"outbox-{uuid.uuid4().hex[:12]}"
        self.redis: Any = None
        self.task: asyncio.Task[None] | None = None
        self.published = 0
        self.failures = 0
        self.last_error: str | None = None

    async def start(self) -> None:
        from redis.asyncio import Redis

        self.redis = Redis.from_url(self.redis_url, decode_responses=True)
        await self.redis.ping()
        self.task = asyncio.create_task(self._run(), name="outbox-publisher")

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.redis is not None:
            await self.redis.aclose()
            self.redis = None

    async def _run(self) -> None:
        while True:
            try:
                records = await self.publish_once()
                await asyncio.sleep(0.1 if records else 0.75)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the publisher alive across transient DB/Redis failures
                self.failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(1.0)

    async def claim(self) -> list[OutboxRecord]:
        pool = self.database.client()
        rows = await pool.fetch(
            """
            WITH candidates AS (
              SELECT outbox_id FROM outbox_events
              WHERE published_at IS NULL
                AND available_at <= now()
                AND (lease_until IS NULL OR lease_until < now())
              ORDER BY outbox_id
              LIMIT $1
              FOR UPDATE SKIP LOCKED
            )
            UPDATE outbox_events o
            SET lease_owner=$2, lease_until=now() + ($3 * interval '1 second')
            FROM candidates c
            WHERE o.outbox_id=c.outbox_id
            RETURNING o.outbox_id, o.event_key, o.aggregate_type, o.aggregate_id,
                      o.event_type, o.payload, o.attempts
            """,
            self.batch_size,
            self.owner,
            self.lease_seconds,
        )
        return [
            OutboxRecord(
                outbox_id=int(row["outbox_id"]),
                event_key=str(row["event_key"]),
                aggregate_type=str(row["aggregate_type"]),
                aggregate_id=str(row["aggregate_id"]),
                event_type=str(row["event_type"]),
                payload=json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"]),
                attempts=int(row["attempts"] or 0),
            )
            for row in rows
        ]

    async def publish_once(self) -> int:
        records = await self.claim()
        for record in records:
            try:
                await self._publish(record)
                await self.database.client().execute(
                    """
                    UPDATE outbox_events SET published_at=now(), lease_owner=NULL,
                      lease_until=NULL, last_error=NULL
                    WHERE outbox_id=$1 AND lease_owner=$2 AND published_at IS NULL
                    """,
                    record.outbox_id,
                    self.owner,
                )
                self.published += 1
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                delay = min(60, 2 ** min(record.attempts + 1, 6))
                await self.database.client().execute(
                    """
                    UPDATE outbox_events SET attempts=attempts+1, last_error=$3,
                      available_at=now() + ($4 * interval '1 second'),
                      lease_owner=NULL, lease_until=NULL
                    WHERE outbox_id=$1 AND lease_owner=$2 AND published_at IS NULL
                    """,
                    record.outbox_id,
                    self.owner,
                    self.last_error[:1000],
                    delay,
                )
        return len(records)

    async def _publish(self, record: OutboxRecord) -> str:
        if self.redis is None:
            raise RuntimeError("Outbox Redis 尚未初始化")
        dedup_key = f"{self.stream}:dedup:{record.event_key}"
        return str(
            await self.redis.eval(
                self._PUBLISH_LUA,
                2,
                self.stream,
                dedup_key,
                self.maxlen,
                record.event_key,
                record.aggregate_type,
                record.aggregate_id,
                record.event_type,
                json.dumps(record.payload, ensure_ascii=False, separators=(",", ":")),
                int(os.environ.get("OUTBOX_DEDUP_TTL_SECONDS", "604800")),
            )
        )

    def stats(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "stream": self.stream,
            "published": self.published,
            "failures": self.failures,
            "last_error": self.last_error,
        }


def create_outbox_publisher(database: PostgresDatabase) -> PostgresRedisOutboxPublisher | None:
    enabled = os.environ.get("OUTBOX_PUBLISHER_ENABLED", "false").strip().lower()
    if enabled not in {"1", "true", "yes"}:
        return None
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        raise RuntimeError("OUTBOX_PUBLISHER_ENABLED=true 时必须配置 REDIS_URL")
    return PostgresRedisOutboxPublisher(
        database,
        redis_url,
        stream=os.environ.get("OUTBOX_STREAM", "dramaflow:outbox"),
        batch_size=int(os.environ.get("OUTBOX_BATCH_SIZE", "50")),
        lease_seconds=int(os.environ.get("OUTBOX_LEASE_SECONDS", "30")),
        maxlen=int(os.environ.get("OUTBOX_STREAM_MAXLEN", "20000")),
    )
