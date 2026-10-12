"""Shared PostgreSQL connection and idempotent schema bootstrap.

PostgreSQL stores durable business facts. Redis remains responsible for
ephemeral run coordination and event fan-out; media blobs belong in object
storage and only their metadata is represented here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


class PostgresDatabase:
    """Small shared-pool wrapper with reference-counted lifecycle."""

    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self.url = url
        self.min_size = max(1, min_size)
        self.max_size = max(self.min_size, max_size)
        self.pool: Any = None
        self._users = 0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            self._users += 1
            if self.pool is not None:
                return
            try:
                import asyncpg
            except ImportError as exc:  # pragma: no cover - deployment error
                self._users -= 1
                raise RuntimeError(
                    "PostgreSQL 存储已启用，但未安装 asyncpg；请执行 pip install -r requirements.txt"
                ) from exc
            try:
                self.pool = await asyncpg.create_pool(
                    self.url, min_size=self.min_size, max_size=self.max_size,
                    command_timeout=30,
                )
                await self.migrate()
            except Exception:
                self._users -= 1
                if self.pool is not None:
                    await self.pool.close()
                    self.pool = None
                raise

    async def migrate(self) -> None:
        if self.pool is None:
            raise RuntimeError("PostgreSQL 尚未初始化")
        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        async with self.pool.acquire() as connection:
            # Prevent two API/worker processes from bootstrapping concurrently.
            await connection.execute("SELECT pg_advisory_lock(hashtext('dramaflow:migrations'))")
            try:
                await connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations "
                    "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
                for path in files:
                    version = path.stem
                    exists = await connection.fetchval(
                        "SELECT 1 FROM schema_migrations WHERE version=$1", version
                    )
                    if exists:
                        continue
                    async with connection.transaction():
                        await connection.execute(path.read_text(encoding="utf-8"))
                        await connection.execute(
                            "INSERT INTO schema_migrations(version) VALUES($1)", version
                        )
            finally:
                await connection.execute("SELECT pg_advisory_unlock(hashtext('dramaflow:migrations'))")

    def client(self) -> Any:
        if self.pool is None:
            raise RuntimeError("PostgreSQL 存储尚未初始化")
        return self.pool

    async def close(self) -> None:
        async with self._lock:
            self._users = max(0, self._users - 1)
            if self._users == 0 and self.pool is not None:
                await self.pool.close()
                self.pool = None

