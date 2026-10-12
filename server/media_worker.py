"""独立媒体 Worker 进程入口。

生产/多实例模式：API 设置 MEDIA_WORKER_EMBEDDED=false，再单独运行
`python -m server.media_worker`。两端必须配置同一个 Redis 或 PostgreSQL 后端。
"""

from __future__ import annotations

import asyncio
import os

from core.media_job_store import MediaJobWorker, PostgresMediaJobStore, RedisMediaJobStore
from core.invocation_store import PostgresToolInvocationStore, RedisToolInvocationStore
from server.app import _execute_media_job, media_jobs, registry


async def main() -> None:
    if not isinstance(media_jobs, (RedisMediaJobStore, PostgresMediaJobStore)):
        raise RuntimeError(
            "独立 Worker 必须配置共享任务存储：MEDIA_JOB_STORE_BACKEND=redis/postgres"
        )
    if not isinstance(registry.invocations, (RedisToolInvocationStore, PostgresToolInvocationStore)):
        raise RuntimeError(
            "独立 Worker 还必须使用共享调用账本：INVOCATION_STORE_BACKEND=redis/postgres"
        )
    await registry.ainit()
    await media_jobs.start()
    worker = MediaJobWorker(
        media_jobs,
        _execute_media_job,
        lease_seconds=int(os.environ.get("MEDIA_JOB_LEASE_SECONDS", "30")),
    )
    await worker.start()
    print(f"Media Worker started: {worker.worker_id}")
    try:
        await asyncio.Event().wait()
    finally:
        await worker.close()
        await media_jobs.close()
        await registry.aclose()


if __name__ == "__main__":
    asyncio.run(main())
