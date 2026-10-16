"""Acceptance-driven orchestration for the drama production team."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .canvas import Canvas
from .llm import Usage
from .production_repository import ProductionRunRepository
from .team import StepResult, TeamResult, TeamRunner, clip, PLAN_CHARS, SUMMARY_CHARS, UPSTREAM_CHARS, READ_HINT


class ProductionStage(str, Enum):
    PLANNING = "planning"
    SCRIPTING = "scripting"
    ASSET_DESIGN = "asset_design"
    STORYBOARDING = "storyboarding"
    PROMPTING = "prompting"
    REVIEWING = "reviewing"
    PREPARING_MEDIA = "preparing_media"
    WAITING_APPROVAL = "waiting_approval"
    GENERATING = "generating"
    WAITING_COMPOSITION = "waiting_composition"
    COMPOSING = "composing"
    FINAL_REVIEW = "final_review"
    COMPLETED = "completed"


MEMBER_STAGE = {
    "screenwriter": ProductionStage.SCRIPTING,
    "asset_designer": ProductionStage.ASSET_DESIGN,
    "storyboarder": ProductionStage.STORYBOARDING,
    "prompt_writer": ProductionStage.PROMPTING,
    "reviewer": ProductionStage.REVIEWING,
}


@dataclass
class ValidationResult:
    passed: bool
    problems: list[str] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)


def _node_text(canvas: Canvas, node_id: str) -> str:
    try:
        return canvas.read_text(node_id).strip()
    except (OSError, ValueError, KeyError):
        return ""


def validate_production_stage(stage: ProductionStage, canvas: Canvas, answer: str = "") -> ValidationResult:
    """Validate persisted artifacts, not the member's claim that it finished."""
    problems: list[str] = []
    artifacts: list[str] = []

    if stage == ProductionStage.PLANNING:
        text = (answer or "").strip()
        if len(text) < 60:
            problems.append("导演规划过短，没有形成可执行的制作规格")
        if not any(token in text for token in ("时长", "秒")):
            problems.append("导演规划没有明确总时长")
        if not any(token in text for token in ("画幅", "9:16", "16:9", "比例")):
            problems.append("导演规划没有明确画幅")
        if "验收" not in text:
            problems.append("导演规划没有给出验收标准")

    elif stage == ProductionStage.SCRIPTING:
        scripts = [
            node for node in canvas.nodes.values()
            if node.type == "text" and ("剧本" in node.label or node.data.get("kind") == "script")
        ]
        if not scripts:
            problems.append("画布中没有剧本正文节点")
        for node in scripts:
            text = _node_text(canvas, node.id)
            if len(text) < 200:
                problems.append(f"剧本「{node.label}」正文只有 {len(text)} 字，疑似摘要或未落盘")
            elif not any(token in text for token in ("场", "人物", "角色", "台词", "：")):
                problems.append(f"剧本「{node.label}」缺少可识别的场次、人物或台词结构")
            else:
                artifacts.append(node.id)

    elif stage == ProductionStage.ASSET_DESIGN:
        assets = [node for node in canvas.nodes.values() if node.type == "asset"]
        characters = [node for node in assets if node.data.get("kind") == "character"]
        scenes = [node for node in assets if node.data.get("kind") == "scene"]
        if not characters:
            problems.append("没有创建角色资产节点")
        if not scenes:
            problems.append("没有创建场景资产节点")
        for node in [*characters, *scenes]:
            if not node.data.get("consistency_tags"):
                problems.append(f"资产「{node.label}」缺少 consistency_tags")
            else:
                artifacts.append(node.id)

    elif stage == ProductionStage.STORYBOARDING:
        boards = [node for node in canvas.nodes.values() if node.type == "storyboard"]
        if not boards:
            problems.append("没有创建结构化分镜节点")
        relevant = ("分镜", "镜", "时长", "片段")
        problems.extend(item for item in canvas.validate() if any(token in item for token in relevant))
        artifacts.extend(node.id for node in boards if node.data.get("shots"))

    elif stage == ProductionStage.PROMPTING:
        prompts = [node for node in canvas.nodes.values() if node.type == "prompt"]
        if not prompts:
            problems.append("没有创建视频提示词节点")
        relevant = ("提示词", "参考图", "segment_no", "编号", "片段")
        problems.extend(item for item in canvas.validate() if any(token in item for token in relevant))
        for node in prompts:
            if _node_text(canvas, node.id):
                artifacts.append(node.id)
                try:
                    leftovers = canvas.render_prompt(node.id).get("leftovers") or []
                except (OSError, ValueError, KeyError) as exc:
                    problems.append(f"提示词「{node.label}」无法展开：{exc}")
                    continue
                if leftovers:
                    problems.append(f"提示词「{node.label}」存在无法展开的引用：{', '.join(leftovers)}")

    elif stage == ProductionStage.REVIEWING:
        problems.extend(canvas.validate())
        artifacts.extend(canvas.nodes.keys())

    # Preserve order while eliminating duplicate diagnostics.
    problems = list(dict.fromkeys(problems))
    return ValidationResult(not problems, problems, artifacts)


