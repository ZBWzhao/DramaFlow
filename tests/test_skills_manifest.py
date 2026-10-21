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
