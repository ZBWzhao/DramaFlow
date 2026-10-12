"""媒体 Provider 与人工审批边界的无网络回归测试。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import Settings
from core.context import ToolContext
from core.llm import LLM
from core.media import MediaRegistry
from server.registry import SessionRegistry


async def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        registry = MediaRegistry()
        result = registry.generate(
            "video",
            {
                "prompt": "approval regression",
                "provider": "mock",
                "model": "mock-video-v1",
                "size": "128x128",
            },
            Path(folder),
        )
        assert result.mock is True
        assert (Path(folder) / result.path).is_file()
        assert all("api_key" not in item for item in registry.describe())

    approved = ToolContext(cwd=str(Path("workspace").resolve()), media_approved=True)
    assert approved.derive().media_approved is False, "媒体审批不得传播到下一上下文"

    sessions = SessionRegistry(LLM(Settings.load()), str(Path("workspace").resolve()))
    await sessions.ainit()
    live = sessions.get_or_create("approval-regression")
    names = {tool.name for tool in live.tools_for(True, allow_media_generation=False)}
    assert "generate_media" not in names
    assert "run_canvas" not in names
    assert "list_media_models" in names
    print("PASS: media provider / approval guard / non-sticky authorization")


if __name__ == "__main__":
    asyncio.run(main())
