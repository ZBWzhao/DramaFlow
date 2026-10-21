"""Pipeline 端点测试夹具：临时 workspace + fake LLM + TestClient。

隔离原则与 tests/p0/conftest.py 相同：不改业务代码，import server.app 前
设置环境变量；studio 路由用 configure() 重新注入临时环境。
"""
from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# 必须在 import server.app 之前设置：Settings.load() 缺 key 会 SystemExit。
os.environ.setdefault("DEEPSEEK_API_KEY", "p0-test-key")
os.environ.setdefault("CANVAS_STORE_BACKEND", "file")
os.environ.setdefault("EVENT_STORE_BACKEND", "memory")
os.environ.setdefault("MEDIA_WORKER_EMBEDDED", "true")
for _name in ("DATABASE_URL", "REDIS_URL"):
    os.environ.pop(_name, None)


class FakeLLM:
    """按序回放回复；耗尽后回落到一份合法的世界观 JSON，避免流式端点挂死。"""

    def __init__(self, replies: list[str] | None = None):
        self.replies = list(replies or [])
        self.calls: list[list[dict]] = []

    async def simple(self, messages):
        self.calls.append(messages)
        if self.replies:
            return self.replies.pop(0)
        return '{"worldview": "默认世界观"}'


@contextlib.contextmanager
def _pipeline_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, llm: FakeLLM):
    from fastapi.testclient import TestClient

    import server.app as app_module
    from core.canvas_repository import create_canvas_repository
    from core.session_repository import JsonlSessionRepository
    from server.registry import SessionRegistry

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    registry = SessionRegistry(
        llm=llm,
        workspace=str(workspace),
        canvas_repository=create_canvas_repository(workspace),
        session_repository=JsonlSessionRepository(tmp_path / "sessions"),
    )
    monkeypatch.setattr(app_module, "WORKSPACE", workspace)
    monkeypatch.setattr(app_module, "registry", registry)

    # 制片台路由在 import 阶段就用「真实 registry/workspace」装配过一次
    # （server/app.py 的 studio.configure），必须重新注入临时环境。
    from server import studio as studio_module

    studio_module.configure(registry=registry, workspace=workspace, media_registry=None)

    with TestClient(app_module.app) as client:
        yield SimpleNamespace(client=client, registry=registry, llm=llm, workspace=workspace)


@pytest.fixture()
def pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """默认 fake LLM：任何 simple() 调用都返回合法世界观 JSON。"""
    with _pipeline_client(tmp_path, monkeypatch, FakeLLM()) as env:
        yield env


def make_project(client, *, title="测试项目", script="", skills=None, auto=False):
    body = {"title": title, "script": script, "split": True}
    if skills is not None:
        body["skills"] = skills
    if auto:
        body["auto_asset_table"] = True
    return client.post("/api/studio/projects", json=body).json()
