"""agent 循环。

对照 Pi 的 packages/agent/src/agent-loop.ts（mini_pi.py 的 run_agent 是它的简化版）：
  加一条用户消息 → 问模型 → 模型要么回答（结束）要么要调工具
  → 执行工具、结果也进历史 → 再问 …… 直到不再要工具

新增的三件事（相对 mini_pi.py）：
  1. 工具来自插件装载器，参数用 Pydantic 校验（校验失败 = 工具报错，喂回模型让它改）
  2. 全程发事件（EventBus），后面 FastAPI 靠它推 NDJSON
  3. 支持取消：每个 await 点都可能被 task.cancel() 打断
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .compaction import maybe_compact
from .context import ToolContext
from .events import EventBus
from .invocation_store import InvocationState, ToolInvocationStore, canonical_hash, canonical_json
from .llm import AssistantMessage, LLM, Usage
from .plugin import ToolDefinition, ToolResult
from .skills import SkillEntry, skills_prompt_section
from .session import Session

# 事件里带正文的上限：事件是给人看的，没必要把整个文件内容塞进流里
# （前端要完整内容应该让 Agent 去读文件，而不是靠事件）。超长时前端会提示"已截断"。
MAX_EVENT_TEXT = 6000


def _positive_env_int(name: str, default: int) -> int:
    """读取正整数配置；配置写错时使用安全默认值，而不是让服务启动失败。"""
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


class AgentAborted(Exception):
    """被取消。对应 Toonflow 的 AbortError。"""


@dataclass
class Agent:
    llm: LLM
    session: Session
    tools: list[ToolDefinition]
    context: ToolContext
    bus: EventBus = field(default_factory=EventBus)
    name: str = "main"
    # 12 轮不足以稳定完成“剧本→分镜→资产→提示词→连线”的完整生产图。
    # 仍保留硬上限防止无限工具循环，但达到上限属于 limited，不伪装成 completed。
    max_turns: int = field(default_factory=lambda: _positive_env_int("AGENT_MAX_TURNS", 40))
    usage_label: str = "main"
    cancel: asyncio.Event | None = None  # 取消信号（子 Agent 的取消级联靠它 + task.cancel）
    # 压缩阈值（字符数）。None = 用全局默认；子 Agent 会传更小的值。
    # 为什么能压得更狠：子 Agent 的产物都落盘成了画布节点正文，历史里丢掉的细节随时能重读。
    compact_threshold: int | None = None
    compact_token_limit: int | None = None
    last_finish_reason: str | None = None
    last_status: str = "idle"
    last_checkpoint: dict[str, Any] | None = None
    # 运行中追加的要求。只在模型/工具调用之间的安全点进入上下文，避免破坏
    # assistant(tool_calls) -> tool 的协议配对。
    followups: deque[str] = field(default_factory=deque)
    accepting_followups: bool = False
    external_followup_loader: Callable[[], Awaitable[list[str]]] | None = None
    invocations: ToolInvocationStore | None = None

    @property
    def tool_map(self) -> dict[str, ToolDefinition]:
        return {t.name: t for t in self.tools}

    # ------------------------------------------------------------------
    def _emit(self, type_: str, **data: Any) -> None:
        self.bus.emit(type_, session_id=self.session.id, **data)

    def _check_abort(self) -> None:
        """模型请求之间的检查点。

        Toonflow 是每个工具执行前 throwIfAborted()；这里在两个地方检查：
        await 点靠 task.cancel() 打断，循环间隙靠这个 Event。
        """
        if self.cancel is not None and self.cancel.is_set():
            raise AgentAborted(self.name)

    def queue_followup(self, text: str) -> int:
        """把运行中的补充要求加入队列，返回当前排队数量。"""
        value = str(text or "").strip()
        if not value:
            raise ValueError("补充要求不能为空")
        if not self.accepting_followups:
            raise RuntimeError("当前 Agent 已进入收尾阶段，请作为下一条消息发送")
        self.followups.append(value)
        self.session.append_custom("followup", {"text": value, "status": "queued"}, display=True)
        self._emit("followup_queued", text=value, pending=len(self.followups))
        return len(self.followups)

    def _drain_followups(self) -> int:
        """在安全点把补充要求转成真正的 user 消息。"""
        count = 0
        while self.followups:
            text = self.followups.popleft()
            entry_id = self.session.append({"role": "user", "content": text})
            self.session.append_custom(
                "followup", {"text": text, "status": "consumed", "entry_id": entry_id}, display=True
            )
            self._emit("followup_consumed", text=text, entry_id=entry_id, pending=len(self.followups))
            count += 1
        return count

    async def _pull_external_followups(self) -> int:
        """在模型/工具之间的安全点读取共享队列，不破坏 tool-call 消息配对。"""
        if self.external_followup_loader is None:
            return 0
        values = await self.external_followup_loader()
        for text in values:
            self.followups.append(text)
            self.session.append_custom("followup", {"text": text, "status": "queued"}, display=True)
            self._emit("followup_queued", text=text, pending=len(self.followups), source="shared_store")
        return len(values)

    # ------------------------------------------------------------------
    async def run(self, user_input: str) -> str:
        self.last_finish_reason = None
        self.last_status = "running"
        self.last_checkpoint = None
        self.accepting_followups = True
        self._emit("agent_start", agent=self.name)
        user_entry_id = self.session.append({"role": "user", "content": user_input})
        self._emit("user_message_committed", entry_id=user_entry_id)
        final = ""
        tool_calls = 0
        tool_errors = 0
        last_turn = 0

        for turn in range(1, self.max_turns + 1):
            last_turn = turn
            self._check_abort()
            await self._pull_external_followups()
            self._drain_followups()
            # 压缩检查放在每次请求之前（和 Pi 的位置一致）
            settings = getattr(self.llm, "settings", None)
            await maybe_compact(
                self.session,
                self.llm,
                bus=self.bus,
                threshold=self.compact_threshold,
                context_window=getattr(settings, "context_window", 128000),
                max_output_tokens=getattr(settings, "max_output_tokens", 8192),
                token_limit=self.compact_token_limit,
                tools=[tool.to_openai_schema() for tool in self.tools],
            )
            self._emit("turn_start", turn=turn)

            message = await self._request_model(turn)
            self.last_finish_reason = message.finish_reason
            if message.finish_reason == "length" and message.tool_calls:
                # 输出被 max_tokens 截断时，模型往往正停在半个 tool_call 上。这些调用不会执行，
                # 若照原样写进历史，下一轮请求就带着悬空的 tool_calls —— provider 可能直接 400，
                # 也可能只回一条空消息（线上症状：点「继续」0.5s 空回复再报错）。
                dropped = len(message.tool_calls)
                message.tool_calls = []
                limit = getattr(getattr(self.llm, "settings", None), "max_output_tokens", 8192)
                note = (
                    f"（上一轮输出达到 {limit} token 上限被截断，"
                    f"{dropped} 个未执行的工具调用已丢弃。请把这一步拆成更小的批次再试。）"
                )
                message.content = f"{message.content}\n\n{note}".strip() if message.content else note
            if message.content:
                final = message.content
            assistant_parent_id = self.session.current_id
            assistant_entry_id = self.session.append(message.to_history_item())
            self._emit("assistant_message_committed", entry_id=assistant_entry_id, parent_id=assistant_parent_id)

            if message.finish_reason == "length":
                self.last_status = "limited"
                limit = getattr(getattr(self.llm, "settings", None), "max_output_tokens", 8192)
                self._emit(
                    "limited",
                    reason="length",
                    message=(
                        f"模型这一轮写到了 {limit} token 的输出上限被截断，当前结果不完整。"
                        "把这一步拆成更小的批次（例如一次只写几个分镜）再继续，比直接续跑更稳。"
                    ),
                )
                self._emit("turn_end", turn=turn)
                await asyncio.sleep(0)
                await self._pull_external_followups()
                if self.followups:
                    continue
                self.accepting_followups = False
                break

            if not message.tool_calls:
                self._emit("turn_end", turn=turn)
                # 模型生成期间用户可能补了新要求；先让当前回复完整落盘，再开下一轮。
                await asyncio.sleep(0)
                await self._pull_external_followups()
                if self.followups:
                    continue
                self.accepting_followups = False
                if final.strip():
                    self.last_status = "completed"
                else:
                    self.last_status = "error"
                    self.last_finish_reason = "empty_response"
                    reason = message.finish_reason or "unknown"
                    self._emit(
                        "error",
                        message=(
                            f"模型这次返回了空内容（finish_reason={reason}），任务没有完成。"
                            "常见原因：上一条输出被截断后留下的半截工具调用、或单轮请求过大；"
                            "直接再发一次「继续」即可，若仍是空回复就新建会话。"
                        ),
                    )
                break

            calls, errors = await self._run_tools(message, turn)
            tool_calls += calls
            tool_errors += errors
            self._emit("turn_end", turn=turn)
        else:
            self.last_finish_reason = "max_turns"
            self.last_status = "limited"
            self._emit(
                "limited",
                reason="max_turns",
                message=f"Agent 已达到最大工具循环轮次（{self.max_turns}），任务可能尚未完成。",
            )
            final = final or "（已暂停：达到本次运行的最大轮次，可从检查点继续制作。）"

        if self.last_status == "limited":
            self.last_checkpoint = {
                "status": "limited",
                "reason": self.last_finish_reason or "unknown",
                "turn": last_turn,
                "max_turns": self.max_turns,
                "tool_calls": tool_calls,
                "tool_errors": tool_errors,
                "last_entry_id": self.session.current_id,
                "resume_instruction": (
                    "继续当前任务：先读取当前画布和已有产物，核对未完成项；"
                    "不要重复创建已存在的节点，只补齐缺失内容并最终给出交付总结。"
                ),
            }
            checkpoint_id = self.session.append_custom(
                "runCheckpoint", self.last_checkpoint, display=True
            )
            self._emit("checkpoint", checkpoint_id=checkpoint_id, **self.last_checkpoint)
        else:
            # 写入最新终态，用来覆盖更早的 limited 检查点；否则成功续跑后刷新页面，
            # 前端会错误地把旧检查点当成仍需继续的任务。
            self.session.append_custom(
                "runCheckpoint",
                {
                    "status": self.last_status,
                    "reason": self.last_finish_reason or "unknown",
                    "turn": last_turn,
                    "max_turns": self.max_turns,
                    "tool_calls": tool_calls,
                    "tool_errors": tool_errors,
                    "last_entry_id": self.session.current_id,
                },
                display=False,
            )

        self._emit(
            "agent_end",
            agent=self.name,
            status=self.last_status,
            finish_reason=self.last_finish_reason,
        )
        self.accepting_followups = False
        return final

    async def _request_model(self, turn: int) -> AssistantMessage:
        """一次模型请求。

        `block_id` 是给前端的分组键：一次 assistant 回复 = 一个 part 容器，
        思考 / 工具 / 正文按**发生顺序**内联在同一条消息里（照 Toonflow 的 parts[]）。
        没有它，前端只能靠"事件到达顺序"猜分组，就会出现一堆空气泡。
        """
        block = f"b{turn}"
        self._emit("message_start", role="assistant", block_id=block)
        buffer: list[str] = []
        thinking: list[str] = []

        def on_text(delta: str) -> None:
            buffer.append(delta)
            self._emit("message_update", block_id=block, delta=delta)

        def on_reasoning(delta: str) -> None:
            thinking.append(delta)
            self._emit("thinking_update", block_id=block, delta=delta)

        try:
            message = await self.llm.chat(
                self.session.request_messages(),
                [t.to_openai_schema() for t in self.tools] or None,
                on_text=on_text,
                on_reasoning=on_reasoning,
            )
        except asyncio.CancelledError:
            self._emit("message_end", role="assistant", block_id=block, interrupted=True)
            raise AgentAborted(self.name)

        if thinking:
            # 用推理模型（如 deepseek-reasoner）时才有；非推理模型不产生这个事件
            self._emit("thinking_end", block_id=block, chars=len("".join(thinking)))

        # 用量必须记账：没有这一步，模块二的增量统计就无从谈起
        self.session.record_usage(message.usage, label=self.usage_label)
        self._emit("usage", **message.usage.as_dict(), label=self.usage_label, block_id=block)
        self._emit(
            "message_end",
            role="assistant",
            block_id=block,
            chars=len("".join(buffer)),
            content="".join(buffer)[:MAX_EVENT_TEXT],  # 前端可据此对齐/纠偏
        )
        return message

    async def _run_tools(self, message: AssistantMessage, turn: int) -> tuple[int, int]:
        errors = 0
        for index, call in enumerate(message.tool_calls):
            self._check_abort()
            tool = self.tool_map.get(call.name)
            started = time.perf_counter()
            # tool_call_id 让前端能精确配对"开始/结束"：同一轮里同名工具调两次也不会串卡
            self._emit(
                "tool_execution_start",
                tool=call.name,
                args=call.arguments,
                tool_call_id=call.id,
                index=index,
            )

            if tool is None:
                content, details, is_error = f"Error: 没有这个工具 {call.name}", {}, True
            else:
                content, details, is_error = await self._execute_one(
                    tool, call.parsed_args(), call.name, call.id
                )

            # `sub_agent` 这个键已经被"事件由哪个子 Agent 发出"占用（subagent.py 的 forward），
            # 而委托工具的结果里也想表达"这次调用涉及哪个子 Agent" —— 语义撞车会让前端
            # 把父的工具结果误挂进子 Agent 块里（实测过）。所以普通字段一律改名。
            details = {
                ("sub_agent_name" if key == "sub_agent" else key): value for key, value in details.items()
            }

            self._emit(
                "tool_execution_end",
                tool=call.name,
                tool_call_id=call.id,
                isError=is_error,
                duration_ms=round((time.perf_counter() - started) * 1000),
                result=content[:MAX_EVENT_TEXT],  # 结果摘要：前端要能直接看出"改成了什么"
                result_chars=len(content),
                **details,
            )
            self.session.append({"role": "tool", "tool_call_id": call.id, "content": content})
            errors += int(is_error)
        return len(message.tool_calls), errors

    async def _execute_one(
        self, tool: ToolDefinition, raw_args: dict, name: str, call_id: str
    ) -> tuple[str, dict, bool]:
        try:
            validated = tool.validate_args(raw_args)  # Pydantic 校验
            normalized_args = (
                validated.model_dump(mode="json", exclude_none=True)
                if validated is not None
                else raw_args
            )
        except Exception as exc:
            return f"Error: {type(exc).__name__}: {exc}", {}, True

        invocation_id: str | None = None
        if tool.side_effect and self.invocations is not None:
            idempotency_key = canonical_hash({"session_id": self.session.id, "tool_call_id": call_id})
            prepared, created = await self.invocations.prepare(
                InvocationState(
                    invocation_id=f"inv_{idempotency_key[:32]}",
                    idempotency_key=idempotency_key,
                    session_id=self.session.id,
                    run_id=self.bus.run_id or "",
                    tool_name=name,
                    args_hash=canonical_hash(normalized_args),
                    args_json=canonical_json(normalized_args),
                )
            )
            invocation_id = prepared.invocation_id
            if not created:
                if prepared.args_hash != canonical_hash(normalized_args) or prepared.tool_name != name:
                    return "Error: 幂等键冲突，拒绝执行参数不同的副作用调用", {"invocation_id": invocation_id}, True
                if prepared.status in {"succeeded", "failed"} and prepared.result_json:
                    restored = json.loads(prepared.result_json)
                    details = dict(restored.get("details") or {})
                    details["invocation_id"] = invocation_id
                    details["idempotent_replay"] = True
                    return str(restored.get("content") or ""), details, bool(restored.get("is_error"))
                if prepared.status in {"executing", "unknown"}:
                    return (
                        "Error: 上次副作用调用结果不确定，已禁止自动重试，请人工核对后处理",
                        {"invocation_id": invocation_id, "invocation_status": prepared.status},
                        True,
                    )
            try:
                await self.invocations.mark_executing(invocation_id)
            except RuntimeError:
                current = await self.invocations.get(invocation_id)
                return (
                    "Error: 同一副作用调用正在执行或状态不确定，已禁止并发重复执行",
                    {"invocation_id": invocation_id, "invocation_status": current.status if current else "missing"},
                    True,
                )

        try:
            # 校验后的参数才是唯一执行输入：默认值已补齐，Optional=None 不会被误当成更新字段。
            result = tool.execute(normalized_args, self.context)
            if inspect.isawaitable(result):
                result = await result
        except asyncio.CancelledError:
            if invocation_id and self.invocations is not None:
                await self.invocations.mark_unknown(invocation_id, "任务在副作用执行期间被取消")
            raise
        except Exception as exc:  # 工具失败不中断循环，把错误喂回模型
            outcome = (f"Error: {type(exc).__name__}: {exc}", {}, True)
            if invocation_id and self.invocations is not None:
                await self.invocations.complete(
                    invocation_id,
                    {"content": outcome[0], "details": outcome[1], "is_error": True},
                    failed=True,
                )
            return outcome

        if isinstance(result, ToolResult):
            outcome = (result.content, result.details, result.is_error)
        elif isinstance(result, str):
            outcome = (result, {}, False)
        else:
            outcome = (json.dumps(result, ensure_ascii=False), {}, False)
        if invocation_id and self.invocations is not None:
            outcome[1]["invocation_id"] = invocation_id
            await self.invocations.complete(
                invocation_id,
                {"content": outcome[0], "details": outcome[1], "is_error": outcome[2]},
                failed=outcome[2],
            )
        return outcome


def build_system_prompt(
    cwd: str,
    tools: list[ToolDefinition],
    skills: list[SkillEntry] | None = None,
) -> str:
    """分段拼装（对应 Pi 的分段 system prompt / Toonflow 的 prompt.ts）。

    promptGuidelines 单独成段：它是"工具作者给模型的用法约束"，
    和工具描述分开，方便单独替换某一段（这就是所谓"补丁"）。

    技能段只放**清单**（name + description + location），正文靠 read_skill 按需读 ——
    技能数量增长时，每轮请求的上下文不会跟着线性膨胀。
    """
    guidelines = [g for t in tools for g in t.prompt_guidelines]
    parts = [
        "你是 ToonFlow 的 AI 创作搭档，帮助用户把故事、剧本、分镜与素材做成作品。默认使用中文。",
        "<tools>",
        *[f"- {t.name}: {t.description}" for t in tools],
        "</tools>",
        "<rules>",
        "- 需要看文件内容时必须调用工具，不要凭空猜测",
        "- 工具报错时先看错误信息，再决定是否换参数重试",
        "- 所有路径都是工作区内的相对路径",
        "- 对话说明保持简洁；剧本、对白、分镜、正式生成提示词和技能要求的作品成果必须完整，不得为了简洁压成梗概",
        "- 技能是分层的：`workflow` 管**流程**（从哪开始、顺序、门禁、确认与恢复，正文是一张路由表，按阶段指到 references 分册）；`drama_craft` 管**工艺**（首帧八段式、引用与文字锁、分镜信息块、表演与抛光细节）。两者配合使用，不是二选一；判断相关后先用 read_skill 读正文再动手，references 只读当前阶段需要的那一册",
        "- 用户要求制作、生成、继续或修改视频作品时，先读 workflow 判定处在哪个阶段，再按它指的路由读对应分册；写提示词、首帧、引用、文字锁、表演、抛光时读 drama_craft。禁止引用或叠加已删除的 legacy 技能 asset_design/drama_script/storyboard/shot_prompt",
        "- 每次收到新的故事/短剧创意，必须先调用 identify_story 做剧本识别，再对它返回的画布调用 get_canvas；不得默认把新创意追加到当前画布",
        "- 只有用户明确表示续写、修改或继续某个已有故事时才复用旧画布；用户说另一段、新故事或内容核心人物/事件不同，就建立独立剧本画布",
        "- 用户只有一句创意时，先补成可审阅的完整剧本；已有定稿剧情且目标是视频制作时，按 workflow 锁存 Seedance 路由和后续阶段",
        "- 用户提出短剧、视频或图片创意时，完成当前已授权且可执行的上游工作；不要因为最终媒体生成暂不可用而拒绝剧本、分镜、资产和正式提示词",
        "- 媒体生成前先检查实际 provider；只有 Mock 时必须明确它仅验证链路，不能把可播放的占位 MP4 说成真实模型作品",
        "- 缺少非关键参数时采用合理默认值继续；缺少会实质改变结果的参数时可直接在回复中询问，question 工具不可用不代表不能提问",
        "- 如果用户要求最终视频但只有 Mock：仍可建立完整生产包和生成节点；在消耗算力或执行 Mock 前展示完整方案并取得明确确认",
        "- 不擅自增加用户未提供的剧情事件、人物、前史、字幕、站立或转场；合理的摄影与物理执行细节必须保持剧情事实不变",
        "- 已写入节点的长正文在最终回复中只给摘要和查看位置，但节点/文件里的作品正文必须保存完整版本",
        "</rules>",
    ]
    if skills:
        parts.append(skills_prompt_section(skills))
    if guidelines:
        parts += ["<guidelines>", *guidelines, "</guidelines>"]
    parts += [f"<cwd>\n{cwd}\n</cwd>"]
    return "\n".join(parts)
