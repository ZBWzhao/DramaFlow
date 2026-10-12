"""自研工具插件：canvas —— 让 Agent 在画布上建图（短剧生产流程的骨架）。

对应 Toonflow 的 packages/tools/canvas（16 个工具）。这里先做 P0 需要的 6 个：
    get_canvas / add_node / update_node / connect_nodes / delete_nodes / set_text

和 Toonflow 的**关键差异**（有意为之）：
    Toonflow 的画布状态在前端，工具调用要经过"事件桥 + 回传"；
    这里画布就是工作区里的一个 JSON 文件，工具**直接读改文件**，改完在结果里带
    `canvas_changed`，前端看到就刷新。少一层桥、少一个超时故障点。

一切读写都走 ctx（沙箱 + 写锁），没有一行裸 open()。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.canvas import NODE_SPEC, Canvas, CanvasError, canvas_path, story_similarity
from core.context import ToolContext
from core.plugin import ToolResult, define_tool, tool_result

TOOL_META = {
    "name": "canvas",
    "display_name": "画布",
    "description": "在画布上创建/连接/更新节点，构成短剧生产图（剧本→分镜→资产→提示词）",
    "version": "0.1.0",
    "author": "toonflow-py-lab",
    "prompt": (
        "做短剧时先用 identify_story 识别并切换到独立剧本画布，再用 add_node 建节点、connect_nodes 连线，"
        "再按 get_canvas 返回的拓扑顺序逐个产出内容。不要一次性把正文塞进节点，正文用 set_text 落盘。"
    ),
}

DEFAULT_CANVAS = "main"


class CanvasConfig(BaseModel):
    default_canvas: str = Field(DEFAULT_CANVAS, min_length=1, max_length=64)


# ---------------------------------------------------------------------------
# 参数模型（Pydantic → JSON Schema，模型乱传会被挡住并收到可读错误）
# ---------------------------------------------------------------------------
class GetCanvasArgs(BaseModel):
    canvas: str = Field(DEFAULT_CANVAS, description="画布名（不带扩展名）")


class IdentifyStoryArgs(BaseModel):
    title: str = Field(min_length=2, max_length=100, description="本次故事的短标题，必须包含核心人物/事件")
    summary: str = Field(min_length=2, max_length=500, description="本次故事的一句话事件摘要")
    force_new: bool = Field(False, description="用户明确说新故事/另一段/无关项目时设为 true")
    continuation_canvas: str = Field("", description="用户明确要续写某画布时填写画布名")


class AddNodeArgs(BaseModel):
    canvas: str = Field(DEFAULT_CANVAS, description="画布名")
    type: Literal["text", "storyboard", "asset", "image", "video", "prompt", "image_gen", "video_gen"] = Field(description="节点类型")
    label: str = Field(min_length=1, max_length=80, description="节点显示名，如「第1场·片段2」")
    data: dict[str, Any] = Field(default_factory=dict, description="节点业务数据（见节点类型说明）")


class NodeSpec(BaseModel):
    type: Literal["text", "storyboard", "asset", "image", "video", "prompt", "image_gen", "video_gen"]
    label: str = Field(min_length=1, max_length=80)
    data: dict[str, Any] = Field(default_factory=dict)


class AddNodesArgs(BaseModel):
    canvas: str = DEFAULT_CANVAS
    nodes: list[NodeSpec] = Field(min_length=1)


class UpdateNodeArgs(BaseModel):
    canvas: str = DEFAULT_CANVAS
    node_id: str = Field(description="节点 id")
    label: str | None = None
    status: Literal["draft", "ready", "generating", "done", "error"] | None = None
    data: dict[str, Any] | None = Field(None, description="要合并的字段")
    outputs: dict[str, Any] | None = Field(None, description="端口产出")


class Connection(BaseModel):
    source: str = Field(description="上游节点 id")
    source_handle: str = Field(description="上游输出端口")
    target: str = Field(description="下游节点 id")
    target_handle: str = Field(description="下游输入端口")


class ConnectArgs(BaseModel):
    canvas: str = DEFAULT_CANVAS
    connections: list[Connection] = Field(min_length=1)


class DeleteNodesArgs(BaseModel):
    canvas: str = Field(DEFAULT_CANVAS)
    node_ids: list[str] = Field(min_length=1)


class SetTextArgs(BaseModel):
    canvas: str = Field(DEFAULT_CANVAS)
    node_id: str = Field(description="必须是 text / storyboard / prompt 这类文本节点")
    content: str = Field(min_length=1, description="正文内容（会写到 assets/<node_id>/content.md）")
    segment_no: int | None = Field(None, ge=1, description="prompt 节点对应的全局片段号；其他类型留空")


class ExportPromptArgs(BaseModel):
    canvas: str = Field(DEFAULT_CANVAS)
    path: str = Field("", description="导出路径，默认 assets/export/<画布名>-prompts.md")


class Shot(BaseModel):
    """一个镜头的 6 个必填字段（缺了 Pydantic 直接挡回，不用等到导出才发现）。

    字段描述刻意写短：工具 schema 每一轮都要进上下文，描述越长越贵。
    """

    no: int | None = None  # 镜号，留空自动编号
    segment_no: int = Field(1, ge=1, description="片段号")
    subject: str = Field(min_length=1, description="主体")
    action: str = Field(min_length=1, description="可见动作")
    shot_size: str = Field(min_length=1, description="景别：远/全/中/近/特写")
    camera_move: str = Field("固定", description="运镜：固定/推/拉/摇/移/跟")
    sound: str = Field(min_length=1, description="声音/台词")
    duration: float = Field(gt=0, le=180, description="时长秒")


class SetStoryboardArgs(BaseModel):
    canvas: str = DEFAULT_CANVAS
    node_id: str = Field(description="分镜节点 id")
    target_duration: float | None = Field(None, gt=0, description="目标总时长(秒)，会校验合计")
    shots: list[Shot] = Field(min_length=1)


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
async def _load(ctx: ToolContext, name: str) -> Canvas:
    return await ctx.require_canvas_repository().load(name)


async def _save(ctx: ToolContext, canvas: Canvas) -> None:
    await ctx.require_canvas_repository().save(canvas)


def _changed(canvas: Canvas) -> dict[str, Any]:
    """**写操作**的结果里带这个字段：前端看到就刷新画布并显示"已更新"（不需要推送通道）。"""
    return {
        "canvas_changed": canvas.name,
        "revision": canvas.revision,
        "nodes": len(canvas.nodes),
        "edges": len(canvas.edges),
    }


def _info(canvas: Canvas) -> dict[str, Any]:
    """**只读**操作：给规模信息，但**不**宣称"画布已更新"。

    原先 get_canvas 也带 canvas_changed，于是界面上每个只读调用都显示"画布已更新"——
    等于告诉用户"改了"，而其实什么也没改。这种噪音正是"看不出 AI 到底改成功没有"的原因之一。
    """
    return {"canvas": canvas.name, "revision": canvas.revision, "nodes": len(canvas.nodes), "edges": len(canvas.edges)}


def build_tools(ctx: ToolContext) -> list[Any]:
    default_canvas = str(ctx.config.get("default_canvas") or DEFAULT_CANVAS)

    async def _identify_story(args: IdentifyStoryArgs, context: ToolContext) -> ToolResult:
        """把自然语言创意确定性地路由到一张单故事画布。"""
        repository = context.require_canvas_repository()
        candidates: list[tuple[float, Canvas]] = []
        for name in await repository.list():
            canvas = await repository.load(name)
            if args.continuation_canvas and canvas.name == args.continuation_canvas:
                candidates = [(1.0, canvas)]
                break
            score = story_similarity(f"{args.title}\n{args.summary}", canvas.story_text())
            candidates.append((score, canvas))

        selected: Canvas | None = None
        score = 0.0
        if args.continuation_canvas:
            match = next((item for item in candidates if item[1].name == args.continuation_canvas), None)
            if match is None:
                return tool_result(f"找不到要续写的画布：{args.continuation_canvas}", is_error=True)
            score, selected = match
        elif candidates and not args.force_new:
            score, selected = max(candidates, key=lambda item: item[0])
            if score < 0.34:
                selected = None

        created = selected is None
        if created:
            digest = hashlib.sha1(f"{args.title}\n{args.summary}".encode("utf-8")).hexdigest()[:8]
            safe = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff-]+", "-", args.title).strip("-")[:24]
            name = f"story-{safe or digest}-{digest}"
            selected = Canvas(name, context.resolve_path(f"{name}.canvas.json"))
        selected.ensure_story(args.title, args.summary)
        selected.arrange()
        await _save(context, selected)
        action = "新建独立剧本画布" if created else "识别为已有剧本"
        return tool_result(
            f"{action}「{args.title}」→ {selected.name}（匹配度 {score:.0%}）。"
            "后续所有节点和连线只写入这张画布。",
            story_id=selected.meta.get("story_id"),
            story_title=args.title,
            match_score=score,
            created=created,
            **_changed(selected),
        )

    async def _get_canvas(args: GetCanvasArgs, context: ToolContext) -> ToolResult:
        canvas = await _load(context, args.canvas or default_canvas)
        known = await context.require_canvas_repository().list()
        body = canvas.summary()
        if len(known) > 1:
            body += f"\n\n工作区里的其他画布：{', '.join(n for n in known if n != canvas.name)}"
        if not canvas.nodes:
            specs = "；".join(f"{k}={v['label']}" for k, v in NODE_SPEC.items())
            body += f"\n\n（画布还是空的）节点类型：{specs}"
        return tool_result(body, **_info(canvas))

    async def _add_node(args: AddNodeArgs, context: ToolContext) -> ToolResult:
        canvas = await _load(context, args.canvas or default_canvas)
        try:
            node = canvas.add_node(args.type, args.label, data=dict(args.data))
        except CanvasError as exc:
            return tool_result(f"建节点失败：{exc}", is_error=True)
        await _save(context, canvas)
        ports = ", ".join(f"{k}({v})" for k, v in node.out_ports.items())
        context.log(f"画布新增 {args.type} 节点：{args.label}")
        return tool_result(
            f"已创建节点 {node.id}「{node.label}」（{node.type}），输出端口：{ports}",
            node_id=node.id,
            **_changed(canvas),
        )

    async def _add_nodes(args: AddNodesArgs, context: ToolContext) -> ToolResult:
        """批量建节点：一次工具往返建多个（成本优化：资产多的场景省 5+ 次往返）。"""
        canvas = await _load(context, args.canvas or default_canvas)
        created: list[Any] = []
        errors: list[str] = []
        for spec in args.nodes:
            try:
                created.append(canvas.add_node(spec.type, spec.label, data=dict(spec.data)))
            except CanvasError as exc:
                errors.append(f"{spec.label}: {exc}")
        await _save(context, canvas)
        lines = [f"- {n.id}｜{n.type}｜{n.label}" for n in created]
        body = f"已创建 {len(created)} 个节点：\n" + "\n".join(lines)
        if errors:
            body += "\n失败：" + "；".join(errors)
        context.log(f"画布批量新增 {len(created)} 个节点")
        return tool_result(
            body,
            node_ids=[n.id for n in created],
            created=len(created),
            is_error=bool(errors) and not created,
            **_changed(canvas),
        )

    async def _update_node(args: UpdateNodeArgs, context: ToolContext) -> ToolResult:
        canvas = await _load(context, args.canvas or default_canvas)
        try:
            node = canvas.update_node(
                args.node_id,
                label=args.label,
                status=args.status,
                data=args.data,
                outputs=args.outputs,
            )
        except CanvasError as exc:
            return tool_result(f"更新失败：{exc}", is_error=True)
        await _save(context, canvas)
        return tool_result(
            f"已更新 {node.id}「{node.label}」状态={node.status}",
            node_id=node.id,
            **_changed(canvas),
        )

    async def _connect(args: ConnectArgs, context: ToolContext) -> ToolResult:
        canvas = await _load(context, args.canvas or default_canvas)
        done: list[str] = []
        try:
            for conn in args.connections:
                edge = canvas.connect(conn.source, conn.source_handle, conn.target, conn.target_handle)
                src = canvas.nodes[conn.source]
                dst = canvas.nodes[conn.target]
                done.append(f"{src.label}.{conn.source_handle} → {dst.label}.{conn.target_handle}（{edge.id}）")
        except CanvasError as exc:
            # 部分成功也落盘：连线是幂等的，重试用不着从头来
            await _save(context, canvas)
            return tool_result(f"连线失败：{exc}\n已完成：{done or '（无）'}", is_error=True, **_changed(canvas))
        await _save(context, canvas)
        return tool_result(
            "已建立 " + str(len(done)) + " 条连线：\n" + "\n".join(f"- {d}" for d in done),
            **_changed(canvas),
        )

    async def _delete_nodes(args: DeleteNodesArgs, context: ToolContext) -> ToolResult:
        canvas = await _load(context, args.canvas or default_canvas)
        nodes, edges = canvas.remove_nodes(args.node_ids)
        await _save(context, canvas)
        return tool_result(f"已删除 {nodes} 个节点、{edges} 条相关连线", **_changed(canvas))

    async def _set_text(args: SetTextArgs, context: ToolContext) -> ToolResult:
        canvas = await _load(context, args.canvas or default_canvas)
        try:
            node = canvas.require(args.node_id)
        except CanvasError as exc:
            return tool_result(f"写正文失败：{exc}", is_error=True)
        if node.type not in ("text", "storyboard", "prompt"):
            return tool_result(f"节点「{node.label}」是 {node.type} 类型，不能写正文", is_error=True)

        relative = f"assets/{node.id}/content.md"
        await context.files.write_text(relative, args.content)  # 走沙箱 + 写锁
        node.data["content_path"] = relative
        if node.type == "prompt":
            if args.segment_no is not None:
                node.data["segment_no"] = args.segment_no
            elif not node.data.get("segment_no"):
                # 兼容先按“片段N/GNN”命名、后写正文的常见路径。
                import re

                match = re.search(r"(?:片段|G)\s*0*(\d+)", node.label, re.IGNORECASE)
                if match:
                    node.data["segment_no"] = int(match.group(1))
        node.status = "ready" if node.status == "draft" else node.status
        # 端口产出保留足够长的前端预览，全文仍以 content_path 为权威来源。
        output_port = "prompt" if node.type == "prompt" else "text"
        node.outputs[output_port] = {"data_type": "STRING", "value": args.content[:1200]}
        await _save(context, canvas)
        context.log(f"{node.label} 正文已落盘（{len(args.content)} 字）")
        return tool_result(
            f"已写入 {node.label} 的正文：{len(args.content)} 字 → {relative}（状态={node.status}）",
            node_id=node.id,
            content_path=relative,
            chars=len(args.content),
            **_changed(canvas),
        )

    async def _export_prompt(args: ExportPromptArgs, context: ToolContext) -> ToolResult:
        canvas = await _load(context, args.canvas or default_canvas)
        try:
            markdown = canvas.render_all()  # 展开 {{ref N}} → 【参考N】+ 参考声明
        except CanvasError as exc:
            return tool_result(f"导出失败：{exc}", is_error=True)
        out_path = args.path or f"assets/export/{canvas.name}-prompts.md"
        await context.files.write_text(out_path, markdown)  # 走沙箱 + 写锁
        segments = len([n for n in canvas.nodes.values() if n.type == "prompt"])
        context.log(f"导出 {segments} 个片段的提示词 → {out_path}")
        return tool_result(
            f"已导出 {segments} 个片段到 {out_path}（{len(markdown)} 字）\n"
            "前 500 字预览：\n" + markdown[:500],
            export_path=out_path,
            segments=segments,
        )

    async def _set_storyboard(args: SetStoryboardArgs, context: ToolContext) -> ToolResult:
        canvas = await _load(context, args.canvas or default_canvas)
        try:
            node = canvas.require(args.node_id)
        except CanvasError as exc:
            return tool_result(f"写分镜失败：{exc}", is_error=True)
        if node.type != "storyboard":
            return tool_result(f"「{node.label}」是 {node.type} 类型，不是分镜节点", is_error=True)

        shots: list[dict[str, Any]] = []
        for index, shot in enumerate(args.shots, 1):
            item = shot.model_dump()
            item["no"] = item.get("no") or index
            shots.append(item)

        total = canvas.total_duration(shots)
        segments = canvas.segment_durations(shots)
        node.data["shots"] = shots
        node.data["total_duration"] = total
        node.data["segment_durations"] = segments
        if args.target_duration:
            node.data["target_duration"] = args.target_duration
        node.status = "ready"

        relative = f"assets/{node.id}/content.md"
        text = canvas.render_storyboard(node.id)  # 时间范围由时长累加算出，不靠模型手写
        await context.files.write_text(relative, text)
        node.data["content_path"] = relative
        node.outputs["shots"] = {"data_type": "STRING", "value": text[:1200]}
        await _save(context, canvas)

        warn = ""
        if args.target_duration:
            diff = round(total - float(args.target_duration), 2)
            warn = (
                f"\n⚠️ 与目标时长相差 {diff}s，请调整镜头时长使其相等"
                if abs(diff) > 0.01
                else "\n✓ 时长合计与目标一致"
            )
        by_seg = "、".join(f"片段{k} {v}s" for k, v in segments.items())
        context.log(f"分镜写入 {len(shots)} 镜，共 {total}s（{by_seg}）")
        return tool_result(
            f"已写入分镜「{node.label}」：{len(shots)} 个镜头、{len(segments)} 个片段，合计 {total}s"
            f"（{by_seg}）{warn}",
            node_id=node.id,
            total_duration=total,
            segments=segments,
            **_changed(canvas),
        )

    return [
        define_tool(
            name="identify_story",
            description="识别本次创意属于已有剧本还是新剧本，并切换/创建对应的单故事画布",
            args_model=IdentifyStoryArgs,
            execute=_identify_story,
            side_effect=True,
            prompt_guidelines=[
                "每个新的短剧/视频创意必须先调用 identify_story，再调用 get_canvas",
                "用户明确说『新故事/另一段/完全无关』时 force_new=true；只有明确续写才传 continuation_canvas",
                "identify_story 返回的 canvas_changed 是本次唯一工作画布，禁止继续向旧画布添加节点",
            ],
        ),
        define_tool(
            name="get_canvas",
            description="读画布当前结构与可执行顺序（建图前先看一眼，避免重复建节点）",
            args_model=GetCanvasArgs,
            execute=_get_canvas,
            prompt_guidelines=["每次开工先 get_canvas，不要凭记忆改图"],
        ),
        define_tool(
            name="add_node",
            description="在画布上新建节点：text / storyboard / asset / image / video / prompt / image_gen / video_gen",
            args_model=AddNodeArgs,
            execute=_add_node,
            side_effect=True,
        ),
        define_tool(
            name="add_nodes",
            description="批量创建多个节点（建资产/提示词这类成组的节点时用它，比一个个建省往返）",
            args_model=AddNodesArgs,
            execute=_add_nodes,
            side_effect=True,
            prompt_guidelines=["要建 2 个以上同类节点时用 add_nodes 一次建完"],
        ),
        define_tool(
            name="update_node",
            description="更新节点：改标签、改状态、合并业务数据、写端口产出",
            args_model=UpdateNodeArgs,
            execute=_update_node,
            side_effect=True,
        ),
        define_tool(
            name="connect_nodes",
            description="连接节点（上游输出端口 → 下游输入端口），类型不匹配会被拒绝",
            args_model=ConnectArgs,
            execute=_connect,
            side_effect=True,
            prompt_guidelines=["连线前先确认端口名：asset 输出 ref(IMAGE)/spec(STRING)，prompt 输入 ref(IMAGE)/ctx(STRING)"],
        ),
        define_tool(
            name="delete_nodes",
            description="删除节点（连带它的连线）",
            args_model=DeleteNodesArgs,
            execute=_delete_nodes,
            side_effect=True,
        ),
        define_tool(
            name="set_storyboard",
            description=(
                "把分镜以**结构化**方式写入（每个镜头 6 字段：主体/动作/景别/运动/声音/时长）；"
                "时间范围与总时长由系统算出，填了 target_duration 就会校验时长预算"
            ),
            args_model=SetStoryboardArgs,
            execute=_set_storyboard,
            side_effect=True,
            prompt_guidelines=[
                "分镜一律用 set_storyboard 写（不要用 set_text 手写分镜表，那样时长无法校验）",
                "每镜必须能拍到：主体、动作、景别、运动、声音、时长缺一不可",
            ],
        ),
        define_tool(
            name="set_text",
            description="把正文写进文本类节点（剧本/提示词），落盘到 assets/<节点id>/content.md",
            args_model=SetTextArgs,
            execute=_set_text,
            side_effect=True,
            prompt_guidelines=[
                "正文一律用 set_text 落盘，不要把长文塞进节点的 data 字段",
                "给 prompt 写正文时必须传 segment_no；正式视频提示词必须保存完整内容，不能只写镜头摘要",
            ],
        ),
        define_tool(
            name="export_prompt",
            description="把画布上所有片段提示词导出成一份 Markdown（{{ref N}} 会自动展开成参考声明），并落盘",
            args_model=ExportPromptArgs,
            execute=_export_prompt,
            side_effect=True,
            prompt_guidelines=["所有片段写完后再导出；导出内容里若有『未展开的编号』告警，说明资产没连上，要回去补连线"],
        ),
    ]


class Plugin:
    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return CanvasConfig.model_validate(config or {}).model_dump()

    def create_tools(self, context: ToolContext) -> list[Any]:
        return build_tools(context)
