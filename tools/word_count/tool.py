"""自研工具插件：word_count —— 统计工作区内文件的行数/字符数/高频词。

对应 Toonflow 里的 packages/tools/wordCount/（冲刺计划里 Day1 的硬验收物）。
它演示一个工具插件的最小结构：
    TOOL_META  —— 元数据（装载器靠它发现工具、校验、注入提示词）
    Plugin     —— 契约实现（validate_config + create_tools）
    define_tool —— 参数用 Pydantic 声明，schema 自动生成

★ 注意：它全程只用 ctx.files，**没有一行 open()** —— 这就是 ToolContext 的意义。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from pydantic import BaseModel, Field

from core.context import ToolContext
from core.plugin import ToolResult, define_tool, tool_result

TOOL_META = {
    "name": "word_count",
    "display_name": "词频统计",
    "description": "统计工作区内文件的行数、字符数与高频词",
    "version": "0.1.0",
    "author": "toonflow-py-lab",
    "prompt": "需要了解某个文件的规模或内容概要时用 word_count，不要用猜的。",
}


class WordCountArgs(BaseModel):
    path: str = Field(".", description="文件或目录路径（必须在工作区内）")
    pattern: str = Field("*", description="文件过滤，如 *.py / *.md")
    top: int = Field(5, ge=1, le=30, description="返回高频词数量")


class WordCountConfig(BaseModel):
    max_files: int = Field(200, ge=1, le=5000)


async def _execute(args: WordCountArgs, ctx: ToolContext) -> ToolResult:
    max_files = int(ctx.config.get("max_files", 200))
    target = ctx.resolve_path(args.path)

    files = [target] if target.is_file() else ctx.files.iter_files(args.path, args.pattern)
    if not files:
        return tool_result(f"没有匹配到文件：{args.path}（pattern={args.pattern}）")
    files = files[:max_files]

    lines = chars = 0
    counter: Counter[str] = Counter()
    for file in files:
        text = file.read_text(encoding="utf-8", errors="replace")
        lines += text.count("\n") + 1
        chars += len(text)
        counter.update(word.lower() for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))

    ctx.log(f"word_count 扫描 {len(files)} 个文件")
    top_words = ", ".join(f"{w}×{n}" for w, n in counter.most_common(args.top)) or "（无可统计的词）"
    body = (
        f"扫描 {len(files)} 个文件（{args.path}，pattern={args.pattern}）\n"
        f"行数 {lines}｜字符数 {chars}｜不同词 {len(counter)}\n"
        f"高频词 top{args.top}：{top_words}"
    )
    return tool_result(body, files=len(files), lines=lines, chars=chars)


class Plugin:
    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """配置校验：错了在装载期就炸，不要等到模型调用时。"""
        parsed = WordCountConfig.model_validate(config or {})
        return parsed.model_dump()

    def create_tools(self, context: ToolContext) -> list[Any]:
        return [
            define_tool(
                name="word_count",
                description="统计工作区内文件的行数、字符数和高频词（用于快速了解代码/文档规模）",
                args_model=WordCountArgs,
                execute=_execute,
                prompt_guidelines=["不要对大目录用 pattern=*，先缩小范围"],
            )
        ]
