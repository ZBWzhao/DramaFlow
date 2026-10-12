"""Lifecycle, discovery, isolation and invocation for external MCP servers."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.stdio import get_default_environment
from mcp.client.streamable_http import streamable_http_client

from core.plugin import ToolDefinition

from .adapter import adapt_tool
from .config import MCPConfig, MCPServerConfig, load_mcp_config


@dataclass
class MCPConnection:
    name: str
    config: MCPServerConfig
    status: str = "configured"
    error: str = ""
    connected_at: float | None = None
    remote_tools: list[Any] = field(default_factory=list)
    client: Client | None = None
    stack: AsyncExitStack | None = None


class MCPManager:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.config = MCPConfig()
        self.config_error = ""
        self.connections: dict[str, MCPConnection] = {}
        self._tools: list[ToolDefinition] = []

    async def start(self) -> None:
        await self.close()
        self.config_error = ""
        try:
            self.config = load_mcp_config(self.config_path)
        except Exception as exc:  # noqa: BLE001 - bad optional config must not stop the main app
            self.config = MCPConfig()
            self.config_error = f"{type(exc).__name__}: {exc}"
            return
        for name, config in self.config.servers.items():
            record = MCPConnection(name=name, config=config)
            self.connections[name] = record
            if not config.enabled:
                record.status = "disabled"
                continue
            try:
                await self._connect(record)
            except Exception as exc:  # noqa: BLE001 - one plugin must not prevent app startup
                record.status = "error"
                record.error = f"{type(exc).__name__}: {exc}"
                if record.stack is not None:
                    await record.stack.aclose()
                    record.stack = None
                    record.client = None
        self._rebuild_tools()

    async def _connect(self, record: MCPConnection) -> None:
        config = record.config
        stack = AsyncExitStack()
        record.stack = stack
        if config.transport == "stdio":
            env = get_default_environment()
            for child_name, host_name in config.env_from.items():
                if host_name not in os.environ:
                    raise ValueError(f"环境变量未设置：{host_name}")
                env[child_name] = os.environ[host_name]
            target: Any = StdioServerParameters(
                command=config.command,
                args=config.args,
                env=env,
                cwd=config.cwd,
            )
        else:
            headers: dict[str, str] = {}
            for header_name, host_name in config.headers_from.items():
                if host_name not in os.environ:
                    raise ValueError(f"环境变量未设置：{host_name}")
                headers[header_name] = os.environ[host_name]
            if headers:
                http_client = await stack.enter_async_context(httpx2.AsyncClient(headers=headers))
                target = streamable_http_client(config.url, http_client=http_client)
            else:
                target = config.url
        client = Client(target, read_timeout_seconds=config.timeout_seconds)
        record.client = await stack.enter_async_context(client)
        remote_tools: list[Any] = []
        cursor: str | None = None
        while True:
            discovered = await asyncio.wait_for(
                record.client.list_tools(cursor=cursor), timeout=config.timeout_seconds
            )
            remote_tools.extend(discovered.tools)
            cursor = discovered.next_cursor
            if not cursor:
                break
        record.remote_tools = [tool for tool in remote_tools if config.permits(tool.name)]
        record.status = "connected"
        record.connected_at = time.time()

    def _rebuild_tools(self) -> None:
        tools: list[ToolDefinition] = []
        names: set[str] = set()
        for server, record in self.connections.items():
            if record.status != "connected":
                continue
            candidates = [adapt_tool(self, server, record.config, remote) for remote in record.remote_tools]
            candidate_names = [tool.name for tool in candidates]
            collisions = sorted(
                name for name in set(candidate_names)
                if candidate_names.count(name) > 1 or name in names
            )
            if collisions:
                record.status = "error"
                record.error = f"公开工具名冲突：{', '.join(collisions)}"
                continue
            names.update(candidate_names)
            tools.extend(candidates)
        self._tools = tools

    def tool_definitions(self) -> list[ToolDefinition]:
        return list(self._tools)

    async def call_tool(self, server: str, tool: str, arguments: dict[str, Any]) -> Any:
        record = self.connections.get(server)
        if record is None or record.status != "connected" or record.client is None:
            raise RuntimeError(f"MCP 服务不可用：{server}")
        if not record.config.permits(tool):
            raise PermissionError(f"MCP 工具未获白名单授权：{server}/{tool}")
        return await asyncio.wait_for(
            record.client.call_tool(
                tool,
                arguments,
                read_timeout_seconds=record.config.timeout_seconds,
            ),
            timeout=record.config.timeout_seconds + 1,
        )

    def status(self) -> list[dict[str, Any]]:
        rows = [
            {
                "name": record.name,
                "transport": record.config.transport,
                "status": record.status,
                "error": record.error,
                "tools": [tool.name for tool in record.remote_tools],
                "public_tools": [
                    tool.name for tool in self._tools
                    if tool.description.startswith(f"[MCP: {record.name}/")
                ],
                "connected_at": record.connected_at,
            }
            for record in self.connections.values()
        ]
        if self.config_error:
            rows.insert(
                0,
                {
                    "name": "__config__",
                    "transport": None,
                    "status": "error",
                    "error": self.config_error,
                    "tools": [],
                    "public_tools": [],
                    "connected_at": None,
                },
            )
        return rows

    async def close(self) -> None:
        for record in reversed(list(self.connections.values())):
            if record.stack is not None:
                try:
                    await record.stack.aclose()
                except Exception:  # noqa: BLE001 - continue closing other transports
                    pass
        self.connections.clear()
        self._tools.clear()
        # Shutdown remains best-effort. Connection errors were already observable
        # through status(); one broken plugin must not keep the web process alive.
