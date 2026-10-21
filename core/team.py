"""声明式多 Agent 团队。

对照 Toonflow：packages/teams/storyboardTeam/team.json + packages/teamScaffold/ + agent/teams/index.ts
  · team.json 声明成员、委派关系、工具白名单（Zod schema → Pydantic）
  · createTeamRunner 按 entry 成员开始跑  →  TeamRunner.run()

设计取舍：**成员 = 子 Agent**。
  团队运行器本身不是 Agent，它只是"按声明编排子 Agent"的调度器：
  每个成员都走 DelegationManager.delegate()，于是自动获得
  独立会话、取消级联、工具白名单、**增量 token 计量** —— 不重复造轮子。

最小字段集（面试答点）：
  entry（从谁开始）+ members（谁在队里）+ delegates（谁能派给谁）+ tools（谁能用什么）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .events import EventBus
from .llm import LLM, Usage
from .plugin import ToolDefinition
from .subagent import DelegationManager

TEAMS_ROOT = Path(__file__).resolve().parent.parent / "teams"

# ---------------------------------------------------------------------------
# 成本控制：流水线里只传**摘要**，完整产物让下游自己去工作区读
#
# 为什么：上游写了 3000 字，如果全文塞进每个下游成员的上下文，
# 5 个成员就要把这段文字重复计费 4 次。而事实是"下游只需要知道上游做了什么、
# 产物在哪，细节按需去读" —— 这正是本项目一直强调的"按需加载"。
# ---------------------------------------------------------------------------
PLAN_CHARS = 400  # 入口成员定的约束摘要
UPSTREAM_CHARS = 600  # 交给下游的上游结论
SUMMARY_CHARS = 400  # 汇总时每个成员的产出摘要

READ_HINT = (
    "需要细节时用 `read_file` 读**你真正需要的那一个文件**（节点正文在 `data.content_path`）；"
    "不要把所有节点的正文都读一遍 —— 那是白花 token。"
)


def clip(text: str, limit: int) -> str:
    """截断长文本并明确告知完整内容在哪 —— 不能只说"已截断"就完事。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…（此处截断，原文共 {len(text)} 字；完整内容按下方提示去读）"


# ---------------------------------------------------------------------------
# 声明式 schema
# ---------------------------------------------------------------------------
class TeamMember(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=2000)
    instructions: str = Field(description="成员提示词的相对路径（members/*.md）")
    delegates: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list, description="工具白名单，不给就是没有工具")
    # 注入给该成员的技能子集（按 skills/<name> 取名）。空 = 全部可用，
    # 这样没写这个字段的老 team.json 行为不变。
    skills: list[str] = Field(
        default_factory=list,
        description="该成员可用的技能名（如 workflow / drama_craft）；空表示全部",
    )


class TeamManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(pattern=r"^[a-z][a-zA-Z0-9]*$")
    display_name: str = Field(alias="displayName", min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=4000)
    version: str = "1.0.0"
    author: str = ""
    entry: str
    members: dict[str, TeamMember] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_graph(self) -> "TeamManifest":
        if self.entry not in self.members:
            raise ValueError(f"入口成员不存在: {self.entry}")
        for name, member in self.members.items():
            for target in member.delegates:
                if target not in self.members:
                    raise ValueError(f"成员 {name} 委派了不存在的 {target}")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                raise ValueError(f"成员委派关系成环: {name}")
            visiting.add(name)
            for target in self.members[name].delegates:
                visit(target)
            visiting.discard(name)
            visited.add(name)

        for name in self.members:
            visit(name)
        return self


def load_team(name: str, root: Path = TEAMS_ROOT) -> tuple[TeamManifest, Path]:
    """装载并校验 team.json。配置错了要在装载期炸，不要等到跑到一半。"""
    path = root / name / "team.json"
    if not path.exists():
        raise FileNotFoundError(f"找不到团队: {name}（{path}）")
    manifest = TeamManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    return manifest, path


def read_instructions(team_dir: Path, member: TeamMember) -> str:
    """成员提示词放在 md 文件里（和 Toonflow 一样），好处是改提示词不用改代码。"""
    target = team_dir / member.instructions
    if not target.exists():
        return member.description
    return target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------
