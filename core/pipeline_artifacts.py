"""管线产物注册表：产物类型 ↔ 产出技能 ↔ JSON 文件名的唯一映射。

事实来源：docs/pipeline/data-contract.md（Task 2 定稿）。
阶段 1 导入 API 与阶段 2 运行时从这张表取名字，禁止在别处散落产物名字符串。
字段值与契约文档"类型映射表"逐字一致；契约文档更新时同步更新本表。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


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


# ---------------------------------------------------------------------------
# schema_version 解析（阶段 1 自 server/pipeline.py 整体迁入，行为不变）
#
# schema_version 的唯一写者是导入路由（server/pipeline.py）：请求体显式给出
# （非空串）优先，否则从产出技能 pipeline_skills/<skill>/SKILL.md frontmatter
# 的 `version:` 行推导（进程内缓存）；两者都拿不到记空串并附告警 finding。
# ---------------------------------------------------------------------------

_SKILLS_ROOT = Path(__file__).resolve().parent.parent / "pipeline_skills"
# frontmatter version 进程内缓存：技能文件运行期不变，含不可读的空串结果也缓存，
# 避免每次导入重复读盘。清空即失效（仅测试需要）。
_VERSION_CACHE: dict[str, str] = {}

# version: 可在 frontmatter 顶层，也可缩进在 metadata: 下，故匹配任意缩进。
_VERSION_LINE = re.compile(r"^\s*version:\s*(\S+)\s*$", re.MULTILINE)


def resolve_schema_version(art: ArtifactType, requested: str) -> tuple[str, list[str]]:
    """显式非空优先；否则 frontmatter `version:` 推导；不可读 → ("", 告警 finding)。"""
    explicit = (requested or "").strip()
    if explicit:
        return explicit, []
    version = _VERSION_CACHE.get(art.skill)
    if version is None:
        version = ""
        try:
            text = (_SKILLS_ROOT / art.skill / "SKILL.md").read_text(encoding="utf-8")
            frontmatter = text.split("---", 2)[1] if text.startswith("---") else ""
            match = _VERSION_LINE.search(frontmatter)
            if match:
                version = match.group(1)
        except (OSError, IndexError):
            pass
        _VERSION_CACHE[art.skill] = version
    if version:
        return version, []
    return "", [
        f"{art.name}: 未能从技能 {art.skill}/SKILL.md frontmatter 读取 version，"
        "schema_version 记为空串"
    ]
