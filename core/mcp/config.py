"""Validated, secret-safe configuration for external MCP servers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

SERVER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    transport: Literal["stdio", "streamable-http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    url: str = ""
    # 子进程变量名 -> 宿主变量名；配置文件不存真正密钥。
    env_from: dict[str, str] = Field(default_factory=dict)
    # HTTP Header 名 -> 宿主环境变量名，例如 Authorization -> MY_MCP_AUTH。
    headers_from: dict[str, str] = Field(default_factory=dict)
    allow_tools: list[str] = Field(default_factory=list)
    deny_tools: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    max_result_chars: int = Field(default=16_000, ge=1_000, le=200_000)

    @model_validator(mode="after")
    def validate_target(self) -> "MCPServerConfig":
        if self.transport == "stdio" and not self.command.strip():
            raise ValueError("stdio MCP server 必须配置 command")
        if self.transport == "streamable-http":
            parsed = urlparse(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("streamable-http MCP server 必须配置有效的 http(s) URL")
        return self

    def permits(self, tool_name: str) -> bool:
        if tool_name in self.deny_tools:
            return False
        return not self.allow_tools or tool_name in self.allow_tools


class MCPConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    servers: dict[str, MCPServerConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_names(self) -> "MCPConfig":
        invalid = [name for name in self.servers if not SERVER_NAME.fullmatch(name)]
        if invalid:
            raise ValueError(f"MCP server 名称无效：{', '.join(invalid)}")
        return self


def load_mcp_config(path: Path) -> MCPConfig:
    """Load config. A missing file means 'no external MCP servers'."""
    if not path.exists():
        return MCPConfig()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return MCPConfig.model_validate(raw)
