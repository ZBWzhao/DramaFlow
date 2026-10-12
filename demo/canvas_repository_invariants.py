"""CanvasRepository optimistic-lock and legacy import invariants."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.canvas import Canvas
from core.canvas_repository import CanvasConflictError, CanvasRepository, FileCanvasRepository


async def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        legacy = Canvas("legacy", root / "legacy.canvas.json")
        legacy.add_node("text", "旧画布")
        # Simulate a pre-repository file without a revision field.
        raw = legacy.to_dict()
        raw.pop("revision", None)
        legacy.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        repository = FileCanvasRepository(root)
        assert isinstance(repository, CanvasRepository)
        await repository.start()
        first = await repository.load("legacy")
        second = await repository.load("legacy")
        assert first.revision == second.revision == 0

        first.add_node("text", "用户修改")
        assert await repository.save(first) == 1
        second.add_node("text", "过期 Agent 修改")
        try:
            await repository.save(second)
        except CanvasConflictError as exc:
            assert exc.expected == 0 and exc.actual == 1
        else:
            raise AssertionError("stale canvas write was not rejected")

        latest = await repository.load("legacy")
        assert latest.revision == 1
        assert {node.label for node in latest.nodes.values()} == {"旧画布", "用户修改"}
        assert not list(root.glob("*.lock"))
        print("PASS: legacy canvas acquires revision on first repository save")
        print("PASS: stale writer receives CanvasConflictError and cannot overwrite newer state")
        await repository.close()


if __name__ == "__main__":
    asyncio.run(main())
