"""Redis Streams 真实连接回归；默认连接本机 6379，只写临时测试 Stream。"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.runtime_store import RedisRunStateStore, RedisStreamEventStore, RunState, event_id_after  # noqa: E402
from core.question_store import QuestionState, RedisQuestionStateStore  # noqa: E402


async def main() -> None:
    session_id = f"test-{uuid.uuid4().hex}"
    suffix = uuid.uuid4().hex
    prefix = f"dramaflow:test-events:{suffix}"
    url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    store = RedisStreamEventStore(
        url=url,
        prefix=prefix,
        maxlen=100,
    )
    await store.start()
    try:
        first = await store.append(session_id, {"type": "message_start", "run_id": "test"})
        second = await store.append(session_id, {"type": "message_update", "run_id": "test", "delta": "x"})
        third = await store.append(session_id, {"type": "done", "run_id": "test"})
        assert event_id_after(second, first) and event_id_after(third, second)
        replay = await store.read_after(session_id, first)
        assert [item.payload["type"] for item in replay] == ["message_update", "done"]
        assert await store.oldest_id(session_id) == first
        assert await store.latest_id(session_id) == third
        print("✓ Redis Stream ID 单调递增")
        print("✓ last_event_id 之后的遗漏事件可补发")
    finally:
        # 只删除本脚本刚创建、带随机会话 ID 的测试 Stream。
        await store._client().delete(store._key(session_id))
        await store.close()

    # 两个独立客户端模拟两个服务实例：运行占用、取消与 Follow-up 必须互相可见。
    state_prefix = f"dramaflow:test-runtime:{suffix}"
    first_store = RedisRunStateStore(url=url, prefix=state_prefix, retention_seconds=300, active_ttl_seconds=60)
    second_store = RedisRunStateStore(url=url, prefix=state_prefix, retention_seconds=300, active_ttl_seconds=60)
    await first_store.start()
    await second_store.start()
    run_id = uuid.uuid4().hex
    other_id = uuid.uuid4().hex
    try:
        assert await first_store.claim(RunState(run_id=run_id, session_id=session_id, kind="agent"))
        assert not await second_store.claim(RunState(run_id=other_id, session_id=session_id, kind="agent"))
        active = await second_store.active_run(session_id)
        assert active and active.run_id == run_id
        assert await second_store.heartbeat(run_id)
        assert await second_store.enqueue_followup(run_id, "补充一个反转") == 1
        assert await first_store.drain_followups(run_id) == ["补充一个反转"]
        assert await second_store.request_cancel(run_id)
        assert await first_store.is_cancelled(run_id)
        await first_store.finish(run_id, "aborted")
        assert await second_store.active_run(session_id) is None
        print("✓ 多实例运行互斥与状态共享")
        print("✓ 跨实例取消与 Follow-up 队列")
    finally:
        client = first_store._client()
        await client.delete(
            first_store._run_key(run_id),
            first_store._run_key(other_id),
            first_store._active_key(session_id),
            first_store._followup_key(run_id),
        )
        await first_store.close()
        await second_store.close()

    question_store = RedisQuestionStateStore(url=url, prefix=state_prefix, ttl_seconds=300)
    await question_store.start()
    question_id = f"q_{uuid.uuid4().hex[:10]}"
    try:
        await question_store.create(
            QuestionState(
                question_id=question_id,
                session_id=session_id,
                run_id=run_id,
                call_id="call-test",
                title="画幅",
                question="请选择画幅",
                options=["16:9", "9:16"],
                allow_custom=False,
                agent="main",
            )
        )
        assert len(await question_store.list_pending(session_id)) == 1
        invalid_rejected = False
        try:
            await question_store.answer(session_id, question_id, "4:3")
        except ValueError:
            invalid_rejected = True
        assert invalid_rejected
        answered = await question_store.answer(session_id, question_id, "9:16")
        assert answered.status == "answered" and answered.answer == "9:16"
        duplicate_rejected = False
        try:
            await question_store.answer(session_id, question_id, "16:9")
        except RuntimeError:
            duplicate_rejected = True
        assert duplicate_rejected
        print("✓ Redis 结构化问答、选项校验与重复回答防护")
    finally:
        await question_store._client().delete(
            question_store._key(question_id),
            question_store._pending_key(session_id),
        )
        await question_store.close()


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
