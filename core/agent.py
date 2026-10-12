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
    max_turns: int = 12
    usage_label: str = "main"
    cancel: asyncio.Event | None = None  # 取消信号（子 Agent 的取消级联靠它 + task.cancel）
    # 压缩阈值（字符数）。None = 用全局默认；子 Agent 会传更小的值。
    # 为什么能压得更狠：子 Agent 的产物都落盘成了画布节点正文，历史里丢掉的细节随时能重读。
    compact_threshold: int | None = None
    compact_token_limit: int | None = None
    last_finish_reason: str | None = None
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
        self.accepting_followups = True
        self._emit("agent_start", agent=self.name)
        user_entry_id = self.session.append({"role": "user", "content": user_input})
        self._emit("user_message_committed", entry_id=user_entry_id)
        final = ""

        for turn in range(1, self.max_turns + 1):
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
            if message.content:
                final = message.content
            assistant_parent_id = self.session.current_id
            assistant_entry_id = self.session.append(message.to_history_item())
            self._emit("assistant_message_committed", entry_id=assistant_entry_id, parent_id=assistant_parent_id)

            if message.finish_reason == "length":
                self._emit(
                    "limited",
                    reason="length",
                    message="模型输出达到长度限制，当前结果可能不完整，请继续生成或缩小任务范围。",
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
                break

            await self._run_tools(message, turn)
            self._emit("turn_end", turn=turn)
        else:
            self.last_finish_reason = "max_turns"
            self._emit(
                "limited",
                reason="max_turns",
                message=f"Agent 已达到最大工具循环轮次（{self.max_turns}），任务可能尚未完成。",
            )
            final = final or "（达到最大轮次，提前结束）"

        self._emit("agent_end", agent=self.name)
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

    async def _run_tools(self, message: AssistantMessage, turn: int) -> None:
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

    async def _execute_one(
        self, tool: ToolDefinition, raw_args: dict, name: str, call_id: str
    ) -> tuple[str, dict, bool]:
        try:
            validated = tool.validate_args(raw_args)  # Pydantic 校验
            normalized_args = validated.model_dump(mode="json") if validated is not None else raw_args
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
            result = tool.execute(raw_args, self.context)
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
        "- 判断某个技能与任务相关后，先用 read_skill 读正文再动手；references 仅按正文路由和当前需要读取",
        "- 用户要求制作、生成、继续或修改视频作品时，优先读取 workflow，并以它作为唯一制作主流程；不要再叠加 legacy 的 drama_script/storyboard/asset_design/shot_prompt 形成冲突模板",
        "- 每次收到新的故事/短剧创意，必须先调用 identify_story 做剧本识别，再对它返回的画布调用 get_canvas；不得默认把新创意追加到当前画布",
        "- 只有用户明确表示续写、修改或继续某个已有故事时才复用旧画布；用户说另一段、新故事或内容核心人物/事件不同，就建立独立剧本画布",
        "- 用户只有一句创意时，先补成可审阅的完整剧本；已有定稿剧情且目标是视频制作时，按 workflow 锁存 Seedance 路由和后续阶段",
        "- 用户提出短剧、视频或图片创意时，完成当前已授权且可执行的上游工作；不要因为最终媒体生成暂不可用而拒绝剧本、分镜、资产和正式提示词",
        "- 媒体生成前先检查实际 provider；只有 Mock 时必须明确它仅验证链路，不能把占位首帧说成真实视频",
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
