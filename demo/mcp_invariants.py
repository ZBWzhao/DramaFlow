"""End-to-end MCP smoke checks: public server and external-tool adapter."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp import Client  # noqa: E402

from core.context import ToolContext  # noqa: E402
from core.mcp import MCPManager  # noqa: E402
from mcp_server.server import mcp  # noqa: E402


async def check_public_server() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools.tools}
        assert {"list_canvases", "inspect_canvas", "create_story_canvas", "export_prompts"} <= names
        result = await client.call_tool("list_canvases", {})
        assert not result.is_error
        assert isinstance(result.structured_content, dict)
        templates = await client.list_resource_templates()
        assert any(str(item.uri_template) == "dramaflow://canvas/{name}" for item in templates.resource_templates)
        resource = await client.read_resource("dramaflow://canvas/main")
        assert resource.contents
        prompts = await client.list_prompts()
        assert any(item.name == "short_drama_workflow" for item in prompts.prompts)
        prompt = await client.get_prompt("short_drama_workflow", {"title": "雨夜追逐", "duration_seconds": "15"})
        assert prompt.messages


async def check_client_adapter() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config_path = Path(directory) / "mcp.json"
        config_path.write_text(
            json.dumps(
                {
                    "servers": {
                        "selftest": {
                            "transport": "stdio",
                            "command": sys.executable,
                            "args": ["-m", "mcp_server.server"],
                            "cwd": str(ROOT),
                            "allow_tools": ["list_canvases", "inspect_canvas"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        manager = MCPManager(config_path)
        try:
            await manager.start()
            status = manager.status()
            assert status[0]["status"] == "connected", status
            tools = manager.tool_definitions()
            assert [tool.name for tool in tools] == [
                "mcp_selftest_list_canvases",
                "mcp_selftest_inspect_canvas",
            ]
            result = await tools[0].execute({}, ToolContext(cwd=str(ROOT / "workspace")))
            assert not result.is_error
            assert result.details["mcp_server"] == "selftest"
        finally:
            await manager.close()


async def check_bad_config_isolated() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config_path = Path(directory) / "mcp.json"
        config_path.write_text("{not-json", encoding="utf-8")
        manager = MCPManager(config_path)
        await manager.start()
        assert manager.status()[0]["name"] == "__config__"
        assert manager.tool_definitions() == []


async def main() -> None:
    await check_public_server()
    await check_client_adapter()
    await check_bad_config_isolated()
    print("MCP invariants: OK")


if __name__ == "__main__":
    asyncio.run(main())
