"""Live PostgreSQL contract for session projection + transactional Outbox."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.postgres import PostgresDatabase  # noqa: E402
from core.outbox import PostgresRedisOutboxPublisher  # noqa: E402
from core.session_repository import PostgresSessionRepository  # noqa: E402


async def main() -> None:
    url = os.environ.get(
        "DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/dramaflow"
    )
    session_id = f"repo-{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        repository = PostgresSessionRepository(PostgresDatabase(url), root)
        await repository.start()
        session = repository.get_or_create(session_id, root / f"{session_id}.jsonl")
        first = session.append({"role": "user", "content": "请记住角色叫阿澈"})
        session.append({"role": "assistant", "content": "已记录"})
        session.append_custom("sessionMeta", {"canvas": "main", "title": "仓储契约"})
        await repository.flush()

        pool = repository.database.client()
        entry_count = await pool.fetchval(
            "SELECT count(*) FROM session_entries WHERE session_id=$1", session_id
        )
        outbox_count = await pool.fetchval(
            "SELECT count(*) FROM outbox_events WHERE aggregate_type='session' AND aggregate_id=$1",
            session_id,
        )
        assert entry_count == 4, entry_count
        assert outbox_count == 4, outbox_count
        await repository.close()

        restored_repository = PostgresSessionRepository(PostgresDatabase(url), root)
        await restored_repository.start()
        stream = f"dramaflow:test:outbox:{session_id}"
        publisher = PostgresRedisOutboxPublisher(
            restored_repository.database,
            os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
            stream=stream,
            batch_size=100,
        )
        await publisher.start()
        restored = restored_repository.get(session_id)
        assert restored is not None
        assert [item.get("content") for item in restored.messages] == ["请记住角色叫阿澈", "已记录"]
        assert restored.message_ids[0] == first
        assert restored.customs_of("sessionMeta")[-1]["title"] == "仓储契约"
        for _ in range(50):
            pending = await restored_repository.database.client().fetchval(
                "SELECT count(*) FROM outbox_events WHERE aggregate_id=$1 AND published_at IS NULL",
                session_id,
            )
            if not pending:
                break
            await asyncio.sleep(0.1)
        assert pending == 0, pending
        dedup = await publisher.redis.get(f"{stream}:dedup:{restored.entries()[0]['record_id']}")
        assert dedup
        await publisher.close()
        restored_repository.delete(session_id)
        await restored_repository.flush()
        await restored_repository.database.client().execute(
            "DELETE FROM outbox_events WHERE aggregate_id=$1", session_id
        )
        await restored_repository.close()

    print("PASS: PostgreSQL session replay + transactional Outbox publisher")


if __name__ == "__main__":
    asyncio.run(main())
