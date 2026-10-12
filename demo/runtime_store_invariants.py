"""运行状态 / 事件存储 / 重放游标的本地回归测试（不需要 Redis 服务）。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.events import EventBus  # noqa: E402
from core.runtime_store import InMemoryEventStore, InMemoryRunStateStore, RunState, event_id_after  # noqa: E402


async def main() -> None:
    events = InMemoryEventStore(maxlen=100)
    runs = InMemoryRunStateStore()
    await events.start()
    await runs.start()

    bus = EventBus(store=events, session_id="session_a")
    bus.run_id = "a" * 32
    queue = bus.subscribe()
    bus.emit("message_start", block_id="b1")
    bus.emit("message_update", delta="你好")
    done = await bus.publish("done", status="done")
    assert done.event_id
    assert queue.qsize() == 3

    replay = await events.read_after("session_a", "0-0")
    assert [item.payload["type"] for item in replay] == ["message_start", "message_update", "done"]
    assert all(event_id_after(replay[index].event_id, replay[index - 1].event_id) for index in range(1, 3))
    missing = await events.read_after("session_a", replay[0].event_id)
    assert [item.payload["type"] for item in missing] == ["message_update", "done"]

    state = RunState(run_id="a" * 32, session_id="session_a", kind="agent", start_event_id="0-0")
    assert await runs.claim(state)
    assert not await runs.claim(RunState(run_id="b" * 32, session_id="session_a", kind="agent"))
    assert (await runs.active_run("session_a")).run_id == state.run_id
    assert await runs.enqueue_followup(state.run_id, "补充要求") == 1
    assert await runs.drain_followups(state.run_id) == ["补充要求"]
    assert await runs.request_cancel(state.run_id)
    assert await runs.is_cancelled(state.run_id)
    assert await runs.heartbeat(state.run_id)
    await runs.finish(state.run_id, "done")
    restored = await runs.get(state.run_id)
    assert restored and restored.status == "done" and restored.finished_at
    assert await runs.active_run("session_a") is None

    await bus.close()
    await events.close()
    await runs.close()
    print("✓ RunStateStore 内存实现、运行互斥、取消与 Follow-up")
    print("✓ EventStore 递增 ID 与断点补发")
    print("✓ EventBus 按持久化顺序广播")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
