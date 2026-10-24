"""管线产物注册表：产物类型 ↔ 产出技能 ↔ JSON 文件名的唯一映射。

事实来源：docs/pipeline/data-contract.md（Task 2 定稿）。
阶段 1 导入 API 与阶段 2 运行时从这张表取名字，禁止在别处散落产物名字符串。
字段值与契约文档"类型映射表"逐字一致；契约文档更新时同步更新本表。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArtifactType:
    name: str        # 系统规范名（= DB pipeline_artifacts.artifact_type）
    skill: str       # 产出技能（pipeline_skills/ 目录名）
    filename: str    # 技能产出的 JSON 文件名
    schema_doc: str  # 技能内 schema 说明文件（相对技能目录；无独立文件填 "SKILL.md"）
    title: str       # 中文标题


ARTIFACTS: tuple[ArtifactType, ...] = (
    ArtifactType("outline", "novel-outline", "outline.json", "references/schema.md", "短剧大纲五件套"),
    ArtifactType("script", "novel-script", "script.json", "references/schema.md", "剧本"),
    ArtifactType("characters", "novel-characters", "cast.json", "references/schema.md", "角色设定卡"),
    ArtifactType("art", "novel-art", "art.json", "references/schema.md", "美术设定集"),
    ArtifactType("staging", "novel-staging", "staging.json", "references/schema.md", "开场站位"),
    ArtifactType("storyboard", "novel-storyboard", "storyboard.json", "references/schema.md", "分镜"),
    ArtifactType("character_refs", "character-refs", "asset.json", "references/schema.md", "角色参考图资产"),
)

_ARTIFACTS_BY_NAME = {a.name: a for a in ARTIFACTS}
_ARTIFACTS_BY_SKILL = {a.skill: a for a in ARTIFACTS}


def get(name: str) -> ArtifactType:
    return _ARTIFACTS_BY_NAME[name]


def for_skill(skill: str) -> ArtifactType:
    return _ARTIFACTS_BY_SKILL[skill]
