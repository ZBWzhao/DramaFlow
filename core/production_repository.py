"""Durable production-run state for the short-drama workflow.

The local adapter keeps the zero-dependency development experience.  PostgreSQL
is the authoritative adapter in ``run_full.py``.  Both expose compare-and-swap
updates so two API instances cannot advance the same production run silently.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .postgres import PostgresDatabase


RUN_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


class ProductionRunConflict(RuntimeError):
    def __init__(self, run_id: str, expected: int, actual: int) -> None:
        self.run_id, self.expected, self.actual = run_id, expected, actual
        super().__init__(
            f"生产任务 {run_id} 已被其他执行器推进（期望 revision={expected}，实际={actual}）"
        )


@runtime_checkable
class ProductionRunRepository(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def create(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def get(self, run_id: str) -> dict[str, Any] | None: ...
    async def latest_for_session(self, session_id: str) -> dict[str, Any] | None: ...
    async def save(self, payload: dict[str, Any], expected_revision: int) -> dict[str, Any]: ...


def _validate_run_id(run_id: str) -> str:
    value = str(run_id or "").strip().lower()
    if not RUN_ID_PATTERN.fullmatch(value):
        raise ValueError("生产任务 ID 无效")
    return value


class FileProductionRunRepository:
    """Atomic JSON snapshots with process-local serialization and revision CAS."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._lock = asyncio.Lock()

    def _path(self, run_id: str) -> Path:
        return self.root / f"{_validate_run_id(run_id)}.json"

    async def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        return None

    async def _read(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            return json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
        except (OSError, ValueError):
            return None

    async def _write(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        await asyncio.to_thread(tmp.write_text, encoded, encoding="utf-8")
        await asyncio.to_thread(os.replace, tmp, path)

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = self._path(str(payload.get("id") or ""))
        async with self._lock:
            if path.exists():
                raise ProductionRunConflict(path.stem, 0, int((await self._read(path) or {}).get("revision") or 0))
            saved = {**payload, "revision": 1, "updated_at": time.time()}
            await self._write(path, saved)
            return saved

    async def get(self, run_id: str) -> dict[str, Any] | None:
        return await self._read(self._path(run_id))

    async def latest_for_session(self, session_id: str) -> dict[str, Any] | None:
        latest: dict[str, Any] | None = None
        for path in self.root.glob("*.json"):
            row = await self._read(path)
            if row and row.get("session_id") == session_id:
                if latest is None or float(row.get("updated_at") or 0) > float(latest.get("updated_at") or 0):
                    latest = row
        return latest

    async def save(self, payload: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        path = self._path(str(payload.get("id") or ""))
        async with self._lock:
            current = await self._read(path)
            actual = int((current or {}).get("revision") or 0)
            if current is None or actual != expected_revision:
                raise ProductionRunConflict(path.stem, expected_revision, actual)
            saved = {**payload, "revision": actual + 1, "updated_at": time.time()}
            await self._write(path, saved)
            return saved


class PostgresProductionRunRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    async def start(self) -> None:
        await self.database.start()

    async def close(self) -> None:
        await self.database.close()

    @staticmethod
    def _decode(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        content = row["content"]
        payload = dict(content) if isinstance(content, dict) else json.loads(content)
        payload["revision"] = int(row["revision"])
        payload["updated_at"] = float(row["updated_at"])
        return payload

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = _validate_run_id(str(payload.get("id") or ""))
        encoded = json.dumps({**payload, "revision": 1}, ensure_ascii=False, default=str)
        row = await self.database.client().fetchrow(
            """INSERT INTO production_runs(run_id,session_id,canvas_name,status,current_stage,content,revision)
               VALUES($1,$2,$3,$4,$5,$6::jsonb,1)
               ON CONFLICT(run_id) DO NOTHING
               RETURNING content,revision,extract(epoch from updated_at) AS updated_at""",
            run_id, payload["session_id"], payload["canvas_name"], payload["status"],
            payload["current_stage"], encoded,
        )
        if row is None:
            existing = await self.get(run_id)
            raise ProductionRunConflict(run_id, 0, int((existing or {}).get("revision") or 0))
        return self._decode(row) or payload

    async def get(self, run_id: str) -> dict[str, Any] | None:
        row = await self.database.client().fetchrow(
            "SELECT content,revision,extract(epoch from updated_at) AS updated_at FROM production_runs WHERE run_id=$1",
            _validate_run_id(run_id),
        )
        return self._decode(row)

    async def latest_for_session(self, session_id: str) -> dict[str, Any] | None:
        row = await self.database.client().fetchrow(
            """SELECT content,revision,extract(epoch from updated_at) AS updated_at
               FROM production_runs WHERE session_id=$1 ORDER BY updated_at DESC LIMIT 1""",
            session_id,
        )
        return self._decode(row)

    async def save(self, payload: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        run_id = _validate_run_id(str(payload.get("id") or ""))
        encoded = json.dumps({**payload, "revision": expected_revision + 1}, ensure_ascii=False, default=str)
        row = await self.database.client().fetchrow(
            """UPDATE production_runs
               SET status=$2,current_stage=$3,content=$4::jsonb,revision=revision+1,updated_at=now()
               WHERE run_id=$1 AND revision=$5
               RETURNING content,revision,extract(epoch from updated_at) AS updated_at""",
            run_id, payload["status"], payload["current_stage"], encoded, expected_revision,
        )
        if row is None:
            actual = await self.database.client().fetchval(
                "SELECT revision FROM production_runs WHERE run_id=$1", run_id
            )
            raise ProductionRunConflict(run_id, expected_revision, int(actual or 0))
        return self._decode(row) or payload


def create_production_run_repository(root: Path) -> ProductionRunRepository:
    backend = os.environ.get("PRODUCTION_RUN_STORE_BACKEND", "file").strip().lower() or "file"
    if backend in {"file", "json"}:
        return FileProductionRunRepository(root)
    if backend in {"postgres", "postgresql"}:
        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError("PRODUCTION_RUN_STORE_BACKEND=postgres 时必须配置 DATABASE_URL")
        return PostgresProductionRunRepository(
            PostgresDatabase(
                url,
                min_size=int(os.environ.get("POSTGRES_POOL_MIN_SIZE", "1")),
                max_size=int(os.environ.get("POSTGRES_POOL_MAX_SIZE", "10")),
            )
        )
    raise RuntimeError(f"未知 PRODUCTION_RUN_STORE_BACKEND：{backend}")