@dataclass
class StepResult:
    member: str
    role: str  # plan / delegate / summary
    answer: str
    usage: Usage = field(default_factory=Usage)
    usage_all: Usage = field(default_factory=Usage)


@dataclass
class TeamResult:
    team: str
    steps: list[StepResult] = field(default_factory=list)
    final: str = ""
    status: str = "done"
    production_run_id: str | None = None

    @property
    def usage(self) -> Usage:
        total = Usage()
        for step in self.steps:
            total.add(step.usage)
        return total

    @property
    def usage_all(self) -> Usage:
        """全量口径（对照用）：每个成员会话的累计值相加。"""
        total = Usage()
        for step in self.steps:
            total.add(step.usage_all)
        return total


class TeamRunner:
    """按 team.json 编排成员。流水线：entry 规划 → delegates 依次执行 → entry 汇总。"""

    def __init__(
        self,
        manifest: TeamManifest,
        team_dir: Path,
        llm: LLM,
        available_tools: list[ToolDefinition],
        manager: DelegationManager,
        bus: EventBus,
    ) -> None:
        self.manifest = manifest
        self.team_dir = team_dir
        self.llm = llm
        self.by_name = {t.name: t for t in available_tools}
        self.manager = manager
        self.bus = bus

    @classmethod
    def create(
        cls,
        team_name: str,
        llm: LLM,
        available_tools: list[ToolDefinition],
        manager: DelegationManager,
        bus: EventBus,
        root: Path = TEAMS_ROOT,
    ) -> "TeamRunner":
        manifest, path = load_team(team_name, root)
        return cls(manifest, path.parent, llm, available_tools, manager, bus)

    # ------------------------------------------------------------------
    async def _run_member(self, name: str, task: str, role: str) -> StepResult:
        member = self.manifest.members[name]
        tools = [t for t in member.tools if t in self.by_name]
        instructions = read_instructions(self.team_dir, member)
        # 不同团队经常都有 director / reviewer。若直接拿成员名作为子会话键，
        # 同一个聊天里切换团队会复用另一团队的历史和 system prompt。
        # 用 team + member 做运行时命名空间，彻底隔离角色会话。
        runtime_name = f"{self.manifest.name}_{name}"

        self.bus.emit("team_step_start", member=name, role=role, tools=tools)
        result = await self.manager.delegate(
            name=runtime_name,
            task=task,
            tool_names=tools,
            role_prompt=f"# 你的角色：{name}\n\n{instructions}",
            skill_names=member.skills or None,
        )
        self.bus.emit("team_step_end", member=name, role=role, chars=len(result.answer))
        return StepResult(member=name, role=role, answer=result.answer, usage=result.usage, usage_all=result.usage_all)

    async def run(self, task: str) -> TeamResult:
        entry = self.manifest.entry
        result = TeamResult(team=self.manifest.name)

        # ① 入口成员：定约束与验收标准（只要结论，不要正文）
        plan = await self._run_member(
            entry,
            f"用户需求：{task}\n\n请输出约束与验收标准（不要写正文，控制在 {PLAN_CHARS} 字以内）。",
            role="plan",
        )
        result.steps.append(plan)

        # ② 依次执行 delegates：只给上游摘要 + 读文件的提示
        upstream = clip(plan.answer, PLAN_CHARS)
        upstream_from = entry
        for name in self.manifest.members[entry].delegates:
            step = await self._run_member(
                name,
                (
                    f"用户需求：{task}\n\n"
                    f"上游（{upstream_from}）的结论摘要：\n{upstream}\n\n"
                    f"{READ_HINT}\n"
                    "只做你这一环，不要重复上游已经做完的部分。"
                ),
                role="delegate",
            )
            result.steps.append(step)
            upstream = clip(step.answer, UPSTREAM_CHARS)
            upstream_from = name

        # ③ 回到入口成员汇总：同样只给摘要，细节让它自己去读
        summary = await self._run_member(
            entry,
            f"用户需求：{task}\n\n各成员产出摘要：\n"
            + "\n\n".join(f"[{s.member}] {clip(s.answer, SUMMARY_CHARS)}" for s in result.steps[1:])
            + f"\n\n{READ_HINT}\n请逐条核对你在第一步定的验收标准，并汇总成最终结论。",
            role="summary",
        )
        result.steps.append(summary)
        result.final = summary.answer
        return result
