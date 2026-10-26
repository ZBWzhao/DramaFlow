"""管线产物仓库：pipeline_artifacts 表的 asyncpg 读写（latest-wins upsert）。

表结构与约定：migrations/0005_pipeline_artifacts.sql + docs/pipeline/data-contract.md
"存储与导入约定"——PRIMARY KEY (project_id, artifact_type)，导入即覆盖；
schema_version 由请求/API 层负责（save 的可选参数，缺省不越权改写）。

asyncpg 访问模式照抄 core/production_repository.py 的 PostgresProductionRunRepository：
PostgresDatabase 包装 + db.client() 取池、$n::jsonb 显式转型、JSON 容错解码。
区别仅在时间戳：本表用 DOUBLE PRECISION（epoch 秒），直接读写数值，无需 extract。
"""

from __future__ import annotations

import json
from typing import Any

from .postgres import PostgresDatabase


def _decode_content(raw: Any) -> Any:
    """JSONB 容错解码：asyncpg 默认把 jsonb 读成 str；已设 codec 时是原生对象。"""
    if isinstance(raw, (dict, list)):
        return raw
    return json.loads(raw)


async def save(
    db: PostgresDatabase,
    project_id: str,
    art_type: str,
    content: Any,
    source_run_id: str = "",
    schema_version: str = "",
    *,
    findings: list | None = None,
) -> dict:
    """upsert 一行（latest-wins）。返回 {"content","schema_version","source_run_id","updated_at"}。

    schema_version 可选（阶段 1 Task 3：API 层是唯一写者）：非空时落库；
    缺省 '' 保持本模块原行为——首次插入落列默认值 ''，覆盖时保留库内原值。

    findings 可选（gate severity tiers Task 3，migrations/0008）：分级门标记随产物
    落库，形状与 StageResult.findings 一致（[{"gate","rule","severity","detail"}]）。
    None（缺省）= 保留行内既有 findings（UPDATE 不触碰该列）——既有调用点（导入、
    集级编辑、改稿 apply）零改动即不误清标记；[] = 显式清空；非空 list 原样覆盖。
    run 阶段通过路径传计算出的 findings：latest-wins 刷新语义（新跑无标记即清空，
    语义正确——标记属于最新产物）。
    """
    encoded = json.dumps(content, ensure_ascii=False, default=str)
    encoded_findings = None if findings is None else json.dumps(
        findings, ensure_ascii=False, default=str)
    row = await db.client().fetchrow(
        """INSERT INTO pipeline_artifacts(project_id, artifact_type, schema_version, content,
                                          source_run_id, findings, created_at, updated_at)
           VALUES($1, $2, $5, $3::jsonb, $4, COALESCE($6::jsonb, '[]'::jsonb),
                  extract(epoch from now()), extract(epoch from now()))
           ON CONFLICT(project_id, artifact_type) DO UPDATE
               SET content = EXCLUDED.content,
                   source_run_id = EXCLUDED.source_run_id,
                   schema_version = CASE WHEN $5 <> '' THEN $5
                                         ELSE pipeline_artifacts.schema_version END,
                   findings = CASE WHEN $6::jsonb IS NULL THEN pipeline_artifacts.findings
                                   ELSE EXCLUDED.findings END,
                   updated_at = EXCLUDED.updated_at
           RETURNING content, schema_version, source_run_id, updated_at""",
        project_id, art_type, encoded, source_run_id, schema_version, encoded_findings,
    )
    return {
        "content": _decode_content(row["content"]),
        "schema_version": str(row["schema_version"]),
        "source_run_id": str(row["source_run_id"]),
        "updated_at": float(row["updated_at"]),
    }


async def get(db: PostgresDatabase, project_id: str, art_type: str) -> dict | None:
    """读单行；无则 None。返回 {"content", "schema_version", "source_run_id", "findings", "updated_at"}。

    source_run_id（Task 8 起带出）：改稿脑 apply 的回滚要按旧 provenance 原样写回，
    调用方（skill_executor / pipeline_run / 导入路由）只用 content，多出的键是加法。
    findings（gate severity tiers Task 3）：0008 起带出分级门标记（缺省 []）。
    """
    row = await db.client().fetchrow(
        "SELECT content, schema_version, source_run_id, findings, updated_at FROM pipeline_artifacts"
        " WHERE project_id=$1 AND artifact_type=$2",
        project_id, art_type,
    )
    if row is None:
        return None
    return {
        "content": _decode_content(row["content"]),
        "schema_version": str(row["schema_version"]),
        "source_run_id": str(row["source_run_id"]),
        "findings": _decode_content(row["findings"]),
        "updated_at": float(row["updated_at"]),
    }


async def list_all(db: PostgresDatabase, project_id: str) -> list[dict]:
    """列出一个项目的全部产物行，按 artifact_type 排序。"""
    rows = await db.client().fetch(
        "SELECT artifact_type, content, schema_version, source_run_id, findings, updated_at"
        " FROM pipeline_artifacts WHERE project_id=$1 ORDER BY artifact_type",
        project_id,
    )
    return [
        {
            "artifact_type": str(row["artifact_type"]),
            "content": _decode_content(row["content"]),
            "schema_version": str(row["schema_version"]),
            "source_run_id": str(row["source_run_id"]),
            "findings": _decode_content(row["findings"]),
            "updated_at": float(row["updated_at"]),
        }
        for row in rows
    ]


async def delete(db: PostgresDatabase, project_id: str, art_type: str) -> bool:
    """删除单行；删到返回 True，本来就没有返回 False。"""
    deleted = await db.client().fetchval(
        "DELETE FROM pipeline_artifacts WHERE project_id=$1 AND artifact_type=$2 RETURNING 1",
        project_id, art_type,
    )
    return deleted is not None
