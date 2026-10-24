"""产物注册表：7 类产物、名字唯一、技能目录与 schema 文件真实存在。"""
from pathlib import Path

import pytest

from core.pipeline_artifacts import ARTIFACTS, for_skill, get

PIPELINE_ROOT = Path(__file__).resolve().parent.parent / "pipeline_skills"

EXPECTED_NAMES = {"outline", "script", "characters", "art", "staging", "storyboard", "character_refs"}


def test_seven_artifacts_in_pipeline_order():
    assert {a.name for a in ARTIFACTS} == EXPECTED_NAMES
    assert len(ARTIFACTS) == 7


def test_names_and_skills_unique():
    assert len({a.name for a in ARTIFACTS}) == 7
    assert len({a.skill for a in ARTIFACTS}) == 7


@pytest.mark.parametrize("art", ARTIFACTS, ids=lambda a: a.name)
def test_skill_dir_and_schema_doc_exist(art):
    skill_dir = PIPELINE_ROOT / art.skill
    assert (skill_dir / "SKILL.md").is_file(), f"{art.skill}: 缺 SKILL.md"
    assert (skill_dir / art.schema_doc).is_file(), f"{art.skill}: 缺 {art.schema_doc}"


def test_filename_endswith_json():
    for art in ARTIFACTS:
        assert art.filename.endswith(".json"), f"{art.name}: filename 必须是 .json"


def test_get_and_for_skill_roundtrip():
    for art in ARTIFACTS:
        assert get(art.name) is art
        assert for_skill(art.skill) is art
    with pytest.raises(KeyError):
        get("nope")
    with pytest.raises(KeyError):
        for_skill("drama_script")


def test_artifacts_in_canonical_pipeline_order():
    names = [a.name for a in ARTIFACTS]
    assert names == ["outline", "script", "characters", "art", "staging", "storyboard", "character_refs"]
