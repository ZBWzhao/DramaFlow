"""上下文压缩。

对照 Toonflow：runtime/index.ts 的 streamFunction + Pi 的 core/compaction/
  · 时机：**每次请求之前**检查（和 Pi 的位置一致），不是等超窗口才救火
  · 做法：把较早的历史换成一段摘要，保留最近若干条
  · Toonflow 的接管点：**空摘要拒收** —— 模型返回空就当没压缩，否则上下文会被抹掉

另外这里补了 Pi 没强调的一点：**切点不能落在 tool 结果中间**，
否则历史里会出现"没有对应 tool_calls 的孤儿 tool 消息"，很多 provider 会直接报错。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from typing import Any

from .llm import AssistantMessage, LLM
from .session import Session

# DeepSeek 当前上下文足以容纳 workflow（约 10 万字符）和一轮完整制片工具轨迹。
# 旧值 12000 会在读完技能后立刻把技能压成摘要，随后每几次工具调用又压一次，
# 直接丢失创作约束。这里保留约 20 万字符再压缩，并多留最近的工具配对。
THRESHOLD_CHARS = int(os.environ.get("COMPACT_THRESHOLD_CHARS", "200000"))
KEEP_RECENT = int(os.environ.get("COMPACT_KEEP_RECENT", "12"))
TRIGGER_RATIO = float(os.environ.get("COMPACT_TRIGGER_RATIO", "0.75"))
FALLBACK_SUMMARY_CHARS = int(os.environ.get("COMPACT_FALLBACK_SUMMARY_CHARS", "18000"))

SUMMARY_PROMPT = (
    "请把下面这段对话压缩成可继续执行的中文制作状态，保留：用户原始剧情事实和禁止改动、"
    "已确认的模型/时长/画幅/风格、当前 workflow 阶段、已读取技能及仍需遵循的关键规则、"
    "完整对白、节点/文件路径、工具返回的 ID 和关键数字、未完成事项。"
    "不要把创作提案写成用户已确认事实，不要遗漏路径、ID、时间轴和版本状态。\n\n"
)


def _is_orphan(message: dict) -> bool:
    """保留段的开头不能是这两种消息，否则会变成"没有前文的孤儿"：
      · role=tool —— 它的 tool_calls 在前面，被压掉后 provider 直接报 400
      · assistant 且带 tool_calls —— 它对应的 tool 结果被压掉了
    """
    return message.get("role") == "tool" or bool(message.get("tool_calls"))


def safe_cut_point(messages: list[dict], keep_recent: int) -> int:
    """向后推到第一个"能独立成立"的消息为止（孤儿一律归入被压缩的一侧）。"""
    cut = max(0, len(messages) - keep_recent)
    while cut < len(messages) and _is_orphan(messages[cut]):
        cut += 1
    return cut


def estimate_tokens(value: Any) -> int:
    """保守估算上下文 token；中文按约 1 token/字，拉丁文本按约 4 字符/token。"""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    cjk = len(re.findall(r"[\u3400-\u9fff\uf900-\ufaff]", text))
    other = len(text) - cjk
    return max(1, cjk + math.ceil(other / 4))


def estimate_context_tokens(session: Session, tools: list[dict[str, Any]] | None = None) -> int:
    """估算真正送给模型的消息与工具 schema，而不是只看消息字符数。"""
    return estimate_tokens(session.request_messages()) + estimate_tokens(tools or [])


def _fallback_summary(messages: list[dict]) -> str:
    """摘要模型失败时的确定性降级，优先保住用户事实、工具结果和最近进展。"""
    rows: list[str] = ["[自动降级摘要：模型摘要失败，以下为原始历史关键摘录，不代表新增用户授权]"]
    for message in messages:
        role = str(message.get("role") or "unknown")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            if message.get("tool_calls"):
                content = json.dumps(message["tool_calls"], ensure_ascii=False)
            else:
                continue
        excerpt = content.strip()
        if len(excerpt) > 1800:
            excerpt = excerpt[:1400] + "\n…[中间内容已裁剪]…\n" + excerpt[-300:]
        rows.append(f"\n[{role}]\n{excerpt}")
    text = "".join(rows)
    source_chars = sum(len(str(message.get("content") or "")) for message in messages)
    # 降级摘要必须真的比源历史小；否则会陷入“压缩后反而更大”的循环。
    limit = min(FALLBACK_SUMMARY_CHARS, max(2000, source_chars // 3))
    if len(text) <= limit:
        return text
    # 同时保留开头的原始目标和末尾的最近状态。
    head = limit // 3
    tail = limit - head
    return text[:head] + "\n…[较早过程已裁剪，完整记录仍在 JSONL]…\n" + text[-tail:]


async def maybe_compact(
    session: Session,
    llm: LLM,
    bus: Any | None = None,
    threshold: int | None = None,
    keep_recent: int | None = None,
    context_window: int | None = None,
    max_output_tokens: int = 8192,
    token_limit: int | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> str | None:
    """接近模型窗口就压缩；threshold 仅保留给旧 demo 的字符阈值测试。"""
    keep_recent = keep_recent if keep_recent is not None else KEEP_RECENT
    before_tokens = estimate_context_tokens(session, tools)
    if threshold is not None:
        should_compact = session.size_chars() >= threshold
    else:
        window = max(16000, context_window or 128000)
        reserve = max_output_tokens + 4096
        trigger = token_limit or min(int(window * TRIGGER_RATIO), max(1, window - reserve))
        should_compact = before_tokens >= trigger
    if not should_compact:
        return None

    cut = safe_cut_point(session.messages, keep_recent)
    old = session.messages[:cut]
    if not old:
        return None
    recent = session.messages[cut:]

    if bus is not None:
        bus.emit(
            "compaction_start",
            session_id=session.id,
            chars=session.size_chars(),
            tokens=before_tokens,
            compacted=len(old),
        )

    failure_reason = ""
    try:
        message = await llm.chat(
            [{"role": "user", "content": SUMMARY_PROMPT + json.dumps(old, ensure_ascii=False)}],
            tools=None,
        )
    except asyncio.CancelledError:
        raise  # 取消要原样往上传（对应"停止"按钮）
    except Exception as exc:  # noqa: BLE001
        failure_reason = f"{type(exc).__name__}: {exc}"[:200]
        message = None

    # 压缩本身也是一次请求，记账，否则用量账本会缺一块
    if message is not None:
        session.record_usage(message.usage, label="compact")

    summary = (message.content or "").strip() if message is not None else ""
    fallback = False
    if not summary:
        fallback = True
        failure_reason = failure_reason or "empty_summary"
        summary = _fallback_summary(old)
        if bus is not None:
            bus.emit(
                "compaction_fallback",
                session_id=session.id,
                reason=failure_reason,
                summary_chars=len(summary),
            )

    session.replace_with_summary(summary, len(recent))
    if bus is not None:
        bus.emit(
            "compaction_end",
            session_id=session.id,
            chars=session.size_chars(),
            tokens=estimate_context_tokens(session, tools),
            count=session.compact_count,
            fallback=fallback,
        )
    return summary
