"""Provider operation_id 持久化与“恢复只查询、不重复创建”回归。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.invocation_store import InMemoryToolInvocationStore, InvocationState  # noqa: E402
from core.media import MediaResult, TFRouterProvider  # noqa: E402


class FakeTFRouter(TFRouterProvider):
    def __init__(self) -> None:
        self.api_key = "test"
        self.created = 0
        self.polled = 0

    def _json(self, path: str, body: dict) -> dict:
        if "generate" in path:
            self.created += 1
            return {"data": "remote-task-42"}
        self.polled += 1
        return {"status": "success", "data": {"data": "https://unused.invalid/result.mp4"}}

    def _save_asset(self, value: str, kind: str, root: Path, model: str) -> MediaResult:
        return MediaResult(kind=kind, path="assets/generated/test.mp4", mime="video/mp4", provider=self.name)


async def main() -> None:
    store = InMemoryToolInvocationStore()
    await store.start()
    state, _ = await store.prepare(InvocationState(
        invocation_id="inv_test", idempotency_key="key", session_id="", run_id="run",
        tool_name="media.generate", args_hash="hash", args_json="{}",
    ))
    await store.mark_executing(state.invocation_id)
    provider = FakeTFRouter()
    seen: list[str] = []

    def remember(operation_id: str) -> None:
        seen.append(operation_id)

    request = {"prompt": "test", "model": "Seedance 2.0", "operation_callback": remember}
    result = provider.generate("video", request, ROOT)
    assert result.path.endswith("test.mp4")
    assert seen == ["remote-task-42"] and provider.created == 1
    await store.set_remote_operation(state.invocation_id, seen[0])
    restored = await store.get(state.invocation_id)
    assert restored and restored.remote_operation_id == "remote-task-42"

    provider.resume("video", "remote-task-42", request, ROOT)
    assert provider.created == 1, "恢复路径不得再次调用 generate"
    assert provider.polled == 2
    print("PASS: Provider 创建后立即暴露并持久化 operation_id")
    print("PASS: 恢复路径只查询已有远端任务，不重复创建")


if __name__ == "__main__":
    asyncio.run(main())
