"""Skill manifest 扩展：version/role/allowed_tools 解析 + 白名单装载校验。"""
from pathlib import Path

from core.skills import SkillEntry, discover_skills, skills_by_role, validate_skill_tools


def _write_skill(root: Path, name: str, frontmatter: str, body: str = "正文") -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")


def test_discover_parses_version_role_allowed_tools(tmp_path: Path) -> None:
    _write_skill(tmp_path, "director", (
        "name: director\nversion: 1.2.0\nrole: director\n"
        "description: 单集制作\nallowed_tools: [get_canvas, set_storyboard]\n"
    ))
    entries, errors = discover_skills(tmp_path)
    assert errors == []
    entry = entries[0]
    assert entry.version == "1.2.0"
    assert entry.role == "director"
    assert entry.allowed_tools == ["get_canvas", "set_storyboard"]


def test_defaults_when_fields_missing(tmp_path: Path) -> None:
    _write_skill(tmp_path, "legacy", "name: legacy\ndescription: 旧技能\n")
    entries, errors = discover_skills(tmp_path)
    assert errors == []
    assert entries[0].version == "0.1.0"
    assert entries[0].role == ""
    assert entries[0].allowed_tools == []


def test_validate_flags_unknown_tools() -> None:
    entry = SkillEntry(name="a", description="x", location="skills/a/SKILL.md",
                       allowed_tools=["get_canvas", "ghost_tool"])
    errors = validate_skill_tools([entry], {"get_canvas", "set_text"})
    assert len(errors) == 1 and "ghost_tool" in errors[0]


def test_skills_by_role(tmp_path: Path) -> None:
    _write_skill(tmp_path, "asset", "name: asset\nversion: 1.0.0\nrole: asset\ndescription: 资产\n")
    entries, _ = discover_skills(tmp_path)
    assert skills_by_role(entries)["asset"].name == "asset"


def test_three_role_skills_exist_and_referenced_skill_names_valid() -> None:
    """三角色重构后：角色齐、且 teams 清单引用的技能名都必须真实存在。"""
    import json
    from core.skills import SKILLS_ROOT, discover_skills

    entries, errors = discover_skills(SKILLS_ROOT)
    assert errors == [], errors
    roles = {e.role for e in entries}
    assert {"script-analysis", "director", "asset"} <= roles
    by_name = {e.name: e for e in entries}
    for team_file in (SKILLS_ROOT.parent / "teams").glob("*/team.json"):
        manifest = json.loads(team_file.read_text(encoding="utf-8"))
        for member in manifest["members"].values():
            for skill in member.get("skills", []):
                assert skill in by_name, f"{team_file.name}: 引用了不存在的技能 {skill}"


def test_update_story_analysis_tool_registered_and_whitelist_valid(tmp_path) -> None:
    """新工具已注册；三个角色 Skill 的白名单引用的工具全部真实存在（幽灵引用零容忍）。"""
    import asyncio

    from core.loader import create_agent_tools
    from core.skills import SKILLS_ROOT, discover_skills, validate_skill_tools

    # 校验域 = 插件工具 + skill 系统自带的 read_skill（registry/subagent 运行时会合并它们）
    from core.skills import build_skill_tools

    tools, _entries, _ctx = asyncio.run(create_agent_tools(cwd=str(tmp_path)))
    names = {t.name for t in tools} | {t.name for t in build_skill_tools()}
    assert "update_story_analysis" in names
    entries, _ = discover_skills(SKILLS_ROOT)
    assert validate_skill_tools(entries, names) == []


def test_update_story_analysis_writes_schema_validated_field(tmp_path) -> None:
    """字段写入前过 StoryAnalysis schema；episode_outline 的 JSON 字符串会被解析。"""
    import asyncio

    from core.canvas import Canvas
    from core.canvas_repository import FileCanvasRepository
    from core.context import ToolContext
    from core.loader import create_agent_tools

    async def scenario():
        repo = FileCanvasRepository(tmp_path)
        canvas = Canvas("main", path=tmp_path / "main.canvas.json")
        canvas.meta["story_analysis"] = {"worldview": "旧"}
        await repo.save(canvas)

        tools, _entries, _ctx = await create_agent_tools(cwd=str(tmp_path), canvas_repository=repo)
        tools = {t.name: t for t in tools}
        ctx = ToolContext(
            cwd=str(tmp_path),
            config={"default_canvas": "main"},
            canvas_repository=repo,
        )
        tool = tools["update_story_analysis"]
        await tool.execute({"field": "synopsis", "value": "新梗概"}, ctx)
        await tool.execute({"field": "episode_outline", "value": json_str}, ctx)
        saved = await repo.load("main")
        analysis = saved.meta["story_analysis"]
        assert analysis["worldview"] == "旧"  # 未指名的字段原样保留
        assert analysis["synopsis"] == "新梗概"
        assert analysis["episode_outline"][0]["no"] == 1  # JSON 字符串被解析成数组
        assert analysis["updated_at"] > 0
        return tool

    json_str = '[{"no": 1, "title": "第1集", "hook": "h", "beats": []}]'
    asyncio.run(scenario())


def test_update_story_analysis_rejects_bad_outline(tmp_path) -> None:
    """episode_outline 不是数组时 Pydantic 校验拦截，画布不被改。"""
    import asyncio

    from core.canvas import Canvas
    from core.canvas_repository import FileCanvasRepository
    from core.context import ToolContext
    from core.loader import create_agent_tools

    async def scenario():
        repo = FileCanvasRepository(tmp_path)
        canvas = Canvas("main", path=tmp_path / "main.canvas.json")
        canvas.meta["story_analysis"] = {"worldview": "旧"}
        await repo.save(canvas)
        tools, _entries, _ctx = await create_agent_tools(cwd=str(tmp_path), canvas_repository=repo)
        tools = {t.name: t for t in tools}
        ctx = ToolContext(cwd=str(tmp_path), config={"default_canvas": "main"}, canvas_repository=repo)
        tool = tools["update_story_analysis"]
        try:
            await tool.execute({"field": "episode_outline", "value": '{"not": "array"}'}, ctx)
        except Exception:
            pass
        saved = await repo.load("main")
        assert "episode_outline" not in saved.meta["story_analysis"]  # 校验失败不落盘

    asyncio.run(scenario())
