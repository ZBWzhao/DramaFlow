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
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.canvas import Canvas, CanvasError
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

        request: dict[str, Any] = {"prompt": args.prompt}
        if args.model:
            request["model"] = args.model
        if args.size:
            request["size"] = args.size
        if args.kind == "video":
            request["duration"] = args.duration

        # 若指定了画布节点，先把状态标成 generating（前端会显示"生成中"）
        canvas = None
        node = None
        if args.node_id:
            repository = context.require_canvas_repository()
            canvas = await repository.load(args.canvas or default_canvas)
            try:
                node = canvas.update_node(args.node_id, status="generating")
            except CanvasError as exc:
                return tool_result(f"生成失败：{exc}", is_error=True)
            await repository.save(canvas)

        try:
            result = await asyncio.to_thread(media.generate, args.kind, request)
        except Exception as exc:  # noqa: BLE001
            if canvas is not None and node is not None:
                canvas.update_node(node.id, status="error")
                await repository.save(canvas)
            return tool_result(f"生成失败：{type(exc).__name__}: {exc}", is_error=True)

        # 结果写进节点 outputs：前端据此显示缩略图，下游节点也能继续引用
        if canvas is not None and node is not None:
            port = "image" if args.kind == "image" else "video"
            node.outputs[port] = {
                "data_type": port.upper(),
                "value": {"path": result.path, "mime": result.mime, "mock": result.mock},
            }
            node.status = "done"
            await repository.save(canvas)
            context.log(f"{node.label} 生成完成 → {result.path}")

        head = "占位图" if result.mock else "成图"
        return tool_result(
            f"已生成{head}：{result.path}（{result.meta.get('size')}，provider={result.provider}）"
            + ("\n⚠️ mock provider：文件是真的 PNG，内容是占位图案，接真 provider 后即产出真实画面。"
               if result.mock else ""),
            media_path=result.path,
            mock=result.mock,
            node_id=args.node_id or None,
            canvas_changed=(args.canvas or default_canvas) if args.node_id else None,
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
        canvas = await repository.load(args.canvas or default_canvas)
        jobs = canvas.generation_jobs()
        if args.only:
            jobs = [job for job in jobs if job["node_id"] in set(args.only)]
        if not jobs:
            return tool_result(
                "没有可执行的生成节点。先建 image_gen / video_gen 节点，"
                "并把 prompt 节点的 prompt 端口连到它的 prompt 端口。",
                is_error=True,
            )

        lines: list[str] = []
        done = 0
        for job in jobs:
            if not job["ready"]:
                lines.append(f"⊘ {job['label']}：没有输入提示词，跳过（检查 prompt 端口连线）")
                continue
            canvas.update_node(job["node_id"], status="generating")
            await repository.save(canvas)
            try:
                result = await asyncio.to_thread(
                    media.generate,
                    job["kind"], {
                        "prompt": job["prompt"],
                        "model": args.model or f"mock-{job['kind']}-v1",
                        "size": args.size or ("1024x576" if job["kind"] == "image" else "1280x720"),
                        "references": [
                            {"path": ref["path"], "type": ref["type"], "mime_type": ref.get("mime_type", "")}
                            for ref in job.get("refs", []) if ref.get("path")
                        ],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                canvas.update_node(job["node_id"], status="error")
                await repository.save(canvas)
                lines.append(f"✗ {job['label']}：{type(exc).__name__}: {exc}")
                continue
            port = "image" if job["kind"] == "image" else "video"
            node = canvas.require(job["node_id"])
            node.outputs[port] = {
                "data_type": port.upper(),
                "value": {"path": result.path, "mime": result.mime, "mock": result.mock},
            }
            canvas.update_node(job["node_id"], status="done")
            await repository.save(canvas)
            done += 1
            lines.append(f"✓ {job['label']}（{job['kind']}，提示词 {job['prompt_chars']} 字）→ {result.path}")

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
