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
