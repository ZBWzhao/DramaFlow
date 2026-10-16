"""Canvas persistence adapters with optimistic concurrency control."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from .canvas import Canvas
from .postgres import PostgresDatabase


CANVAS_NAME = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{1,120}$")


class CanvasConflictError(RuntimeError):
    def __init__(self, name: str, expected: int, actual: int) -> None:
        self.name, self.expected, self.actual = name, expected, actual
        super().__init__(
            f"画布 {name} 已被其他用户或 Agent 更新（期望 revision={expected}，实际={actual}），"
            "本次修改未覆盖新版本，请刷新后重试"
        )


def validate_canvas_name(name: str) -> str:
    value = str(name or "").strip()
    if not CANVAS_NAME.fullmatch(value):
        raise ValueError("画布名称无效：只允许中英文、数字、下划线和连字符，长度 1-120")
    return value


@runtime_checkable
class CanvasRepository(Protocol):
    async def start(self) -> None: ...
    async def load(self, name: str) -> Canvas: ...
    async def save(self, canvas: Canvas) -> int: ...
    async def list(self) -> list[str]: ...
    async def delete(self, name: str) -> None: ...
    async def close(self) -> None: ...


class FileCanvasRepository:
    """Development adapter with a sidecar process lock and revision CAS."""

    def __init__(self, root: Path, *, lock_timeout: float = 10.0, stale_lock_seconds: float = 60.0) -> None:
        self.root = root.resolve()
        self.lock_timeout = lock_timeout
        self.stale_lock_seconds = stale_lock_seconds

    def _path(self, name: str) -> Path:
        return self.root / f"{validate_canvas_name(name)}.canvas.json"

    async def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    async def load(self, name: str) -> Canvas:
        path = self._path(name)
        return await asyncio.to_thread(Canvas.load, path, name)

    @asynccontextmanager
    async def _guard(self, path: Path) -> AsyncIterator[None]:
        lock_path = path.with_suffix(path.suffix + ".lock")
        deadline = time.monotonic() + self.lock_timeout
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(descriptor, f"{os.getpid()} {time.time()}".encode("ascii"))
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > self.stale_lock_seconds:
                        lock_path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"等待画布写锁超时：{path.name}")
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            os.close(descriptor)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    async def save(self, canvas: Canvas) -> int:
        path = self._path(canvas.name)
        async with self._guard(path):
            actual = -1
            if path.exists():
                raw = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
                actual = int(raw.get("revision") or 0)
            expected = canvas.revision
            if (canvas.persisted and actual != expected) or (not canvas.persisted and actual >= 0):
                raise CanvasConflictError(canvas.name, expected, actual)
            next_revision = expected + 1
            payload = canvas.to_dict()
            payload["revision"] = next_revision
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
            await asyncio.to_thread(
                tmp.write_text, json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            await asyncio.to_thread(os.replace, tmp, path)
            canvas.path, canvas.revision, canvas.persisted = path, next_revision, True
            return next_revision

    async def list(self) -> list[str]:
        return sorted(path.name[: -len(".canvas.json")] for path in self.root.glob("*.canvas.json"))

    async def delete(self, name: str) -> None:
        path = self._path(name)
        async with self._guard(path):
            if not path.exists():
                raise FileNotFoundError(name)
            await asyncio.to_thread(path.unlink)
            lock_path = path.with_suffix(path.suffix + ".lock")
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    async def close(self) -> None:
        return None


class PostgresCanvasRepository:
    """Production adapter using UPDATE ... WHERE revision = expected."""

    def __init__(self, database: PostgresDatabase, root: Path, project_id: str = "default") -> None:
        self.database = database
        self.root = root.resolve()
        self.project_id = project_id

    async def start(self) -> None:
        await self.database.start()
        await self.database.client().execute(
            """INSERT INTO projects(project_id,name) VALUES($1,$2)
               ON CONFLICT(project_id) DO NOTHING""",
            self.project_id, "DramaFlow",
        )
        # Preserve existing projects when switching from the file adapter.
        for path in sorted(self.root.glob("*.canvas.json")):
            await self.load(path.name[: -len(".canvas.json")])

    def _canvas_id(self, name: str) -> str:
        digest = hashlib.sha256(f"{self.project_id}:{name}".encode()).hexdigest()[:24]
        return f"canvas_{digest}"

    async def load(self, name: str) -> Canvas:
        name = validate_canvas_name(name)
        row = await self.database.client().fetchrow(
            "SELECT content,revision FROM canvases WHERE project_id=$1 AND name=$2",
            self.project_id, name,
        )
        if row is not None:
            raw = row["content"] if isinstance(row["content"], dict) else json.loads(row["content"])
            canvas = Canvas.from_dict(raw, name=name, path=self.root / f"{name}.canvas.json")
            canvas.revision, canvas.persisted = int(row["revision"]), True
            return canvas

        # One-way bootstrap: first PostgreSQL load imports an existing legacy file.
        legacy = self.root / f"{name}.canvas.json"
        if legacy.exists():
            canvas = await asyncio.to_thread(Canvas.load, legacy, name)
            canvas.persisted = False
            canvas.revision = 0
            try:
                await self.save(canvas)
                return canvas
            except CanvasConflictError:
                return await self.load(name)
        return Canvas(name, legacy)

    async def save(self, canvas: Canvas) -> int:
        name = validate_canvas_name(canvas.name)
        pool = self.database.client()
        expected = int(canvas.revision)
        payload = canvas.to_dict()
        payload["revision"] = expected + 1
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        if not canvas.persisted:
            inserted = await pool.fetchval(
                """INSERT INTO canvases(canvas_id,project_id,name,content,revision)
                   VALUES($1,$2,$3,$4::jsonb,1)
                   ON CONFLICT(project_id,name) DO NOTHING RETURNING revision""",
                self._canvas_id(name), self.project_id, name, encoded,
            )
            if inserted is None:
                actual = await pool.fetchval(
                    "SELECT revision FROM canvases WHERE project_id=$1 AND name=$2", self.project_id, name
                )
                raise CanvasConflictError(name, expected, int(actual or 0))
            canvas.revision, canvas.persisted = 1, True
            return 1

        next_revision = await pool.fetchval(
            """UPDATE canvases SET content=$4::jsonb,revision=revision+1,updated_at=now()
               WHERE project_id=$1 AND name=$2 AND revision=$3 RETURNING revision""",
            self.project_id, name, expected, encoded,
        )
        if next_revision is None:
            actual = await pool.fetchval(
                "SELECT revision FROM canvases WHERE project_id=$1 AND name=$2", self.project_id, name
            )
            raise CanvasConflictError(name, expected, int(actual if actual is not None else -1))
        canvas.revision = int(next_revision)
        return canvas.revision

    async def list(self) -> list[str]:
        rows = await self.database.client().fetch(
            "SELECT name FROM canvases WHERE project_id=$1 ORDER BY name", self.project_id
        )
        return [str(row["name"]) for row in rows]

    async def delete(self, name: str) -> None:
        name = validate_canvas_name(name)
        deleted = await self.database.client().execute(
            "DELETE FROM canvases WHERE project_id=$1 AND name=$2", self.project_id, name
        )
        if deleted == "DELETE 0":
            raise FileNotFoundError(name)

    async def close(self) -> None:
        await self.database.close()


def create_canvas_repository(root: Path) -> CanvasRepository:
    backend = os.environ.get("CANVAS_STORE_BACKEND", "file").strip().lower() or "file"
    if backend in {"file", "json"}:
        return FileCanvasRepository(root)
    if backend in {"postgres", "postgresql"}:
        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError("CANVAS_STORE_BACKEND=postgres 时必须配置 DATABASE_URL")
        return PostgresCanvasRepository(
            PostgresDatabase(
                url,
                min_size=int(os.environ.get("POSTGRES_POOL_MIN_SIZE", "1")),
                max_size=int(os.environ.get("POSTGRES_POOL_MAX_SIZE", "10")),
            ),
            root,
            project_id=os.environ.get("DRAMAFLOW_PROJECT_ID", "default").strip() or "default",
        )
    raise RuntimeError(f"未知 CANVAS_STORE_BACKEND：{backend}")
