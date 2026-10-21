"""技能系统：渐进式披露（progressive disclosure）。

对照 Toonflow：apps/server/src/agent/skills/index.ts + runtime/prompt.ts
  · SKILL.md 带 frontmatter（**name 与 description 必填**，且 name 要匹配目录名）
  · 提示词里只放 available_skills 清单：**name + description + location**
  · prompt.ts 的硬约束：*"不能只凭名称和描述开始执行"* —— 需要时必须 read 正文
  · 正文与 references 按需读；技能目录内路径要过沙箱

为什么这么做（简历能讲的点）：
  技能数量会增长，但**每轮请求的上下文不应该随之线性膨胀**。
  清单常驻（几十字 × N），正文按需加载（几千字，只在用到时才进上下文）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .plugin import ToolDefinition, define_tool

SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"


# ---------------------------------------------------------------------------
# frontmatter 解析（只支持 key: value，够用且零依赖）
# ---------------------------------------------------------------------------
def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    data: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data, "\n".join(lines[end + 1 :]).strip()


class SkillEntry(BaseModel):
    """装载期拿到的**元数据** —— 正文不在里面。"""

    name: str
    description: str = Field(min_length=1)
    location: str  # 相对路径，给模型去读
    body_chars: int = 0
    disable_model_invocation: bool = False
    version: str = "0.1.0"
    role: str = ""  # script-analysis | director | asset；空 = 未分类
    allowed_tools: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 发现
# ---------------------------------------------------------------------------
def discover_skills(root: Path = SKILLS_ROOT) -> tuple[list[SkillEntry], list[str]]:
    """扫 skills/<name>/SKILL.md。装载失败只记错误，不拖垮 Agent。"""
    entries: list[SkillEntry] = []
    errors: list[str] = []
    if not root.exists():
        return entries, [f"技能目录不存在: {root}"]

    for path in sorted(root.glob("*/SKILL.md")):
        try:
            frontmatter, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except OSError as exc:
            errors.append(f"{path.parent.name}: 读取失败 {exc}")
            continue

        name = frontmatter.get("name") or path.parent.name
        description = frontmatter.get("description", "")
        if name != path.parent.name:
            errors.append(f"{path.parent.name}: frontmatter 的 name({name}) 与目录名不一致")
            continue
        if not description.strip():
            errors.append(f"{name}: description 为空（SKILL.md 必须写明什么时候该用它）")
            continue

        entries.append(
            SkillEntry(
                name=name,
                description=description.strip(),
                location=f"skills/{path.parent.name}/SKILL.md",
                body_chars=len(body),
                disable_model_invocation=frontmatter.get("disable-model-invocation", "").lower() == "true",
                version=frontmatter.get("version", "0.1.0"),
                role=frontmatter.get("role", ""),
                allowed_tools=_parse_tool_list(frontmatter.get("allowed_tools", "")),
            )
        )
    return entries, errors


def _parse_tool_list(raw: str) -> list[str]:
    """allowed_tools 只写 `[a, b]` 形式——YAML 列表语法不值得为它加依赖。"""
    if not raw.strip():
        return []
    return [t.strip() for t in raw.strip().strip("[]").split(",") if t.strip()]


def validate_skill_tools(entries: list[SkillEntry], tool_names: set[str]) -> list[str]:
    """白名单引用的工具必须存在——装载期拦截，不给运行期留幽灵引用。"""
    errors = []
    for entry in entries:
        for tool in entry.allowed_tools:
            if tool not in tool_names:
                errors.append(f"{entry.name}: allowed_tools 引用了不存在的工具「{tool}」")
    return errors


def skills_by_role(entries: list[SkillEntry]) -> dict[str, SkillEntry]:
    """role → SkillEntry（同 role 多个时取排序靠后的；skill 目录名排序保证确定性）。"""
    by_role: dict[str, SkillEntry] = {}
    for entry in entries:
        if entry.role:
            by_role[entry.role] = entry
    return by_role


def skills_prompt_section(entries: list[SkillEntry]) -> str:
    """注入提示词的**只有清单**：名字 + 什么时候用 + 去哪读。"""
    if not entries:
        return ""
    lines = [
        "<available_skills>",
        "技能正文不在上下文里。判断相关后必须用 read_skill 读取正文再动手，",
        "不能只凭名称和描述开始执行。",
    ]
    for entry in entries:
        flag = "（仅在用户明确指定时使用）" if entry.disable_model_invocation else ""
        lines.append(f"- {entry.name}{flag}：{entry.description} → {entry.location}")
    lines.append("</available_skills>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 按需读正文（带沙箱）
# ---------------------------------------------------------------------------
def read_skill_file(name: str, relative: str = "SKILL.md", root: Path = SKILLS_ROOT) -> str:
    base = (root / name).resolve()
    target = (base / relative).resolve()
    if not str(target).lower().startswith(str(base).lower()):
        raise ValueError(f"路径越界：{relative}")
    if not target.exists():
        raise FileNotFoundError(f"技能 {name} 没有这个文件：{relative}")
    return target.read_text(encoding="utf-8")


class ReadSkillArgs(BaseModel):
    name: str = Field(description="技能名（available_skills 里的名字）")
    path: str = Field(default="SKILL.md", description="技能目录内的相对路径，默认读正文 SKILL.md")


def build_skill_tools(root: Path = SKILLS_ROOT) -> list[ToolDefinition]:
    """技能工具只有 read —— 清单已经在提示词里了，不需要 list。"""

    def _read(args: ReadSkillArgs, _ctx: Any) -> dict[str, Any]:
        body = read_skill_file(args.name, args.path, root)
        return {"skill": args.name, "path": args.path, "content": body}

    return [
        define_tool(
            name="read_skill",
            description=("读取技能正文或它的参考资料。判断某个技能与当前任务相关后必须先读正文，"
                         "再按正文执行；references 只按当前问题读，不要一次读完。"),
            args_model=ReadSkillArgs,
            execute=_read,
            prompt_guidelines=["没有读取正文就声称使用了某个技能，属于错误行为。"],
        )
    ]
