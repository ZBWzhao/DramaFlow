"""剧本页 AI 助手只读问答（phase 1 Task 7）：POST /api/agent 的 mode=script_qa。

红线语义：
- script_qa：本轮会话工具集为空（连 read_file 都不给），系统提示追加只读块
  <assistant_mode mode="script_qa">（内嵌 canvas 名 + story_title/story_summary/episodes 项目概要）；
- 默认（不带 mode）：行为与历史逐字段一致 —— 回归红线，本文件锁死。

装配模式照抄 tests/pipeline/test_pipeline_api.py（自带 _build_client，不跨目录 import
conftest —— 全量跑时 `conftest` 顶层名会被兄弟目录抢占）：临时 workspace + FakeLLM +
TestClient 走真实 app，lifespan 会调用 registry.ainit() 装载真实工具集。
断言直接落在 registry.sessions[sid] 的 agent.tools / agent.session.system_prompt 上：
这两处是 /api/agent 处理器在派发后台任务**之前**同步写好的状态，
也就是「真正递给 LLM 的工具与系统提示」。FakeLLM 没有 chat()，后台运行会在
首次模型调用时进入错误路径并正常收尾（error → done），不影响上述断言。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


class _FakeLLM:
    """Agent 首次 llm.chat() 即抛 AttributeError → 运行走错误路径收尾；会话状态断言不受影响。"""

    model = "fake-model"


def _build_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import contextlib

    from fastapi.testclient import TestClient

    import server.app as app_module
    from core.canvas_repository import create_canvas_repository
    from core.session_repository import JsonlSessionRepository
    from server.registry import SessionRegistry

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    registry = SessionRegistry(
        llm=_FakeLLM(),
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

    @contextlib.contextmanager
    def _run():
        # with 块触发 lifespan → registry.ainit() 装载真实工具集（红线断言的前提）
        with TestClient(app_module.app) as client:
            yield SimpleNamespace(client=client, registry=registry, workspace=workspace)

    return _run()


@pytest.fixture()
def qa(tmp_path, monkeypatch):
    with _build_client(tmp_path, monkeypatch) as env:
        yield env


def _seed_canvas(workspace: Path, name: str = "qa-canvas"):
    """带 meta 的项目画布：story_title / story_summary / episodes 各占一份。"""
    from core.canvas import Canvas

    canvas = Canvas(name, path=workspace / f"{name}.canvas.json")
    canvas.ensure_story("雨夜出租车", "深夜出租车司机程野在每个雨夜接到同一位乘客。")
    canvas.meta["episodes"] = [
        {"no": 1, "title": "末班车", "status": "done"},
        {"no": 2, "title": "后视镜", "status": "in_progress"},
    ]
    canvas.save()
    return canvas


def _post(client, **overrides):
    payload: dict = {"prompt": "第三集的反转是什么？", "canvas": "qa-canvas", "media_policy": "plan_only"}
    payload.update(overrides)
    return client.post("/api/agent", json=payload)


def _live(env, response):
    return env.registry.sessions[response.headers["X-Session-ID"]]


# ---------------------------------------------------------------------------
# script_qa：零工具 + 只读块 + 项目概要
# ---------------------------------------------------------------------------


def test_script_qa_zero_tools_and_project_summary_block(qa):
    _seed_canvas(qa.workspace)

    response = _post(qa.client, mode="script_qa")
    assert response.status_code == 200, response.text

    live = _live(qa, response)
    # 真正递给 LLM 的工具表就是空的；会话完整工具集仍在（派生而非就地改，开关不粘住）
    assert live.agent.tools == []
    assert live.full_tools

    prompt = live.agent.session.system_prompt
    assert '<assistant_mode mode="script_qa">' in prompt
    assert "只读问答助手" in prompt
    assert "不能调用任何工具" in prompt
    # 项目概要：画布名 + 故事标题 + 梗概 + 逐集列表
    assert "qa-canvas" in prompt
    assert "雨夜出租车" in prompt
    assert "深夜出租车司机程野" in prompt
    assert "第1集 末班车" in prompt
    assert "第2集 后视镜" in prompt


def test_script_qa_degrades_to_canvas_name_without_meta(qa):
    """画布缺席（无文件/meta 为空）：只读块仍在，概要退化为画布名，不抛错。"""
    response = _post(qa.client, canvas="no-such-canvas", mode="script_qa")
    assert response.status_code == 200, response.text

    live = _live(qa, response)
    assert live.agent.tools == []
    prompt = live.agent.session.system_prompt
    assert '<assistant_mode mode="script_qa">' in prompt
    assert "no-such-canvas" in prompt
    assert "第1集" not in prompt  # 没有分集数据就不编造


# ---------------------------------------------------------------------------
# 默认（无 mode）：回归红线 —— 与历史行为逐字段一致
# ---------------------------------------------------------------------------


def test_default_mode_regression_lock(qa):
    _seed_canvas(qa.workspace)

    response = _post(qa.client)  # 不带 mode
    assert response.status_code == 200, response.text

    live = _live(qa, response)
    expected_tools = live.tools_for(True, allow_media_generation=False)
    assert expected_tools, "测试装配必须有完整工具集，否则锁不住红线"
    assert live.agent.tools == expected_tools

    prompt = live.agent.session.system_prompt
    assert "<assistant_mode" not in prompt  # 历史提示里没有这个块
    from core.agent import build_system_prompt

    assert prompt.startswith(
        build_system_prompt(qa.registry.workspace, live.agent.tools, qa.registry.skills)
    )
    # media-approval 尾巴逐字节保留（plan_only 文案 + 固定收尾）
    assert "\n<media-approval>\n" in prompt
    assert "用户选择了仅策划" in prompt
    assert prompt.endswith("需要询问时直接在对话正文中提出清晰、可回答的问题，然后结束本轮等待用户。\n</media-approval>")


def test_default_mode_explicit_canvas_same_as_omitted(qa):
    _seed_canvas(qa.workspace)

    response = _post(qa.client, mode="canvas")
    assert response.status_code == 200, response.text
    live = _live(qa, response)
    assert live.agent.tools == live.tools_for(True, allow_media_generation=False)
    assert "<assistant_mode" not in live.agent.session.system_prompt


# ---------------------------------------------------------------------------
# 非法 mode：pydantic 422
# ---------------------------------------------------------------------------


def test_invalid_mode_is_422(qa):
    response = _post(qa.client, mode="nonsense")
    assert response.status_code == 422, response.text
