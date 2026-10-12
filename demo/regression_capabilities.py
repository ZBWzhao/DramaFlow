"""无模型费用回归：能力继承、真实自检、工作区写入与编辑。"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from core.agent import build_system_prompt
from core.loader import TOOLS_ROOT, create_agent_tools
import server.registry as registry_module


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        tools, _, base = await create_agent_tools(directory, root=TOOLS_ROOT)
        tool_map = {tool.name: tool for tool in tools}
        required = {"capabilities", "write_file", "edit_file", "get_canvas", "generate_media"}
        assert required <= tool_map.keys(), required - tool_map.keys()

        # 模拟 SessionRegistry / 子 Agent 派生运行上下文。
        runtime = base.derive(tool_names=set(tool_map))
        assert runtime.media is base.media and runtime.media is not None
        assert runtime._locks is base._locks

        report = await tool_map["capabilities"].execute({}, runtime)
        assert report.is_error is False
        assert report.details["capabilities"]["canvas"] is True
        assert report.details["capabilities"]["file_write"] is True
        assert report.details["media_mode"] == "mock"

        written = await tool_map["write_file"].execute(
            {"path": "draft.md", "content": "第一版剧本", "exclusive": True}, runtime
        )
        assert written.is_error is False
        edited = await tool_map["edit_file"].execute(
            {"path": "draft.md", "old_text": "第一版", "new_text": "最终版", "replace_all": False}, runtime
        )
        assert edited.is_error is False
        assert Path(directory, "draft.md").read_text(encoding="utf-8") == "最终版剧本"

        prompt = build_system_prompt(directory, tools)
        assert "作品成果必须完整，不得为了简洁压成梗概" in prompt
        assert "优先读取 workflow" in prompt

        # 覆盖曾经出错的真实路径：SessionRegistry 创建会话后不能丢失 media。
        registry_module.SESSIONS_DIR = Path(directory, "sessions")
        registry_module.SESSIONS_DIR.mkdir()
        registry = registry_module.SessionRegistry(llm=None, workspace=directory)  # type: ignore[arg-type]
        await registry.ainit()
        live = registry.get_or_create("regression")
        assert live.agent.context.media is registry.base_context.media
        assert {"write_file", "edit_file", "get_canvas", "read_skill"} <= live.agent.context.tool_names
        assert "read_skill" in live.manager._available

    print("PASS: capabilities/context/workspace/orchestration")


if __name__ == "__main__":
    asyncio.run(main())
