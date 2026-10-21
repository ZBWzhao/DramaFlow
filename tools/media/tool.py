"""自研工具插件：media —— 图片/视频生成（Mock + 可配置真实 Provider）。

对照 Toonflow：packages/tools/mediaGeneration（generateImage / generateVideo / listMediaModels）。

三个关键点：
  1. 工具**通过 ToolContext 的能力位**拿 provider（`ctx.require_media()`），
     不 import 任何具体 provider —— 换 provider 不用改工具代码。
  2. 生成结果**写进画布节点的 outputs**，前端据此显示缩略图（链路闭环）。
  3. 产物一律落 `assets/generated/`，与 Toonflow 的默认输出目录一致。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.canvas import Canvas, CanvasError, build_media_request
from core.canvas_repository import CanvasConflictError, mutate_canvas_with_retry
from core.context import CapabilityUnavailable, ToolContext
from core.plugin import ToolResult, define_tool, tool_result

TOOL_META = {
    "name": "media",
    "display_name": "媒体生成",
    "description": "审批后通过已配置 Provider 生成图片/视频，结果写入画布节点 outputs",
    "version": "0.2.0",
    "author": "toonflow-py-lab",
    "prompt": "生成图片前先确认提示词已就绪；生成结果会写进节点，不需要把路径抄进对话里。",
}

DEFAULT_CANVAS = "main"


class MediaConfig(BaseModel):
    default_canvas: str = DEFAULT_CANVAS
    default_model: str = "mock-image-v1"


class ListModelsArgs(BaseModel):
    kind: Literal["image", "video", "audio", ""] = Field("", description="留空看全部")


class RunCanvasArgs(BaseModel):
    canvas: str = DEFAULT_CANVAS
    only: list[str] = Field(default_factory=list, description="只跑这些节点，留空全跑")
    model: str = ""
    size: str = Field("", description="如 1024x576")


class GenerateArgs(BaseModel):
    prompt: str = Field(min_length=1, description="提示词")
    canvas: str = DEFAULT_CANVAS
    node_id: str = Field("", description="写入结果的节点 id")
    kind: Literal["image", "video"] = "image"
    model: str = ""
    size: str = Field("", description="如 1024x576")
    duration: int = Field(5, ge=1, le=60, description="视频秒数")


def _changed(canvas: Canvas) -> dict[str, Any]:
    """结果里带这个字段：前端看到就刷新画布。"""
    return {
        "canvas_changed": canvas.name,
        "revision": canvas.revision,
        "nodes": len(canvas.nodes),
        "edges": len(canvas.edges),
    }


def _pick_model(media: Any, kind: str, requested: str = "") -> tuple[str, str]:
    """返回 ``(model, provider)``：调用方指定优先，否则取模型表里同类第一个。

    Agent 侧没有审批载荷，也就没有"用户选的 provider/model"；这里退回
    「当前 Provider 的第一个同类模型」，而不是硬编码 ``mock-video-v1``。
    """
    if requested:
        return requested, ""
    try:
        models = media.list_models()
    except Exception:  # noqa: BLE001
        return "", ""
    kind_models = [item for item in models if item.get("kind") == kind and item.get("id")]
    # 优先当前激活的 Provider：能力适配器持有 registry（或替身 provider）时能看到 active。
    active = str(getattr(getattr(media, "registry", None), "active", "") or "")
    if active:
        for item in kind_models:
            if str(item.get("provider") or "") == active:
                return str(item["id"]), active
    if kind_models:
        item = kind_models[0]
        return str(item["id"]), str(item.get("provider") or "")
    return "", ""


async def _mark_node(repository: Any, canvas_name: str, node_id: str, status: str) -> None:
    """把节点状态写成 generating/error：标记是尽力而为，不能盖掉主流程的结论。"""
    def mutate(board: Canvas) -> bool:
        board.update_node(node_id, status=status)
        return True

    try:
        await mutate_canvas_with_retry(repository, canvas_name, mutate)
    except (CanvasError, CanvasConflictError):
        pass


def build_tools(ctx: ToolContext) -> list[Any]:
    default_canvas = str(ctx.config.get("default_canvas") or DEFAULT_CANVAS)

    async def _list_models(args: ListModelsArgs, context: ToolContext) -> ToolResult:
        try:
            media = context.require_media()
        except CapabilityUnavailable as exc:
            return tool_result(f"媒体能力不可用：{exc}", is_error=True)
        models = media.list_models()
        if args.kind:
            models = [m for m in models if m.get("kind") == args.kind]
        lines = ["可用媒体模型："]
        lines += [f"- [{m.get('provider', 'unknown')}] {m['id']}（{m['kind']}）{m.get('label', '')}" for m in models]
        if models and all(m.get("mock") for m in models):
            lines.append("\n当前只有 Mock：产物是用于验证链路的占位图，不是真实 AI 媒体。")
        else:
            lines.append("\n真实 Provider 只会在前端人工审批后执行；Agent 不能自行扣费生成。")
        return tool_result("\n".join(lines), models=models)

    async def _generate(args: GenerateArgs, context: ToolContext) -> ToolResult:
        if not context.media_approved:
            return tool_result(
                "等待用户审批：媒体生成会调用外部 Provider，必须由用户在生成审批卡中确认后执行。",
                is_error=True,
                approval_required=True,
            )
        try:
            media = context.require_media()
        except CapabilityUnavailable as exc:
            return tool_result(f"媒体能力不可用：{exc}", is_error=True)

        repository = context.require_canvas_repository()
        canvas_name = args.canvas or default_canvas
        model, provider = _pick_model(media, args.kind, args.model)

        # 若指定了画布节点：参数优先取节点自己的口径（与 Worker 同一套
        # generation_params），否则节点设置会被 Agent 的默认值悄悄覆盖。
        params: dict[str, Any] = {}
        if args.node_id:
            canvas = await repository.load(canvas_name)
            try:
                canvas.require(args.node_id)
            except CanvasError as exc:
                return tool_result(f"生成失败：{exc}", is_error=True)
            params = canvas.generation_params(canvas.require(args.node_id))
            await _mark_node(repository, canvas_name, args.node_id, "generating")

        request: dict[str, Any] = {
            "prompt": args.prompt,
            "model": model,
            "size": args.size or params.get("size") or ("1024x576" if args.kind == "image" else "1280x720"),
            "generate_audio": False,
            "references": [],
        }
        if provider:
            request["provider"] = provider
        for key in ("ratio", "resolution"):
            if params.get(key):
                request[key] = params[key]
        if args.kind == "video":
            request["duration"] = params.get("duration") or args.duration

        try:
            result = await asyncio.to_thread(media.generate, args.kind, request)
        except Exception as exc:  # noqa: BLE001
            if args.node_id:
                await _mark_node(repository, canvas_name, args.node_id, "error")
            return tool_result(f"生成失败：{type(exc).__name__}: {exc}", is_error=True)

        if args.node_id:
            take_id = f"take_{uuid.uuid4().hex[:12]}"

            def apply_result(board: Canvas) -> bool:
                board.record_generated_media(
                    args.node_id,
                    kind=args.kind,
                    path=result.path,
                    mime=result.mime,
                    provider=result.provider or provider,
                    mock=result.mock,
                    model=request["model"],
                    duration_s=float(request.get("duration") or 0) or None,
                    take_id=take_id,
                    mode="canvas",
                )
                return True

            await mutate_canvas_with_retry(repository, canvas_name, apply_result)
            context.log(f"{args.node_id} 生成完成 → {result.path}")

        head = "占位图" if result.mock else "成图"
        return tool_result(
            f"已生成{head}：{result.path}（{result.meta.get('size')}，provider={result.provider}）"
            + ("\n⚠️ mock provider：文件是真的 PNG，内容是占位图案，接真 provider 后即产出真实画面。"
               if result.mock else ""),
            media_path=result.path,
            mock=result.mock,
            node_id=args.node_id or None,
            canvas_changed=canvas_name if args.node_id else None,
        )

    async def _run_canvas(args: RunCanvasArgs, context: ToolContext) -> ToolResult:
        """按拓扑序把画布上所有生成节点跑一遍（这就是"一键生成"）。"""
        if not context.media_approved:
            return tool_result(
                "等待用户审批：已准备好生成节点，但不能由 Agent 自行产生费用。请让用户在前端审批后运行。",
                is_error=True,
                approval_required=True,
            )
        try:
            media = context.require_media()
        except CapabilityUnavailable as exc:
            return tool_result(f"媒体能力不可用：{exc}", is_error=True)

        repository = context.require_canvas_repository()
        canvas_name = args.canvas or default_canvas
        canvas = await repository.load(canvas_name)
        jobs = canvas.generation_jobs()
        if args.only:
            only = set(args.only)
            jobs = [job for job in jobs if job["node_id"] in only]
        if not jobs:
            return tool_result(
                "没有可执行的生成节点。先建 image_gen / video_gen 节点，"
                "再在卡片里填提示词，或把提示词节点的 prompt 端口连到它的 prompt 端口。",
                is_error=True,
            )

        lines: list[str] = []
        done = 0
        for job in jobs:
            if not job["ready"]:
                reason = job.get("ready_reason") or "没有可用提示词"
                lines.append(f"⊘ {job['label']}：{reason}，跳过")
                continue
            # 参数与请求形状都走共享构造器：节点设置（画幅/时长/画质）与 Worker 完全同源。
            model, provider = _pick_model(media, job["kind"], args.model)
            request = build_media_request(job, provider=provider, model=model)
            if args.size:
                request["size"] = args.size  # 显式参数优先，兼容老调用方
            await _mark_node(repository, canvas_name, job["node_id"], "generating")
            try:
                result = await asyncio.to_thread(media.generate, job["kind"], request)
            except Exception as exc:  # noqa: BLE001
                await _mark_node(repository, canvas_name, job["node_id"], "error")
                lines.append(f"✗ {job['label']}：{type(exc).__name__}: {exc}")
                continue
            take_id = f"take_{uuid.uuid4().hex[:12]}"

            def apply_result(board: Canvas) -> bool:
                board.record_generated_media(
                    job["node_id"],
                    kind=job["kind"],
                    path=result.path,
                    mime=result.mime,
                    provider=result.provider or provider,
                    mock=result.mock,
                    model=request["model"],
                    duration_s=float(request.get("duration") or 0) or None,
                    take_id=take_id,
                    mode="canvas",
                )
                return True

            try:
                await mutate_canvas_with_retry(repository, canvas_name, apply_result)
            except CanvasError as exc:
                lines.append(f"✗ {job['label']}：写回画布失败（{exc}）")
                continue
            done += 1
            lines.append(f"✓ {job['label']}（{job['kind']}，提示词 {job['prompt_chars']} 字）→ {result.path}")

        canvas = await repository.load(canvas_name)  # 重读最新版：并发写者可能改过节点
        context.log(f"画布生成：{done}/{len(jobs)} 个节点完成")
        return tool_result(
            f"按拓扑序执行 {len(jobs)} 个生成节点，成功 {done} 个：\n" + "\n".join(lines),
            generated=done,
            total=len(jobs),
            **_changed(canvas),
        )

    return [
        define_tool(
            name="run_canvas",
            description="执行画布生成节点；有外部成本，只有宿主拿到用户本次明确审批后才允许调用",
            args_model=RunCanvasArgs,
            execute=_run_canvas,
            side_effect=True,
            prompt_guidelines=[
                "只在提示词节点都写好之后再 run_canvas；跳过某个节点不会让它后面的节点提前跑（拓扑序保证）",
                "禁止自行生成；先展示模型、片段数和参数，由用户在审批卡中确认",
            ],
        ),
        define_tool(
            name="list_media_models",
            description="列出可用的图片/视频生成模型（含 provider 说明）",
            args_model=ListModelsArgs,
            execute=_list_models,
            prompt_guidelines=["不清楚能生成什么时先问 list_media_models，不要猜模型名"],
        ),
        define_tool(
            name="generate_media",
            description="审批后按提示词生成图片或视频；未获得宿主的本次审批时会拒绝",
            args_model=GenerateArgs,
            execute=_generate,
            side_effect=True,
            prompt_guidelines=[
                "提示词要来自已经写好的 prompt 节点（先 get_canvas 读它的正文）",
                "一次只生成一个片段，失败要说明原因而不是反复重试",
            ],
        ),
    ]


class Plugin:
    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return MediaConfig.model_validate(config or {}).model_dump()

    def create_tools(self, context: ToolContext) -> list[Any]:
        return build_tools(context)
