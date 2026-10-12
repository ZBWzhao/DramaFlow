"""Against a real PostgreSQL instance, verify durable-store invariants.

The test uses random IDs and removes every row it creates. Run it with:

    $env:DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:5432/dramaflow"
    python demo/postgres_live_invariants.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.canvas_repository import CanvasConflictError, PostgresCanvasRepository
from core.invocation_store import (
    ApprovalState,
    InvocationState,
    PostgresApprovalStore,
    PostgresToolInvocationStore,
    canonical_hash,
    canonical_json,
)
from core.media_job_store import MediaJob, PostgresMediaJobStore
from core.postgres import PostgresDatabase


async def main() -> None:
    url = os.environ.get(
        "DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/dramaflow"
    )
    token = uuid.uuid4().hex[:12]
    project_id = f"it_project_{token}"
    canvas_name = f"it-canvas-{token}"
    invocation_id = f"it_inv_{token}"
    invocation_id_2 = f"it_inv_alt_{token}"
    invocation_key = f"it:key:{token}"
    approval_id = f"it_approval_{token}"
    job_id = f"it_job_{token}"
    job_id_2 = f"it_job_alt_{token}"
    job_key = f"it:job:{token}"

    database = PostgresDatabase(url, min_size=1, max_size=6)
    invocations = PostgresToolInvocationStore(database)
    approvals = PostgresApprovalStore(database)
    jobs = PostgresMediaJobStore(database)

    with tempfile.TemporaryDirectory() as directory:
        canvases = PostgresCanvasRepository(database, Path(directory), project_id=project_id)
        stores = (invocations, approvals, jobs, canvases)
        try:
            for store in stores:
                await store.start()

            # A unique idempotency key admits exactly one invocation row even concurrently.
            args = {"path": "assets/test.txt", "content": "hello"}
            args_hash = canonical_hash(args)
            first_state = InvocationState(
                invocation_id=invocation_id,
                idempotency_key=invocation_key,
                session_id=f"it_session_{token}",
                run_id=f"it_run_{token}",
                tool_name="write_file",
                args_hash=args_hash,
                args_json=canonical_json(args),
            )
            second_state = InvocationState(
                invocation_id=invocation_id_2,
                idempotency_key=invocation_key,
                session_id=f"it_session_{token}",
                run_id=f"it_run_{token}",
                tool_name="write_file",
                args_hash=args_hash,
                args_json=canonical_json(args),
            )
            prepared = await asyncio.gather(
                invocations.prepare(first_state), invocations.prepare(second_state)
            )
            assert sum(1 for _, created in prepared if created) == 1
            persisted_id = prepared[0][0].invocation_id
            assert prepared[1][0].invocation_id == persisted_id
            await invocations.mark_executing(persisted_id)
            await invocations.set_remote_operation(persisted_id, f"provider_{token}")
            await invocations.complete(persisted_id, {"ok": True, "path": "assets/test.txt"})
            invocation = await invocations.get(persisted_id)
            assert invocation and invocation.status == "succeeded" and invocation.attempt == 1
            assert invocation.remote_operation_id == f"provider_{token}"
            print("PASS: PostgreSQL 工具调用幂等、防并发重复与 operation_id 持久化")

            # Matching retries are idempotent; changed parameters cannot reuse approval.
            approval = ApprovalState(
                approval_id=approval_id,
                scope="canvas.run",
                args_hash=args_hash,
                payload_json=canonical_json(args),
                expires_at=time.time() + 60,
            )
            await approvals.create(approval)
            consumed = await asyncio.gather(
                approvals.consume(approval_id, "canvas.run", args_hash),
                approvals.consume(approval_id, "canvas.run", args_hash),
            )
            assert all(item.status == "consumed" for item in consumed)
            try:
                await approvals.consume(approval_id, "canvas.run", canonical_hash({"changed": True}))
            except (RuntimeError, ValueError):
                pass
            else:
                raise AssertionError("修改后的参数不应复用已审批请求")
            print("PASS: PostgreSQL 审批行锁、同参重放与参数篡改拦截")

            # Concurrent workers can claim a queued media job only once.
            payload = {"jobs": [{"node_id": "n1", "ready": True}]}
            first_job = MediaJob(
                job_id=job_id,
                idempotency_key=job_key,
                approval_id=approval_id,
                canvas=canvas_name,
                payload_json=json.dumps(payload),
            )
            second_job = MediaJob(
                job_id=job_id_2,
                idempotency_key=job_key,
                approval_id=approval_id,
                canvas=canvas_name,
                payload_json=json.dumps(payload),
            )
            enqueued = await asyncio.gather(jobs.enqueue(first_job), jobs.enqueue(second_job))
            assert sum(1 for _, created in enqueued if created) == 1
            persisted_job_id = enqueued[0][0].job_id
            claims = await asyncio.gather(jobs.claim("it-worker-a", 30), jobs.claim("it-worker-b", 30))
            claimed = [item for item in claims if item is not None]
            assert len(claimed) == 1 and claimed[0].job_id == persisted_job_id
            owner = claimed[0].worker_id
            assert not await jobs.heartbeat(persisted_job_id, "wrong-worker", 30)
            assert await jobs.heartbeat(persisted_job_id, owner, 30)
            try:
                await jobs.complete(persisted_job_id, "wrong-worker", {"ok": True})
            except RuntimeError:
                pass
            else:
                raise AssertionError("非租约所有者不应提交任务结果")
            await jobs.complete(persisted_job_id, owner, {"ok": True, "asset_id": token})
            completed = await jobs.get(persisted_job_id)
            assert completed and completed.status == "succeeded" and completed.attempt == 1
            print("PASS: PostgreSQL 媒体任务幂等、SKIP LOCKED 抢占、租约所有权与完成态")

            # Two stale canvas snapshots cannot overwrite each other.
            canvas = await canvases.load(canvas_name)
            canvas.ensure_story("集成测试短剧")
            canvas.add_node("text", "初始剧本")
            assert await canvases.save(canvas) == 1
            writer_a = await canvases.load(canvas_name)
            writer_b = await canvases.load(canvas_name)
            writer_a.add_node("storyboard", "分镜 A")
            assert await canvases.save(writer_a) == 2
            writer_b.add_node("storyboard", "分镜 B")
            try:
                await canvases.save(writer_b)
            except CanvasConflictError as exc:
                assert exc.expected == 1 and exc.actual == 2
            else:
                raise AssertionError("旧 revision 不应覆盖新画布")
            latest = await canvases.load(canvas_name)
            assert latest.revision == 2
            assert {node.label for node in latest.nodes.values()} == {"初始剧本", "分镜 A"}
            print("PASS: PostgreSQL 画布 revision 乐观锁阻止旧快照覆盖")
        finally:
            if database.pool is not None:
                pool = database.client()
                await pool.execute("DELETE FROM media_jobs WHERE idempotency_key=$1", job_key)
                await pool.execute("DELETE FROM approval_requests WHERE approval_id=$1", approval_id)
                await pool.execute("DELETE FROM tool_invocations WHERE idempotency_key=$1", invocation_key)
                await pool.execute(
                    "DELETE FROM canvases WHERE project_id=$1 AND name=$2", project_id, canvas_name
                )
                await pool.execute("DELETE FROM projects WHERE project_id=$1", project_id)
            for store in reversed(stores):
                await store.close()


if __name__ == "__main__":
    asyncio.run(main())
