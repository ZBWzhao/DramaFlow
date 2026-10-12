"""工作区工具插件：在沙箱内列目录、读取、写入与精确编辑文件。

对应 Toonflow 的 packages/tools/workspace/。
它存在的意义是演示"工具怎么正确地用能力包"：
  · 路径一律 ctx.resolve_path() 授权（越界/符号链接会被拦）
  · 输出要截断（一次读 5MB 会把上下文吃光，Pi 的 truncate.ts 同理）
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from core.context import ToolContext
from core.plugin import ToolResult, define_tool, tool_result

TOOL_META = {
    "name": "workspace",
    "display_name": "工作区",
    "description": "在工作区内列目录、读取、写入和编辑文本文件",
    "version": "0.2.0",
    "author": "toonflow-py-lab",
}

MAX_LINES = 400
MAX_CHARS = 20000


class ListDirArgs(BaseModel):
    path: str = Field(".", description="目录路径")


class ReadFileArgs(BaseModel):
    path: str = Field(description="文件路径")
    offset: int = Field(1, ge=1, description="从第几行开始（1 起算）")
    limit: int = Field(120, ge=1, le=MAX_LINES, description="最多读几行")


class WriteFileArgs(BaseModel):
    path: str = Field(description="工作区内的文件路径")
    content: str = Field(description="要写入的完整文本内容")
    exclusive: bool = Field(False, description="为 true 时文件已存在就报错，避免意外覆盖")


class EditFileArgs(BaseModel):
    path: str = Field(description="工作区内已有文本文件路径")
    old_text: str = Field(min_length=1, description="要替换的原文，必须能精确匹配")
    new_text: str = Field(description="替换后的文本")
    replace_all: bool = Field(False, description="是否替换全部匹配；默认要求原文只出现一次")


def _truncate(text: str) -> tuple[str, bool]:
    lines = text.splitlines()
    cut = len(lines) > MAX_LINES
    if cut:
        lines = lines[:MAX_LINES]
    body = "\n".join(f"{i:>5}  {line}" for i, line in enumerate(lines, 1))
    if len(body) > MAX_CHARS:
        body, cut = body[:MAX_CHARS] + "\n...（已按字节截断）", True
    return body, cut


def _list_dir(args: ListDirArgs, ctx: ToolContext) -> ToolResult:
    entries = ctx.files.list_dir(args.path)
    ctx.log(f"list_dir {args.path} → {len(entries)} 项")
    return tool_result("\n".join(entries) or "(空目录)", count=len(entries))


def _read_file(args: ReadFileArgs, ctx: ToolContext) -> ToolResult:
    text = ctx.files.read_text(args.path)
    lines = text.splitlines()
    start = args.offset - 1
    chunk = lines[start : start + args.limit]
    body, cut = _truncate("\n".join(chunk))
    if start + len(chunk) < len(lines):
        body += f"\n\n（共 {len(lines)} 行，本次读 {start + 1}~{start + len(chunk)} 行；"
        body += f"继续读用 offset={start + len(chunk) + 1}）"
    ctx.log(f"read_file {args.path} → {len(chunk)} 行")
    return tool_result(body, totalLines=len(lines), readCount=len(chunk), truncated=cut)


async def _write_file(args: WriteFileArgs, ctx: ToolContext) -> ToolResult:
    await ctx.files.write_text(args.path, args.content, exclusive=args.exclusive)
    ctx.log(f"write_file {args.path} → {len(args.content)} 字符")
    return tool_result(
        f"已写入 {args.path}（{len(args.content)} 字符）",
        path=args.path,
        chars=len(args.content),
        created=True,
    )


async def _edit_file(args: EditFileArgs, ctx: ToolContext) -> ToolResult:
    original = ctx.files.read_text(args.path)
    matches = original.count(args.old_text)
    if matches == 0:
        return tool_result("编辑失败：old_text 在文件中没有精确匹配", is_error=True, path=args.path)
    if matches > 1 and not args.replace_all:
        return tool_result(
            f"编辑失败：old_text 出现 {matches} 次；请提供更长的唯一原文，或设置 replace_all=true",
            is_error=True,
            path=args.path,
            matches=matches,
        )
    updated = original.replace(args.old_text, args.new_text, -1 if args.replace_all else 1)
    await ctx.files.write_text(args.path, updated)
    ctx.log(f"edit_file {args.path} → 替换 {matches if args.replace_all else 1} 处")
    return tool_result(
        f"已编辑 {args.path}（替换 {matches if args.replace_all else 1} 处）",
        path=args.path,
        replacements=matches if args.replace_all else 1,
    )


class Plugin:
    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return dict(config or {})

    def create_tools(self, context: ToolContext) -> list[Any]:
        return [
            define_tool(
                name="list_dir",
                description="列出工作区目录下的文件和子目录",
                args_model=ListDirArgs,
                execute=_list_dir,
            ),
            define_tool(
                name="read_file",
                description="读取工作区内的文本文件，可按行号范围读取",
                args_model=ReadFileArgs,
                execute=_read_file,
            ),
            define_tool(
                name="write_file",
                description="在工作区内创建或完整写入文本文件；可用 exclusive 防止覆盖已有文件",
                args_model=WriteFileArgs,
                execute=_write_file,
                side_effect=True,
                prompt_guidelines=["写入前确认目标路径；修改已有文件的一小部分优先使用 edit_file"],
            ),
            define_tool(
                name="edit_file",
                description="用精确文本匹配编辑工作区内已有文件；默认只允许唯一匹配",
                args_model=EditFileArgs,
                execute=_edit_file,
                side_effect=True,
                prompt_guidelines=["先 read_file 获取准确原文，再用 edit_file 做局部修改"],
            ),
        ]
