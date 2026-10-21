"""P0-1 / P0-2：子 Agent 技能注入与团队角色 md 的契约一致性。

背景：子 Agent 以前完全拿不到 skills（`build_system_prompt(cwd, tools)` 少了第三参），
团队角色 md 又停在旧 6 字段时代 —— 两件事一起让「子 Agent 的生产工艺」整层失效。
这组测试锁住三件事：
1. team.json 声明的 `skills` 真存在；成员提示词文件存在且非空壳；
2. 带技能的子会话会补上 `read_skill` 工具，并拿到 `<available_skills>` 清单与角色段；
3. 白名单能真的裁掉技能（写 director 的成员看不到 asset），旧会话会被重建。

运行：``python -m pytest tests/prompt/test_team_skill_injection.py -q``
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.events import EventBus
from core.skills import build_skill_tools, discover_skills
from core.subagent import DelegationManager, DelegationResult
from core.team import TeamRunner, load_team

TEAMS = ("dramaTeam", "storyboardTeam")


def _manager(skills=None) -> DelegationManager:
    return DelegationManager(parent=SimpleNamespace(), llm=None, cwd=str(ROOT_DIR), skills=skills)


def test_team_manifests_declare_existing_skills_and_instructions():
    known = {entry.name for entry in discover_skills()[0]}
    for team in TEAMS:
        manifest, path = load_team(team)
        for name, member in manifest.members.items():
            assert member.skills, f"{team}/{name} 没声明 skills"
            assert set(member.skills) <= known, f"{team}/{name} 声明了不存在的技能 {member.skills}"
            target = path.parent / member.instructions
            assert target.exists(), f"{team}/{name} 的 instructions 不存在: {target}"
            assert target.stat().st_size > 200, f"{team}/{name} 的 instructions 太空"


def test_team_role_prompts_use_structured_shot_contract():
    """角色 md 不能再教模型写旧 6 字段（subject/duration），要写结构化字段。"""
    stale_markers = ("\"subject\":", "camera_move\":", "duration\": 5", "video_gen.media")
    for team in TEAMS:
        manifest, path = load_team(team)
        for name, member in manifest.members.items():
            text = (path.parent / member.instructions).read_text(encoding="utf-8")
            for marker in stale_markers:
                assert marker not in text, f"{team}/{name} 仍在教旧字段 {marker}"
    # 分镜相关的角色必须点名新字段与一镜一段
    storyboarder = (ROOT_DIR / "teams/dramaTeam/members/storyboarder.md").read_text(encoding="utf-8")
    for marker in ("一镜一段", "duration_s", "screen_pos", "gaze", "camera"):
        assert marker in storyboarder


def test_produce_instruction_uses_real_port_names():
    """端口是 prompt.prompt → video_gen.prompt；写成 media 会让 connect 直接报错。"""
    from server.studio import _PRODUCE_INSTRUCTION

    assert "video_gen" in _PRODUCE_INSTRUCTION
    assert "media" not in _PRODUCE_INSTRUCTION
    assert "prompt → video_gen" in _PRODUCE_INSTRUCTION


def test_child_tools_get_read_skill_added_once():
    manager = _manager()
    reader = build_skill_tools()[0]
    assert [tool.name for tool in manager._ensure_skill_reader([], manager.skills)] == ["read_skill"]
    # 已经有就不重复加
    assert [tool.name for tool in manager._ensure_skill_reader([reader], manager.skills)] == ["read_skill"]
    # 不给技能就不加（例如只要通用工具的临时子 Agent）
    assert manager._ensure_skill_reader([], []) == []


def test_child_prompt_carries_catalog_and_role():
    manager = _manager()
    skills = manager._skills_for(["director", "asset"])
    tools = manager._ensure_skill_reader([], skills)
    prompt = manager._child_system_prompt("", tools, skills, "# 你的角色：分镜师\n\n一镜一段")
    assert "<available_skills>" in prompt
    assert "director" in prompt and "asset" in prompt
    assert "<role>" in prompt and "分镜师" in prompt


def test_skill_whitelist_really_filters_catalog():
    manager = _manager()
    only_workflow = manager._skills_for(["director"])
    assert [entry.name for entry in only_workflow] == ["director"]
    prompt = manager._child_system_prompt("", [], only_workflow, None)
    # 只看技能清单段：<rules> 里固定会提到 asset（分层分工），不算注入
    catalog = prompt.split("<available_skills>")[1].split("</available_skills>")[0]
    assert "director" in catalog
    assert "asset" not in catalog


def test_stale_child_prompt_is_rebuilt_fresh_prompt_reused():
    manager = _manager()
    skills = manager._skills_for(["director"])
    stale = manager._child_system_prompt("旧的系统提示词，没有技能段", [], skills, None)
    assert "<available_skills>" in stale
    assert "旧的系统提示词" not in stale
    fresh = manager._child_system_prompt(
        "新版系统提示词\n<available_skills>\n- workflow\n</available_skills>", [], skills, None
    )
    assert fresh.startswith("新版系统提示词")


class _StubParent:
    """delegate 需要的父对象：会话、事件总线、context.derive、invocations。"""

    def __init__(self) -> None:
        from core.events import EventBus
        from core.session import Session

        self.session = Session()
        self.bus = EventBus()
        self.context = SimpleNamespace(derive=lambda **kwargs: SimpleNamespace(tool_names=kwargs.get("tool_names")))
        self.invocations = None


def test_delegate_builds_child_prompt_with_catalog_and_tools(monkeypatch):
    """跑通真正的 delegate：子会话拿到技能清单 + read_skill 工具（不联网）。"""
    import asyncio

    from core.agent import Agent as RealAgent

    captured: list[list] = []

    async def fake_run(self, user_input: str) -> str:
        captured.append([tool.name for tool in self.tools])
        return "子 Agent 收工"

    monkeypatch.setattr(RealAgent, "run", fake_run)
    manager = DelegationManager(parent=_StubParent(), llm=None, cwd=str(ROOT_DIR))

    async def _noop(args, ctx):
        return "ok"

    from pydantic import BaseModel

    from core.plugin import define_tool

    class _EmptyArgs(BaseModel):
        pass

    read_file_tool = define_tool(
        name="read_file",
        description="读文件",
        args_model=_EmptyArgs,
        execute=_noop,
    )
    manager._available = {"read_file": read_file_tool}
    result = asyncio.run(
        manager.delegate(
            name="分镜测试",
            task="按技能拆镜",
            tool_names=["read_file"],
            role_prompt="# 你的角色：分镜师\n\n一镜一段",
            skill_names=["director", "asset"],
        )
    )

    assert result.answer == "子 Agent 收工"
    record = manager.registry["分镜测试"]
    prompt = record.session.system_prompt
    assert "<available_skills>" in prompt
    assert "director" in prompt and "asset" in prompt
    assert "<role>" in prompt and "分镜师" in prompt
    # tools 白名单里没有 read_skill，也要自动补上（否则提示词让它读、它却读不了）
    assert "read_skill" in captured[0]
    assert "read_file" in captured[0]

    # 同名再委派：复用同一个子会话，提示词不重建
    before = record.session.system_prompt
    asyncio.run(manager.delegate(name="分镜测试", task="再拆一次", tool_names=["read_file"], skill_names=["director"]))
    assert manager.registry["分镜测试"] is record
    assert record.session.system_prompt == before


class _RecordingManager:
    """只记录 delegate 入参，不真的跑模型。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def delegate(self, **kwargs) -> DelegationResult:
        self.calls.append(kwargs)
        return DelegationResult(name=kwargs["name"], answer="ok", usage=None, usage_all=None)  # type: ignore[arg-type]


def test_team_runner_passes_member_skill_whitelist():
    import asyncio

    manager = _RecordingManager()
    runner = TeamRunner.create(
        "dramaTeam",
        llm=None,  # type: ignore[arg-type]
        available_tools=[],
        manager=manager,  # type: ignore[arg-type]
        bus=EventBus(),
    )
    asyncio.run(runner.run("测试需求"))

    manifest, _ = load_team("dramaTeam")
    asserted = {call["name"].removeprefix("dramaTeam_") for call in manager.calls}
    assert asserted == set(manifest.members)

    by_member = {call["name"].removeprefix("dramaTeam_"): call for call in manager.calls}
    for name, member in manifest.members.items():
        assert by_member[name]["skill_names"] == (member.skills or None)
    # reviewer 不该拿到 asset？—— 它要核对工艺字段，所以拿到才对；这条锁住"按声明走"
    assert "asset" in by_member["storyboarder"]["skill_names"]
