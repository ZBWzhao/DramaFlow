"""Session persistence adapters.

The domain ``Session`` remains synchronous because Agent/tool callbacks append to it
inside the hot execution loop.  Repositories own storage and lifecycle:

* JSONL is the zero-dependency local event log.
* PostgreSQL projects the same immutable entries into ``session_entries`` and writes
  a transactional Outbox row with every new entry.

During the migration period PostgreSQL mode deliberately keeps the JSONL log as a
local write-ahead fallback.  Startup imports missing legacy records idempotently,
so enabling the backend never makes existing conversations disappear.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

from .postgres import PostgresDatabase
from .session import SESSIONS_DIR, Session


class SessionRepository(Protocol):
    async def start(self) -> None: ...
    async def flush(self) -> None: ...
    async def close(self) -> None: ...
    def get_or_create(self, session_id: str, path: Path) -> Session: ...
    def get(self, session_id: str) -> Session | None: ...
    def all(self) -> list[Session]: ...
    def delete(self, session_id: str) -> list[str]: ...


class JsonlSessionRepository:
    """Current local adapter, expressed behind the same repository contract."""

    def __init__(self, root: Path = SESSIONS_DIR) -> None:
        self.root = Path(root)
        self._cache: dict[str, Session] = {}

    async def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    async def flush(self) -> None:
        return None

    async def close(self) -> None:
        self._cache.clear()

    def get_or_create(self, session_id: str, path: Path) -> Session:
        existing = self._cache.get(session_id)
        if existing is not None:
            return existing
        session = Session.load(path) if path.exists() else Session(session_id=session_id, path=path)
        self._cache[session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        existing = self._cache.get(session_id)
        if existing is not None:
            return existing
        path = self.root / f"{session_id}.jsonl"
        if not path.exists():
            return None
        session = Session.load(path)
        self._cache[session_id] = session
        return session

    def all(self) -> list[Session]:
        self.root.mkdir(parents=True, exist_ok=True)
        for path in self.root.glob("*.jsonl"):
            self.get(path.stem)
        return list(self._cache.values())

    def delete(self, session_id: str) -> list[str]:
        cached = self._cache.get(session_id)
        path = cached.path if cached is not None and cached.path is not None else self.root / f"{session_id}.jsonl"
        deleted: list[str] = []
        for target in [path, *path.parent.glob(f"{session_id}.*.jsonl")]:
            resolved = target.resolve()
            if resolved.parent != path.parent.resolve() or not resolved.is_file():
                continue
            resolved.unlink()
            deleted.append(resolved.name)
            self._cache.pop(resolved.stem, None)
        self._cache.pop(session_id, None)
        return deleted


class PostgresSessionRepository(JsonlSessionRepository):
    """Durable session projection with transactional Outbox writes.

    Appends enter an in-process ordered queue after the synchronous JSONL write.
    API/team terminal paths call :meth:`flush`, and shutdown drains the queue.  A
    missing database projection can always be rebuilt from the JSONL event log.
    """

    def __init__(self, database: PostgresDatabase, root: Path = SESSIONS_DIR) -> None:
        super().__init__(root)
        self.database = database
        self._queue: asyncio.Queue[tuple[str, str, dict[str, Any] | None]] = asyncio.Queue()
        self._writer: asyncio.Task[None] | None = None
        self._last_error: Exception | None = None
        self._updated_at: dict[str, float] = {}

    async def start(self) -> None:
        await self.database.start()
        self.root.mkdir(parents=True, exist_ok=True)
        await self._import_jsonl()
        await self._load_cache()
        self._writer = asyncio.create_task(self._writer_loop(), name="session-postgres-writer")

    @staticmethod
    def _legacy_record_id(session_id: str, index: int, entry: dict[str, Any]) -> str:
        canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(f"{session_id}:{index}:{canonical}".encode("utf-8")).hexdigest()

    async def _import_jsonl(self) -> None:
        """Idempotently seed PostgreSQL before it becomes the read source."""
        pool = self.database.client()
        for path in sorted(self.root.glob("*.jsonl")):
            try:
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            except (OSError, ValueError):
                continue
            session_id = path.stem
            async with pool.acquire() as connection:
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO sessions(session_id) VALUES($1) ON CONFLICT(session_id) DO NOTHING",
                        session_id,
                    )
                    for index, raw in enumerate(rows):
                        entry = dict(raw)
                        record_id = str(entry.get("record_id") or self._legacy_record_id(session_id, index, entry))
                        entry["record_id"] = record_id
                        await connection.execute(
                            """
                            INSERT INTO session_entries(entry_id, session_id, parent_id, entry_type, role, content, created_at)
                            VALUES($1,$2,$3,$4,$5,$6::jsonb,to_timestamp($7))
                            ON CONFLICT(entry_id) DO NOTHING
                            """,
                            record_id,
                            session_id,
                            entry.get("parent"),
                            str(entry.get("type") or "unknown"),
                            entry.get("role"),
                            json.dumps(entry, ensure_ascii=False),
                            float(entry.get("ts") or time.time()),
                        )

    async def _load_cache(self) -> None:
        pool = self.database.client()
        rows = await pool.fetch(
            """
            SELECT s.session_id, extract(epoch from s.updated_at) AS updated_at,
                   e.content
            FROM sessions s
            LEFT JOIN session_entries e ON e.session_id=s.session_id
            ORDER BY s.session_id, e.sequence
            """
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            session_id = str(row["session_id"])
            self._updated_at[session_id] = float(row["updated_at"] or time.time())
            grouped.setdefault(session_id, [])
            content = row["content"]
            if content is not None:
                grouped[session_id].append(json.loads(content) if isinstance(content, str) else dict(content))
        for session_id, entries in grouped.items():
            path = self.root / f"{session_id}.jsonl"
            self._cache[session_id] = Session.from_entries(
                session_id,
                entries,
                path=path,
                entry_sink=self._enqueue_entry,
            )

    def _enqueue_entry(self, session_id: str, entry: dict[str, Any]) -> None:
        self._updated_at[session_id] = float(entry.get("ts") or time.time())
        self._queue.put_nowait(("append", session_id, dict(entry)))

    def get_or_create(self, session_id: str, path: Path) -> Session:
        existing = self._cache.get(session_id)
        if existing is not None:
            return existing
        session = Session(
            session_id=session_id,
            path=path,
            entry_sink=self._enqueue_entry,
            write_start=False,
        )
        session._repository_entries = []
        self._cache[session_id] = session
        session._write({"type": "session_start", "id": session_id, "cwd": str(path.parent)})
        return session

    def get(self, session_id: str) -> Session | None:
        return self._cache.get(session_id)

    def all(self) -> list[Session]:
        return list(self._cache.values())

    def delete(self, session_id: str) -> list[str]:
        deleted = super().delete(session_id)
        self._queue.put_nowait(("delete", session_id, None))
        self._updated_at.pop(session_id, None)
        return deleted or [f"{session_id} (PostgreSQL)"]

    async def _writer_loop(self) -> None:
        while True:
            operation, session_id, entry = await self._queue.get()
            try:
                if operation == "stop":
                    return
                if operation == "delete":
                    await self.database.client().execute(
                        "DELETE FROM sessions WHERE session_id=$1", session_id
                    )
                elif entry is not None:
                    await self._persist_entry(session_id, entry)
                self._last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # JSONL remains the durable fallback for replay.
                self._last_error = exc
            finally:
                self._queue.task_done()

    async def _persist_entry(self, session_id: str, entry: dict[str, Any]) -> None:
        record_id = str(entry.get("record_id") or uuid.uuid4().hex)
        entry["record_id"] = record_id
        timestamp = float(entry.get("ts") or time.time())
        payload = json.dumps(entry, ensure_ascii=False)
        title = ""
        if entry.get("type") == "custom" and entry.get("custom_type") == "sessionMeta":
            title = str((entry.get("data") or {}).get("title") or "")[:80]
        pool = self.database.client()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO sessions(session_id, title, updated_at)
                    VALUES($1,$2,to_timestamp($3))
                    ON CONFLICT(session_id) DO UPDATE SET
                      title=CASE WHEN EXCLUDED.title='' THEN sessions.title ELSE EXCLUDED.title END,
                      updated_at=EXCLUDED.updated_at
                    """,
                    session_id,
                    title,
                    timestamp,
                )
                await connection.execute(
                    """
                    INSERT INTO session_entries(entry_id, session_id, parent_id, entry_type, role, content, created_at)
                    VALUES($1,$2,$3,$4,$5,$6::jsonb,to_timestamp($7))
                    ON CONFLICT(entry_id) DO NOTHING
                    """,
                    record_id,
                    session_id,
                    entry.get("parent"),
                    str(entry.get("type") or "unknown"),
                    entry.get("role"),
                    payload,
                    timestamp,
                )
                await connection.execute(
                    """
                    INSERT INTO outbox_events(event_key, aggregate_type, aggregate_id, event_type, payload)
                    VALUES($4,'session',$1,$2,$3::jsonb)
                    ON CONFLICT(event_key) DO NOTHING
                    """,
                    session_id,
                    f"session.{entry.get('type') or 'unknown'}",
                    payload,
                    record_id,
                )

    async def flush(self) -> None:
        await self._queue.join()
        if self._last_error is not None:
            raise RuntimeError(f"PostgreSQL 会话投影写入失败：{self._last_error}") from self._last_error

    async def close(self) -> None:
        await self.flush()
        if self._writer is not None:
            self._queue.put_nowait(("stop", "", None))
            await self._writer
            self._writer = None
        self._cache.clear()
        await self.database.close()


def create_session_repository(root: Path = SESSIONS_DIR) -> SessionRepository:
    backend = os.environ.get("SESSION_STORE_BACKEND", "jsonl").strip().lower() or "jsonl"
    if backend in {"jsonl", "file"}:
        return JsonlSessionRepository(root)
    if backend in {"postgres", "postgresql"}:
        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError("SESSION_STORE_BACKEND=postgres 时必须配置 DATABASE_URL")
        database = PostgresDatabase(
            url,
            min_size=int(os.environ.get("POSTGRES_POOL_MIN_SIZE", "1")),
            max_size=int(os.environ.get("POSTGRES_POOL_MAX_SIZE", "10")),
        )
        return PostgresSessionRepository(database, root)
    raise RuntimeError(f"未知 SESSION_STORE_BACKEND：{backend}")

