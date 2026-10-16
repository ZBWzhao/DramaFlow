"""Metadata repository for generated, uploaded and composed media assets."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .postgres import PostgresDatabase


@dataclass(frozen=True)
class MediaAssetRecord:
    asset_id: str
    canvas: str
    node_id: str
    media_type: str
    storage_key: str
    content_type: str = ""
    file_size: int = 0
    sha256: str = ""
    provider: str = ""
    remote_operation_id: str = ""
    generation_id: str = ""

    @classmethod
    def create(cls, *, canvas: str, node_id: str, media_type: str, storage_key: str, **values: object) -> "MediaAssetRecord":
        generation_id = str(values.get("generation_id") or "")
        seed = f"{canvas}\n{node_id}\n{generation_id}\n{storage_key}".encode("utf-8")
        return cls(
            asset_id=f"asset_{hashlib.sha256(seed).hexdigest()[:32]}",
            canvas=canvas,
            node_id=node_id,
            media_type=media_type,
            storage_key=storage_key,
            content_type=str(values.get("content_type") or ""),
            file_size=int(values.get("file_size") or 0),
            sha256=str(values.get("sha256") or ""),
            provider=str(values.get("provider") or ""),
            remote_operation_id=str(values.get("remote_operation_id") or ""),
            generation_id=generation_id,
        )


@runtime_checkable
class MediaAssetRepository(Protocol):
    async def start(self) -> None: ...
    async def register(self, asset: MediaAssetRecord) -> None: ...
    async def close(self) -> None: ...


class InMemoryMediaAssetRepository:
    def __init__(self) -> None:
        self.items: dict[str, MediaAssetRecord] = {}

    async def start(self) -> None:
        return None

    async def register(self, asset: MediaAssetRecord) -> None:
        self.items[asset.asset_id] = asset

    async def close(self) -> None:
        return None


class PostgresMediaAssetRepository:
    def __init__(self, database: PostgresDatabase, project_id: str = "default") -> None:
        self.database = database
        self.project_id = project_id

    async def start(self) -> None:
        await self.database.start()
        await self.database.client().execute(
            "INSERT INTO projects(project_id,name) VALUES($1,$2) ON CONFLICT(project_id) DO NOTHING",
            self.project_id,
            "DramaFlow",
        )

    def _canvas_id(self, name: str) -> str:
        digest = hashlib.sha256(f"{self.project_id}:{name}".encode()).hexdigest()[:24]
        return f"canvas_{digest}"

    async def register(self, asset: MediaAssetRecord) -> None:
        canvas_id = await self.database.client().fetchval(
            "SELECT canvas_id FROM canvases WHERE project_id=$1 AND name=$2",
            self.project_id,
            asset.canvas,
        )
        await self.database.client().execute(
            """INSERT INTO media_assets(
                   asset_id,project_id,canvas_id,node_id,media_type,storage_key,content_type,
                   file_size,sha256,provider,remote_operation_id,generation_id
               ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
               ON CONFLICT(asset_id) DO UPDATE SET
                   storage_key=EXCLUDED.storage_key,content_type=EXCLUDED.content_type,
                   file_size=EXCLUDED.file_size,sha256=EXCLUDED.sha256,
                   provider=EXCLUDED.provider,remote_operation_id=EXCLUDED.remote_operation_id,
                   generation_id=EXCLUDED.generation_id""",
            asset.asset_id, self.project_id, canvas_id, asset.node_id,
            asset.media_type, asset.storage_key, asset.content_type or None, asset.file_size or None,
            asset.sha256 or None, asset.provider or None, asset.remote_operation_id or None,
            asset.generation_id or None,
        )

    async def close(self) -> None:
        await self.database.close()


def create_media_asset_repository() -> MediaAssetRepository:
    backend = os.environ.get("MEDIA_ASSET_STORE_BACKEND", "memory").strip().lower() or "memory"
    if backend == "memory":
        return InMemoryMediaAssetRepository()
    if backend in {"postgres", "postgresql"}:
        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError("MEDIA_ASSET_STORE_BACKEND=postgres 时必须配置 DATABASE_URL")
        return PostgresMediaAssetRepository(
            PostgresDatabase(
                url,
                min_size=int(os.environ.get("POSTGRES_POOL_MIN_SIZE", "1")),
                max_size=int(os.environ.get("POSTGRES_POOL_MAX_SIZE", "10")),
            ),
            os.environ.get("DRAMAFLOW_PROJECT_ID", "default").strip() or "default",
        )
    raise RuntimeError(f"未知 MEDIA_ASSET_STORE_BACKEND：{backend}")
