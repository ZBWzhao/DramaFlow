"""配置与凭据。

对照 Toonflow：apps/server/src/utils/conf + 模型 provider 配置。
凭据读取顺序沿用 pi-lab/mini_pi.py：环境变量 → ~/.pi/agent/auth.json。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_MAX_OUTPUT_TOKENS = 8192
DEFAULT_CONTEXT_WINDOW = 128000


def load_api_key() -> str:
    """取 API Key：环境变量优先，其次读 Pi 自己的凭据文件。"""
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if key:
        return key.strip()
    auth_path = Path.home() / ".pi" / "agent" / "auth.json"
    if auth_path.exists():
        data = json.loads(auth_path.read_text(encoding="utf-8"))
        for provider in ("deepseek", "openai"):
            entry = data.get(provider) or {}
            if entry.get("key"):
                return entry["key"]
    raise SystemExit("找不到 API Key：请设置 DEEPSEEK_API_KEY，或确认 ~/.pi/agent/auth.json 存在。")


@dataclass
class Settings:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    context_window: int = DEFAULT_CONTEXT_WINDOW

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            api_key=load_api_key(),
            base_url=os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL),
            model=os.environ.get("LLM_MODEL", DEFAULT_MODEL),
            max_output_tokens=int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", str(DEFAULT_MAX_OUTPUT_TOKENS))),
            context_window=int(os.environ.get("LLM_CONTEXT_WINDOW", str(DEFAULT_CONTEXT_WINDOW))),
        )
