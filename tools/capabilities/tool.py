"""报告当前会话的真实能力，包括 Context 能力位和独立工具。

Python 版画布由 get_canvas/add_node 等独立工具提供，不依赖 ToolContext.canvas；
媒体也可能只是 mock provider。自检必须把“可调用”“Mock”“真实生成”分开，
否则模型会把局部缺失误判成整个创作任务不可执行。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from core.context import CapabilityUnavailable, ToolContext
from core.plugin import ToolResult, define_tool, tool_result

TOOL_META = {
    "name": "capabilities",
    "display_name": "能力自检",
    "description": "查询当前 Agent 的 ToolContext 接入了哪些能力（文件/日志/媒体/反问/画布）",
    "version": "0.1.0",
    "author": "toonflow-py-lab",
    "prompt": "不确定能不能做某件事（尤其是生成图片/视频、向用户反问）时，先调 capabilities 查，不要假设。",
}

CANVAS_TOOLS = {
    "get_canvas", "add_node", "add_nodes", "update_node", "connect_nodes",
    "delete_nodes", "set_storyboard", "set_text", "export_prompt", "run_canvas",
}
FILE_WRITE_TOOLS = {"write_file", "edit_file"}


class CapabilitiesArgs(BaseModel):
    probe: str | None = Field(
        None,
        description="可选：要试跑的能力名（media/question/canvas）。给了就真的调用一次，验证接入是否可用",
    )


class CapabilitiesConfig(BaseModel):
    expose_notes: bool = Field(True, description="是否附带每项能力的说明")


class Plugin:
    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return CapabilitiesConfig.model_validate(config or {}).model_dump()

    def create_tools(self, context: ToolContext) -> list[Any]:
        expose_notes = bool(context.config.get("expose_notes", True))

        async def _execute(args: CapabilitiesArgs, ctx: ToolContext) -> ToolResult:
            native = ctx.capabilities()
            canvas_tools = sorted(ctx.tool_names & CANVAS_TOOLS)
            write_tools = sorted(ctx.tool_names & FILE_WRITE_TOOLS)

            provider = None
            media_mode = "unavailable"
            media_models: list[dict[str, Any]] = []
            if ctx.media is not None:
                registry = getattr(ctx.media, "registry", None)
                provider_obj = getattr(registry, "provider", None)
                provider = getattr(provider_obj, "name", None) or type(ctx.media).__name__
                try:
                    media_models = ctx.media.list_models()
                except Exception:  # 自检不应因为 provider 列表失败而中断
                    media_models = []
                real_providers = sorted({
                    str(model.get("provider")) for model in media_models
                    if model.get("provider") and not model.get("mock")
                })
                if real_providers:
                    media_mode = "real"
                    provider = ", ".join(real_providers)
                else:
                    media_mode = "mock"

            available = {
                "files": native["files"],
                "file_write": bool(write_tools),
                "log": native["log"],
                "media": native["media"],
                "question": native["question"],
                "canvas": native["canvas"] or bool(canvas_tools),
            }
            notes = {
                "files": "工作区文件读取受路径沙箱保护",
                "file_write": (
                    f"可用写入工具：{', '.join(write_tools)}" if write_tools else "当前 Agent 未获写入工具"
                ),
                "log": "结构化运行日志",
                "media": (
                    "仅 Mock Provider：可验证完整链路；图片是占位图，视频是可播放的占位 MP4，但都不是真实模型作品"
                    if media_mode == "mock"
                    else f"真实媒体 Provider 已配置：{provider}；执行前仍需用户审批" if media_mode == "real"
                    else "没有注入媒体 Provider"
                ),
                "question": (
                    "已接结构化提问通道" if native["question"]
                    else "未接结构化 askUser；仍可在普通回复中向用户提问"
                ),
                "canvas": (
                    f"通过独立画布工具可用：{', '.join(canvas_tools)}" if canvas_tools
                    else "没有画布桥接或画布工具"
                ),
            }
            lines = ["当前会话实际能力："]
            for name, ok in available.items():
                note = f" —— {notes[name]}" if expose_notes else ""
                lines.append(f"- {name}: {'可用' if ok else '不可用'}{note}")

            probed: str | None = None
            if args.probe:
                # 真的调用一次：能力位是"声明"，能不能用要看 require_* 会不会抛
                try:
                    if args.probe == "canvas" and canvas_tools:
                        probed = "canvas: 可用（独立画布工具）"
                    else:
                        getattr(ctx, f"require_{args.probe}")()
                        probed = f"{args.probe}: 可用"
                except CapabilityUnavailable as exc:
                    probed = f"{args.probe}: 不可用 —— {exc}"
                except AttributeError:
                    probed = f"{args.probe}: 未知能力（可选值：media / question / canvas）"
                lines.append(f"\n试跑结果：{probed}")

            ctx.log("capabilities 自检：已接入 " + ", ".join(k for k, v in available.items() if v))
            return tool_result(
                "\n".join(lines),
                capabilities=available,
                native_context=native,
                provider=provider,
                media_mode=media_mode,
                media_models=media_models,
                canvas_tools=canvas_tools,
                write_tools=write_tools,
                probe=args.probe,
                probe_result=probed,
            )

        return [
            define_tool(
                name="capabilities",
                description="查询当前 Agent 的实际能力，并区分 Mock/真实媒体、Context 能力位与独立画布工具",
                args_model=CapabilitiesArgs,
                execute=_execute,
                prompt_guidelines=[
                    "被要求生成图片/视频前先确认 media_mode；mock 不能被描述成真实媒体",
                    "某个最终能力缺失时，仍应完成剧本、分镜、提示词等不依赖该能力的阶段",
                ],
            )
        ]