@dataclass
class ProductionRun:
    id: str
    session_id: str
    canvas_name: str
    team: str
    user_request: str
    current_stage: str = ProductionStage.PLANNING.value
    status: str = "running"
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    revision: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "canvas_name": self.canvas_name,
            "team": self.team,
            "user_request": self.user_request,
            "current_stage": self.current_stage,
            "status": self.status,
            "stages": self.stages,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProductionRun":
        names = cls.__dataclass_fields__.keys()
        return cls(**{name: raw[name] for name in names if name in raw})


class ProductionRunner:
    """Run dramaTeam as validated stages with bounded, targeted repair."""

    def __init__(
        self,
        team: TeamRunner,
        repository: ProductionRunRepository,
        *,
        session_id: str,
        canvas_name: str,
        production_run_id: str | None = None,
    ) -> None:
        self.team = team
        self.repository = repository
        self.session_id = session_id
        self.canvas_name = canvas_name
        self.production_run_id = production_run_id
        self.max_attempts = max(1, int(os.environ.get("PRODUCTION_STAGE_MAX_ATTEMPTS", "2")))

    async def _load_canvas(self) -> Canvas:
        return await self.team.manager.parent.context.require_canvas_repository().load(self.canvas_name)

    async def _persist(self, run: ProductionRun, *, create: bool = False) -> None:
        saved = await (
            self.repository.create(run.to_dict())
            if create
            else self.repository.save(run.to_dict(), run.revision)
        )
        run.revision = int(saved.get("revision") or run.revision)
        run.updated_at = float(saved.get("updated_at") or time.time())

    async def _set_stage(
        self,
        run: ProductionRun,
        stage: ProductionStage,
        status: str,
        **values: Any,
    ) -> None:
        current = dict(run.stages.get(stage.value) or {})
        current.update({"stage": stage.value, "status": status, **values})
        run.current_stage = stage.value
        run.stages[stage.value] = current
        run.updated_at = time.time()
        await self._persist(run)

    def _emit(self, type_: str, run: ProductionRun, stage: ProductionStage, **data: Any) -> None:
        self.team.bus.emit(
            type_, production_run_id=run.id, stage=stage.value,
            stages=run.stages, production_status=run.status,
            production_run=run.to_dict(), **data,
        )

    @staticmethod
    def _repair_owner(stage: ProductionStage, problems: list[str], default: str) -> str:
        """Route a failed final review back to the member that can change the artifact."""
        if stage != ProductionStage.REVIEWING:
            return default
        text = "\n".join(problems)
        if any(token in text for token in ("提示词", "参考图", "segment_no", "片段")):
            return "prompt_writer"
        if any(token in text for token in ("分镜", "镜", "时长")):
            return "storyboarder"
        if any(token in text for token in ("资产", "consistency_tags", "角色", "场景")):
            return "asset_designer"
        if "剧本" in text:
            return "screenwriter"
        return default

    async def _validated_member(
        self,
        run: ProductionRun,
        member: str,
        stage: ProductionStage,
        task: str,
        role: str,
    ) -> tuple[StepResult, ValidationResult]:
        last_step: StepResult | None = None
        last_check = ValidationResult(False, ["阶段尚未执行"])
        for attempt in range(1, self.max_attempts + 1):
            performer = self._repair_owner(stage, last_check.problems, member) if attempt > 1 else member
            before_revision = (await self._load_canvas()).revision
            await self._set_stage(
                run, stage, "running" if attempt == 1 else "retrying",
                member=performer, attempts=attempt, started_at=time.time(), problems=[],
            )
            self._emit("production_stage_started", run, stage, member=performer, attempt=attempt)
            member_task = task
            if attempt > 1:
                member_task = (
                    f"{task}\n\n这是第 {attempt} 次定向修复。不要重建整张画布，只更新已有节点或补齐缺失节点。\n"
                    "上一次程序验收发现：\n- " + "\n- ".join(last_check.problems[:12])
                    + "\n修复完成后请列出实际修改的节点 ID。"
                )
            last_step = await self.team._run_member(performer, member_task, role=role)
            await self._set_stage(run, stage, "validating", member=performer, attempts=attempt)
            self._emit("production_stage_validating", run, stage, member=performer, attempt=attempt)
            current_canvas = await self._load_canvas()
            last_check = validate_production_stage(stage, current_canvas, last_step.answer)
            if (
                stage in {
                    ProductionStage.SCRIPTING,
                    ProductionStage.ASSET_DESIGN,
                    ProductionStage.STORYBOARDING,
                    ProductionStage.PROMPTING,
                }
                and current_canvas.revision <= before_revision
            ):
                last_check.problems.insert(0, "Agent 没有修改画布，不能用旧产物冒充本阶段交付")
                last_check.passed = False
            if last_check.passed:
                await self._set_stage(
                    run, stage, "succeeded", member=performer, attempts=attempt,
                    artifact_ids=last_check.artifact_ids, problems=[], finished_at=time.time(),
                )
                self._emit(
                    "production_stage_passed", run, stage, member=performer, attempt=attempt,
                    artifact_ids=last_check.artifact_ids,
                )
                return last_step, last_check
            await self._set_stage(
                run, stage, "needs_revision" if attempt < self.max_attempts else "failed",
                member=performer, attempts=attempt, problems=last_check.problems, finished_at=time.time(),
            )
            event = "production_stage_retry" if attempt < self.max_attempts else "production_stage_failed"
            self._emit(event, run, stage, member=performer, attempt=attempt, problems=last_check.problems)
        assert last_step is not None
        return last_step, last_check

    async def _prepare_generation_graph(self, run: ProductionRun) -> list[str]:
        """Deterministically bridge approved prompts to media and timeline nodes.

        Agents author creative artifacts; this mechanical graph step should not
        consume another model round or depend on the model remembering ports.
        """
        stage = ProductionStage.PREPARING_MEDIA
        await self._set_stage(run, stage, "running", started_at=time.time(), problems=[])
        self._emit("production_stage_started", run, stage, member="system", attempt=1)
        canvas = await self._load_canvas()
        changed = False
        created: list[str] = []
        clips: list[str] = []
        prompts = sorted(
            (node for node in canvas.nodes.values() if node.type == "prompt"),
            key=lambda node: (int(node.data.get("segment_no") or 10**6), node.label),
        )
        for prompt in prompts:
            existing = next(
                (
                    canvas.nodes[edge.target]
                    for edge in canvas.outgoing(prompt.id)
                    if edge.source_handle == "prompt"
                    and edge.target in canvas.nodes
                    and canvas.nodes[edge.target].type == "video_gen"
                ),
                None,
            )
            video = existing or canvas.add_node(
                "video_gen",
                f"视频生成·{prompt.label}",
                data={"segment_no": prompt.data.get("segment_no"), "source_prompt_id": prompt.id},
            )
            if existing is None:
                canvas.connect(prompt.id, "prompt", video.id, "prompt")
                created.append(video.id)
                changed = True
            clips.append(video.id)

        timeline = next((node for node in canvas.nodes.values() if node.type == "timeline"), None)
        if timeline is None:
            timeline = canvas.add_node(
                "timeline", "最终成片",
                data={"clip_order": clips, "output": {"width": 720, "height": 1280, "fps": 24}},
            )
            created.append(timeline.id)
            changed = True
        else:
            if list(timeline.data.get("clip_order") or []) != clips:
                changed = True
            timeline.data["clip_order"] = clips
        for video_id in clips:
            before = len(canvas.edges)
            canvas.connect(video_id, "video", timeline.id, "video")
            changed = changed or len(canvas.edges) > before
        if changed:
            canvas.arrange()
            await self.team.manager.parent.context.require_canvas_repository().save(canvas)

        problems: list[str] = []
        if not prompts:
            problems.append("没有可进入媒体生成的提示词节点")
        if not clips:
            problems.append("没有准备出视频生成节点")
        if problems:
            await self._set_stage(
                run, stage, "failed", artifact_ids=created, problems=problems, finished_at=time.time()
            )
            self._emit("production_stage_failed", run, stage, member="system", attempt=1, problems=problems)
            raise RuntimeError("；".join(problems))
        artifacts = [*clips, timeline.id]
        await self._set_stage(
            run, stage, "succeeded", artifact_ids=artifacts, problems=[], finished_at=time.time()
        )
        self._emit("production_stage_passed", run, stage, member="system", attempt=1, artifact_ids=artifacts)
        return artifacts

    async def run(self, task: str) -> TeamResult:
        if self.production_run_id:
            stored = await self.repository.get(self.production_run_id)
            if stored is None:
                raise ValueError("要恢复的生产任务不存在")
            run = ProductionRun.from_dict(stored)
            if run.session_id != self.session_id or run.canvas_name != self.canvas_name:
                raise ValueError("生产任务与当前会话或画布不匹配")
            run.status = "running"
            await self._persist(run)
        else:
            run = ProductionRun(
                id=uuid.uuid4().hex, session_id=self.session_id, canvas_name=self.canvas_name,
                team=self.team.manifest.name, user_request=task,
            )
            await self._persist(run, create=True)

        result = TeamResult(team=self.team.manifest.name, production_run_id=run.id)
        self._emit("production_run_started", run, ProductionStage(run.current_stage), resumed=bool(self.production_run_id))

        entry = self.team.manifest.entry
        plan_state = run.stages.get(ProductionStage.PLANNING.value, {})
        if plan_state.get("status") == "succeeded":
            plan_answer = str(plan_state.get("answer") or "已恢复导演规划")
        else:
            plan, check = await self._validated_member(
                run, entry, ProductionStage.PLANNING,
                f"用户需求：{task}\n\n请输出约束与验收标准（不要写正文，控制在 {PLAN_CHARS} 字以内）。",
                "plan",
            )
            result.steps.append(plan)
            if not check.passed:
                return await self._limit(result, run, ProductionStage.PLANNING, check.problems)
            plan_answer = plan.answer
            run.stages[ProductionStage.PLANNING.value]["answer"] = clip(plan_answer, PLAN_CHARS)
            await self._persist(run)

        upstream, upstream_from = clip(plan_answer, PLAN_CHARS), entry
        for member in self.team.manifest.members[entry].delegates:
            stage = MEMBER_STAGE.get(member)
            if stage is None:
                continue
            existing = run.stages.get(stage.value, {})
            if existing.get("status") == "succeeded":
                upstream = str(existing.get("answer") or f"{member} 阶段已在上次运行中通过验收")
                upstream_from = member
                continue
            step, check = await self._validated_member(
                run, member, stage,
                (
                    f"用户需求：{task}\n\n上游（{upstream_from}）的结论摘要：\n{upstream}\n\n"
                    f"{READ_HINT}\n只做你这一环，不要重复上游已经做完的部分。"
                ),
                "delegate",
            )
            result.steps.append(step)
            if not check.passed:
                return await self._limit(result, run, stage, check.problems)
            upstream = clip(step.answer, UPSTREAM_CHARS)
            upstream_from = member
            run.stages[stage.value]["answer"] = upstream
            await self._persist(run)

        summary = await self.team._run_member(
            entry,
            f"用户需求：{task}\n\n各阶段已通过程序验收。阶段摘要：\n"
            + "\n".join(
                f"[{name}] {str(data.get('answer') or data.get('status') or '')[:SUMMARY_CHARS]}"
                for name, data in run.stages.items()
            )
            + f"\n\n{READ_HINT}\n请给出简洁的最终交付结论，并提醒用户在生成真实媒体前进行审批。",
            role="summary",
        )
        result.steps.append(summary)
        result.final = summary.answer
        await self._prepare_generation_graph(run)
        result.status = "waiting_approval"
        run.status = "waiting_approval"
        await self._set_stage(run, ProductionStage.WAITING_APPROVAL, "waiting_approval", finished_at=time.time())
        self._emit("production_waiting_approval", run, ProductionStage.WAITING_APPROVAL)
        return result

    async def _limit(
        self, result: TeamResult, run: ProductionRun, stage: ProductionStage, problems: list[str]
    ) -> TeamResult:
        run.status = "limited"
        await self._persist(run)
        result.status = "limited"
        result.final = (
            f"制作已在「{stage.value}」阶段暂停。程序验收仍发现：\n- "
            + "\n- ".join(problems[:12])
            + "\n\n进度已经保存，可以只重试当前阶段，不需要从头制作。"
        )
        self._emit("production_run_limited", run, stage, problems=problems)
        return result
