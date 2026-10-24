"""阶段 0 验收：pipeline_skills/ 下 7 个技能就位，frontmatter 可被仓库装载器解析。"""
from pathlib import Path

from core.skills import parse_frontmatter

PIPELINE_ROOT = Path(__file__).resolve().parent.parent / "pipeline_skills"

EXPECTED = {
    "novel-outline", "novel-script", "novel-characters",
    "novel-art", "novel-staging", "novel-storyboard", "character-refs",
}


def test_seven_skill_dirs_exist():
    actual = {p.name for p in PIPELINE_ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")}
    assert EXPECTED <= actual, f"缺技能目录: {EXPECTED - actual}"


def test_each_skill_md_parseable_and_name_matches_dir():
    for dirname in EXPECTED:
        text = (PIPELINE_ROOT / dirname / "SKILL.md").read_text(encoding="utf-8")
        frontmatter, _body = parse_frontmatter(text)
        assert frontmatter.get("name") == dirname, f"{dirname}: frontmatter name 不匹配"
        assert frontmatter.get("description", "").strip(), f"{dirname}: description 为空"
