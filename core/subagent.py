"""子 Agent 委派。

对照 Toonflow：
  · runtime/subAgent.ts  —— 独立会话、abort 级联、**previousEntries 增量用量**
  · runtime/delegation.ts —— 事件转发（打标 subAgentEvent）+ 用量回收
  · runtime/sessions.ts  —— 会话注册表（这里就是 DelegationManager.sessions）
  · tools/subAgent.ts + report.ts —— 父子两条通道

四个要点，逐条对应：
  1. 独立会话：子 Agent 有自己的 Session，父的历史不会被子任务污染
  2. 取消级联：父被中止 → 子必须一起停（Event + task.cancel 两条路都堵上）
  3. 增量用量：跑之前记下 usage_offset()，跑完只求和新增的部分
  4. 父子通信：父→子用 subagent 工具；子→父用 report 工具（消息传递，不是共享变量）
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from .agent import Agent, AgentAborted, build_system_prompt
from .context import ToolContext
from .events import EventBus
from .llm import LLM, Usage
from .plugin import ToolDefinition, define_tool, tool_result
from .session import Session
from .skills import SkillEntry, discover_skills

# 对应 Toonflow subAgent.ts 的 16000：子 Agent 的结论超过这个长度就截断，
# 并提示父 Agent 改去读工作区产物（而不是把超长文本灌进父的上下文）。
MAX_RESULT_CHARS = 16000

# 子 Agent 的压缩阈值（比主会话激进）：
# 团队/委派场景里子会话动辄十几轮，而"第 N 轮的输入 ≈ N × 单轮增量"——
# 累积历史才是成本大头（实测一个 5 人团队跑一次 300k tokens）。
# 子 Agent 的产物都已落盘到画布节点正文，历史里删掉的细节随时能重新读回来，所以敢压。
SUB_AGENT_COMPACT_TOKENS = int(os.environ.get("SUB_AGENT_COMPACT_TOKENS", "72000"))
# 兼容旧 demo/import 名称；新运行时不再按字符触发。
SUB_AGENT_COMPACT_CHARS = SUB_AGENT_COMPACT_TOKENS

# custom entry 的类型名（对应 Toonflow 的 customType）。
# 它们**不进上下文**，只用于落盘、前端展示与"重启后恢复"。
CUSTOM_SUBAGENT = "toonflowSubAgent"
CUSTOM_REPORT = "subagentReport"


def restore_subagents(session: Session) -> list[dict]:
    """从父会话的 JSONL 恢复子 Agent 视图；**上次没跑完的会被标记为中断**。

    对应 Toonflow sessions.ts 里那段：
      agent.status === "running" && !active  →  status: "cancelled", result: "上次运行已中断"

    判定依据是"文件里写着 running"：进程重启后不可能还活着，所以 running 必然是残留。
    """
    latest: dict[str, dict] = {}
    for data in session.customs_of(CUSTOM_SUBAGENT):
        name = data.get("name")
        if name:
            latest[name] = data  # 同名取最后一条（状态是覆盖式更新）

    out: list[dict] = []
    for name, data in latest.items():
        if data.get("status") == "running":
            data = {**data, "status": "aborted", "interrupted": True, "note": "上次运行已中断"}
        out.append(data)
    return out

DEFAULT_SUB_TOOLS = ["list_dir", "read_file", "write_file", "edit_file", "word_count", "read_skill", "ask_user"]


class SubAgentArgs(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-zA-Z0-9_-]*$", description="子 Agent 名字（同名会复用会话）")
    task: str = Field(description="派给子 Agent 的任务描述")
    tools: list[str] = Field(default_factory=lambda: list(DEFAULT_SUB_TOOLS), description="子 Agent 可用的工具白名单")


class ReportArgs(BaseModel):
    summary: str = Field(description="给父 Agent 的结论（会作为委派结果返回）")
    detail: str = Field("", description="可选的过程说明")


@dataclass
class DelegationResult:
    """一次委派的结果。usage 是**增量**，usage_all 是全量（对照实验用）。"""

    name: str
    answer: str
    usage: Usage
    usage_all: Usage
    reports: list[str] = field(default_factory=list)
    aborted: bool = False
    status: str = "done"


@dataclass
class DelegationRecord:
    """注册表里的一条记录（对应 sessions.ts 的 updateSubAgent）。"""

    name: str
    session: Session
    # Toonflow 的子 Agent 状态机：running / completed / error / limited / cancelled / inputRequired
    # 本实现的映射：running→running、completed→done、cancelled→aborted、error/limited 同名。
    status: str = "idle"  # idle / running / inputRequired / done / limited / error / aborted
    task: str = ""
    reports: list[str] = field(default_factory=list)
    last_usage: Usage = field(default_factory=Usage)
    error: str | None = None


class DelegationManager:
    """委派管理器：持有父 Agent 的引用，管理子会话与两条通道。"""

    def __init__(
        self,
        parent: Agent,
        llm: LLM,
        cwd: str,
        skills: list[SkillEntry] | None = None,
    ) -> None:
        self.parent = parent
        self.llm = llm
        self.cwd = cwd
        self.registry: dict[str, DelegationRecord] = {}
        # 技能清单（只有名 + 描述 + 路径，正文仍要 read_skill 按需读）。
        # 子 Agent 以前拿不到它：于是"子 Agent 的专业性"只剩角色 md，
        # 角色 md 一过期（例如仍写旧 6 字段）就与 skills / 校验器三头打架。
        if skills is None:
            discovered, errors = discover_skills()
            self.skills: list[SkillEntry] = discovered
            self.skill_errors: list[str] = errors
        else:
            self.skills = list(skills)
            self.skill_errors = []

    def _skills_for(self, names: list[str] | None) -> list[SkillEntry]:
        """按角色白名单取技能子集；不给名字 = 全部（向后兼容）。"""
        wanted = {str(name).strip() for name in (names or []) if str(name).strip()}
        if not wanted:
            return list(self.skills)
        return [entry for entry in self.skills if entry.name in wanted]

    def mark_question_status(self, name: str, waiting: bool, question: dict[str, Any]) -> None:
        """结构化提问期间把子 Agent 暴露为 inputRequired，回答后恢复 running。"""
        record = self.registry.get(name)
        if record is None:
            return
        record.status = "inputRequired" if waiting else "running"
        self._persist_state(record, record.task)

    # ------------------------------------------------------------------
    # 父 → 子：subagent 工具
    # ------------------------------------------------------------------
    def _ensure_skill_reader(
        self, tools: list[ToolDefinition], skills: list[SkillEntry]
    ) -> list[ToolDefinition]:
        """带技能的子 Agent 必须能读正文：工具表里没有 read_skill 就补一个。

        团队成员的 tools 白名单是手写的，很容易漏；漏了就会出现
        「角色 md 让它 read_skill，它却没有这个工具」的哑火。
        """
        if not skills or any(tool.name == "read_skill" for tool in tools):
            return tools
        reader = getattr(self, "_available", {}).get("read_skill")
        if reader is None:  # 父工具还没注册（demo / 手动工具表）时自建一个
            from .skills import build_skill_tools

            reader = build_skill_tools()[0]
        return [*tools, reader]

    def _child_system_prompt(
        self,
        current: str,
        child_tools: list[ToolDefinition],
        skills: list[SkillEntry],
        role_prompt: str | None,
    ) -> str:
        """子会话的 system prompt：清单 + 角色段。已存在且不陈旧就原样复用。

        旧会话落盘时没有 `<available_skills>` 段 —— 光靠 `if not current`
        会让升级后的团队继续用旧提示词跑，所以带技能时再做一次陈旧判定。
        """
        stale = bool(skills) and "<available_skills>" not in (current or "")
        if current and not stale:
            return current
        base = build_system_prompt(self.cwd, child_tools, skills)
        # 团队里的成员会额外拿到一段角色提示词（写在 md 里，改提示词不用改代码）
        return f"{base}\n\n<role>\n{role_prompt}\n</role>" if role_prompt else base

    def parent_tool(self, available_tools: list[ToolDefinition]) -> ToolDefinition:
        self._available = {t.name: t for t in available_tools}

        async def _execute(args: SubAgentArgs, ctx: ToolContext) -> Any:
            result = await self.delegate(name=args.name, task=args.task, tool_names=args.tools)
            body = result.answer
            if result.reports:
                body += "\n[子 Agent 上报]\n" + "\n".join(f"- {r}" for r in result.reports)
            return tool_result(
                body,
                sub_agent=result.name,
                usage=result.usage.as_dict(),
                aborted=result.aborted,
                status=result.status,  # 让父 Agent 知道子任务是 done / limited / error / aborted
            )

        return define_tool(
            name="subagent",
            description=("派生一个子 Agent 执行独立任务（它有自己独立的会话与上下文）。"
                         "适合调研、统计、审阅这类能独立收尾的子任务；完成后你会拿到它的结论与本次用量。"),
            args_model=SubAgentArgs,
            execute=_execute,
            prompt_guidelines=[
                "子任务要用 subagent 委派，不要自己硬做",
                "同名子 Agent 会复用会话，适合多轮追问",
                "子 Agent 也能读技能（read_skill）：把「按哪个技能做」写进 task，不要把整段工艺细节抄进 task",
            ],
        )

    # ------------------------------------------------------------------
    # 子 → 父：report 工具
    # ------------------------------------------------------------------
    def _report_tool(self, record: DelegationRecord, child_bus: EventBus) -> ToolDefinition:
        def _execute(args: ReportArgs, ctx: ToolContext) -> Any:
            # 注意这里没有共享变量：子 Agent 通过"调用工具"把消息发出来，
            # 父侧收到的是一条可记录、可回放的事件 + 一条上报文本。
            record.reports.append(args.summary)
            self._persist_report(record, args.summary, args.detail)  # 上报也进父会话的 JSONL
            child_bus.emit("sub_agent_report", sub_agent=record.name, summary=args.summary, detail=args.detail)
            ctx.log(f"{record.name} 上报：{args.summary[:40]}")
            return tool_result("已上报给父 Agent", reported=args.summary)

        return define_tool(
            name="report",
            description="把阶段性结论上报给父 Agent（不会结束子任务，可多次调用）",
            args_model=ReportArgs,
            execute=_execute,
        )

    # ------------------------------------------------------------------
    # 子会话：独立落盘 + 可复用
    # ------------------------------------------------------------------
    def _child_session(self, name: str) -> Session:
        """子 Agent 的会话。两处对应 Toonflow：

        · 子会话**独立落盘**（父会话里只留一个 file 引用，不把子历史塞进父文件）
        · 文件已存在就 `load` 复用历史 —— 于是"重启后同名子 Agent 接着聊"成立，
          增量计量的起点也跟着历史一起恢复（usage_log 被还原）
        """
        parent_path = self.parent.session.path
        if parent_path is None:
            return Session()  # 纯内存场景（demo 不落盘）保持原样
        path = parent_path.parent / f"{parent_path.stem}.{name}.jsonl"
        if path.exists():
            return Session.load(path)
        return Session(session_id=f"{parent_path.stem}.{name}", path=path)

    # ------------------------------------------------------------------
    # 落盘：子 Agent 的状态与上报写进**父会话**（对应 updateSubAgent / updateSubAgentReport）
    # ------------------------------------------------------------------
    def _persist_state(
        self,
        record: DelegationRecord,
        task: str,
        delta: Usage | None = None,
        truncated: bool = False,
        aborted: bool = False,
    ) -> None:
        session = self.parent.session
        session.append_custom(
            CUSTOM_SUBAGENT,
            {
                "name": record.name,
                "status": record.status,
                "task": task[:200],
                "file": record.session.path.name if record.session.path else None,
                "usage": delta.as_dict() if delta is not None else None,
                "cumulative": record.session.usage_total().as_dict(),
                "reports": len(record.reports),
                "error": record.error,
                "aborted": aborted,
                "truncated": truncated,
            },
        )
        self.parent.bus.emit(
            "sub_agent_state", session_id=session.id, sub_agent=record.name, status=record.status
        )

    def _persist_report(self, record: DelegationRecord, summary: str, detail: str | None = None) -> None:
        self.parent.session.append_custom(
            CUSTOM_REPORT,
            {"agent": record.name, "content": summary, "detail": detail},
            display=True,
        )

    # ------------------------------------------------------------------
    # 委派执行
    # ------------------------------------------------------------------
    async def delegate(
        self,
        name: str,
        task: str,
        tool_names: list[str] | None = None,
        role_prompt: str | None = None,
        skill_names: list[str] | None = None,
    ) -> DelegationResult:
        tool_names = tool_names or list(DEFAULT_SUB_TOOLS)
        allowed = [self._available[n] for n in tool_names if n in getattr(self, "_available", {})]

        # 会话注册表：同名复用（这也是"为什么必须增量计量"的根因）
        record = self.registry.get(name)
        if record is None:
            record = DelegationRecord(name=name, session=self._child_session(name))
            self.registry[name] = record
        record.task = task

        # ★ 起始偏移：本次委派开始前，这个子会话已经花掉的量不算在我头上
        offset = record.session.usage_offset()

        child_bus = EventBus()
        forwarder = asyncio.create_task(self._forward_events(child_bus, name))
        child_cancel = asyncio.Event()

        def reporter_factory(rec: DelegationRecord) -> ToolDefinition:
            return self._report_tool(rec, child_bus)

        child_tools = allowed + [reporter_factory(record)]
        # 子 Agent 也要能读技能正文：清单进 system prompt，正文靠 read_skill。
        skills = self._skills_for(skill_names)
        child_tools = self._ensure_skill_reader(child_tools, skills)
        child_ctx = self.parent.context.derive(
            config={**getattr(self.parent.context, "config", {}), "agent_name": name},
            tool_names={tool.name for tool in child_tools},
        )
        record.session.system_prompt = self._child_system_prompt(
            record.session.system_prompt, child_tools, skills, role_prompt
        )

        child = Agent(
            llm=self.llm,
            session=record.session,
            tools=child_tools,
            context=child_ctx,
            bus=child_bus,
            name=name,
            cancel=child_cancel,
            usage_label=f"sub:{name}",
            compact_token_limit=SUB_AGENT_COMPACT_TOKENS,
            invocations=self.parent.invocations,
        )

        record.status = "running"
        self._persist_state(record, task)  # 先写 running：这样中断后能看出"它没跑完"
        self.parent.bus.emit("sub_agent_start", session_id=self.parent.session.id, sub_agent=name, task=task[:80])

        child_task = asyncio.create_task(child.run(task))
        watcher = asyncio.create_task(self._watch_parent(child_cancel, child_task))

        aborted = False
        truncated = False
        error: str | None = None
        answer = ""
        delta = Usage()
        try:
            answer = await child_task
            if not answer.strip():
                # 对应 Toonflow：子任务没返回文本结果直接报错，别让父 Agent 拿到空结论
                raise RuntimeError("子任务没有返回文本结果")
            if child.last_finish_reason in {"length", "max_turns"}:
                answer += "\n[模型输出或工具循环达到限制，任务可能未完成]"
                truncated = True
            if len(answer) > MAX_RESULT_CHARS:
                # 对应 Toonflow：结果过长截断，并提示父 Agent 去读工作区产物
                answer = answer[:MAX_RESULT_CHARS] + "\n[结果过长，已截断；请读取工作区产物]"
                truncated = True
        except asyncio.CancelledError:
            # 上层 task.cancel()（客户端断开）→ 立刻把取消传播给子（这就是级联）。
            # 注意：这里继续 raise，但**结算已经在 finally 里**，不会漏账。
            child_cancel.set()
            child_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, AgentAborted):
                await child_task
            aborted = True
            raise
        except AgentAborted:
            aborted = True
        except Exception as exc:  # 子任务失败不该炸掉父 Agent，转成错误结论喂回去
            error = f"{type(exc).__name__}: {exc}"
            answer = f"[子 Agent 执行失败] {error}"
        finally:
            watcher.cancel()
            forwarder.cancel()
            # ★ 结算与状态收尾必须放 finally：成功 / 中止 / 被取消 / 报错 每条路径
            #   都要回收"此刻已经真实产生"的用量，否则被取消的子任务就成了白烧的 token。
            delta = record.session.usage_since(offset)
            record.last_usage = delta
            record.error = error
            record.status = (
                "aborted" if aborted
                else "error" if error
                else "limited" if truncated
                else "done"
            )
            self._persist_state(record, task, delta=delta, truncated=truncated, aborted=aborted)
            self.parent.bus.emit(
                "sub_agent_end",
                session_id=self.parent.session.id,
                sub_agent=name,
                aborted=aborted,
                status=record.status,
            )
            self.parent.bus.emit(
                "sub_agent_usage",
                session_id=self.parent.session.id,
                sub_agent=name,
                **delta.as_dict(),
                since=offset,
            )

        return DelegationResult(
            name=name,
            answer=answer,
            usage=delta,
            usage_all=record.session.usage_total(),
            reports=list(record.reports),
            aborted=aborted,
            status=record.status,
        )

    # ------------------------------------------------------------------
    async def _forward_events(self, child_bus: EventBus, name: str) -> None:
        """把子 Agent 的事件打标后转发到父总线（对应 delegation.ts 的 subAgentEvent）。"""
        q = child_bus.subscribe()
        try:
            while True:
                event = await q.get()
                self.parent.bus.forward(event, sub_agent=name)
        finally:
            child_bus.unsubscribe(q)

    async def _watch_parent(self, child_cancel: asyncio.Event, child_task: asyncio.Task) -> None:
        """父的取消 Event 一触发，就取消子任务。"""
        parent_cancel = self.parent.cancel
        if parent_cancel is None:
            return
        await parent_cancel.wait()
        child_cancel.set()
        child_task.cancel()
