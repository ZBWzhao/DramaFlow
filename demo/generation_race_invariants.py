"""旧媒体任务完成后不得覆盖新一代 generation_id。"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.canvas import Canvas  # noqa: E402
from core.invocation_store import canonical_hash  # noqa: E402
from core.media import MediaResult, default_registry  # noqa: E402
from core.media_job_store import MediaJob  # noqa: E402
from server.app import WORKSPACE, _execute_media_job  # noqa: E402


class SlowProvider:
    name = "slow-test"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def list_models(self) -> list[dict]:
        return [{"id": "slow-image", "kind": "image"}]

    def generate(self, kind: str, request: dict, root: Path) -> MediaResult:
        callback = request.get("operation_callback")
        if callable(callback):
            callback("remote-race-1")
        self.started.set()
        self.release.wait(timeout=5)
        return MediaResult(kind=kind, path="assets/generated/old-result.png", mime="image/png", provider=self.name)

    def resume(self, kind: str, operation_id: str, request: dict, root: Path) -> MediaResult:
        return self.generate(kind, request, root)


async def main() -> None:
    name = f"generation-race-{uuid.uuid4().hex[:8]}"
    path = WORKSPACE / f"{name}.canvas.json"
    provider = SlowProvider()
    default_registry.providers[provider.name] = provider
    try:
        canvas = Canvas(name, path)
        node = canvas.add_node("image_gen", "竞态测试图片")
        generation_id = f"media_{'a' * 32}:{node.id}"
        canvas.update_node(node.id, data={"active_generation_id": generation_id})
        canvas.save()
        planned = {"node_id": node.id, "kind": "image", "model": "slow-image"}
        payload = {
            "canvas": name,
            "approval_id": "apr_race",
            "provider": provider.name,
            "requested_model": "slow-image",
            "duration": 5,
            "resolution": "720p",
            "ratio": "16:9",
            "generate_audio": False,
            "jobs": [{
                "node_id": node.id, "label": node.label, "kind": "image", "ready": True,
                "prompt": "old prompt", "references": [], "model": "slow-image",
                "args_hash": canonical_hash(planned), "generation_id": generation_id,
            }],
        }
        task = MediaJob(
            job_id="media_" + "a" * 32, idempotency_key="race", approval_id="apr_race",
            canvas=name, payload_json=json.dumps(payload, ensure_ascii=False),
        )
        running = asyncio.create_task(_execute_media_job(task))
        assert await asyncio.to_thread(provider.started.wait, 3)

        latest = Canvas.load(path, name)
        latest.update_node(node.id, data={"active_generation_id": "new-generation"})
        latest.save()
        provider.release.set()
        result = await running
        assert result["results"][0]["stale_discarded"] is True
        final = Canvas.load(path, name).require(node.id)
        assert final.data["active_generation_id"] == "new-generation"
        assert "image" not in final.outputs, "旧结果不得写入新一代节点"
        print("PASS: 旧 Worker 晚到的结果被 generation_id 拦截，未覆盖新节点输出")
    finally:
        default_registry.providers.pop(provider.name, None)
        if path.exists():
            path.unlink()


if __name__ == "__main__":
    asyncio.run(main())
