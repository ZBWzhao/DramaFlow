"""后台媒体任务队列、租约、心跳和幂等入队的无网络回归。"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.media_job_store import InMemoryMediaJobStore, MediaJob, MediaJobWorker  # noqa: E402


async def wait_terminal(store: InMemoryMediaJobStore, job_id: str, timeout: float = 3) -> MediaJob:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = await store.get(job_id)
        if state and state.status not in {"queued", "running"}:
            return state
        await asyncio.sleep(0.02)
    raise AssertionError("媒体任务没有在期限内结束")


async def main() -> None:
    store = InMemoryMediaJobStore()
    await store.start()
    calls = 0

    async def execute(job: MediaJob) -> dict:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"ok": True, "done": 1, "total": 1, "job": job.job_id}

    job = MediaJob(
        job_id="media_" + "1" * 32,
        idempotency_key="same-request",
        approval_id="apr_test",
        canvas="main",
        payload_json=json.dumps({"jobs": []}),
    )
    first, created = await store.enqueue(job)
    duplicate, created_again = await store.enqueue(MediaJob(**{**job.__dict__, "job_id": "media_" + "2" * 32}))
    assert created and not created_again and duplicate.job_id == first.job_id

    worker = MediaJobWorker(store, execute, lease_seconds=10, poll_seconds=0.01)
    await worker.start()
    finished = await wait_terminal(store, job.job_id)
    assert finished.status == "succeeded" and calls == 1
    assert json.loads(finished.result_json or "{}")["done"] == 1
    await worker.close()

    stale = MediaJob(
        job_id="media_" + "3" * 32,
        idempotency_key="stale-request",
        approval_id="apr_stale",
        canvas="main",
        payload_json="{}",
    )
    await store.enqueue(stale)
    claimed = await store.claim("dead-worker", lease_seconds=1)
    assert claimed and claimed.status == "running"
    await asyncio.sleep(1.05)
    assert await store.claim("replacement", lease_seconds=1) is None
    stale_state = await store.get(stale.job_id)
    assert stale_state and stale_state.status == "unknown"
    assert "禁止自动重试" in (stale_state.error or "")
    requeued = await store.requeue_unknown(stale.job_id)
    assert requeued.status == "queued"
    replacement = MediaJobWorker(store, execute, lease_seconds=10, poll_seconds=0.01)
    await replacement.start()
    recovered = await wait_terminal(store, stale.job_id)
    assert recovered.status == "succeeded" and recovered.attempt == 2
    await replacement.close()

    await store.close()
    print("PASS: 媒体任务幂等入队并由 Worker 异步完成")
    print("PASS: Worker 租约过期进入 unknown，不会盲目重复生成")
    print("PASS: unknown 任务只能经显式人工动作重新入队")


if __name__ == "__main__":
    asyncio.run(main())
