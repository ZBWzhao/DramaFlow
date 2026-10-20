"""会话注册表（服务端）。

对照 Toonflow：apps/server/src/agent/runtime/sessions.ts
  · registerAgentSession / getAgentSession  →  get_or_create
  · 用户点停止                              →  cancel()（Event + task.cancel）
  · getAgentStats                           →  stats()

关键设计（对应 T02-03 steer）：同一个 session_id 的第二次请求**复用会话**，
而不是新开一个 —— 多轮上下文连续，代价是历史越来越长、后续请求 token 更贵。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.agent import Agent, build_system_prompt
from core.compaction import estimate_context_tokens
from core.canvas_repository import CanvasRepository, create_canvas_repository
from core.context import ToolContext
from core.events import EventBus
from core.llm import LLM
from core.invocation_store import ApprovalStore, ToolInvocationStore, create_invocation_stores
from core.media_asset_repository import MediaAssetRepository, create_media_asset_repository
from core.loader import TOOLS_ROOT, create_agent_tools, reload_tool
from core.mcp import MCPManager
from core.outbox import PostgresRedisOutboxPublisher, create_outbox_publisher
from core.production_repository import ProductionRunRepository, create_production_run_repository
from core.questions import QuestionBroker, build_question_tool
from core.question_store import QuestionStateStore, create_question_store
from core.runtime_store import (
    EventStore,
    RunState,
    RunStateStore,
    create_event_store,
    create_run_state_store,
)
from core.session import SESSIONS_DIR, Session
from core.session_repository import (
    PostgresSessionRepository,
    SessionRepository,
    create_session_repository,
)
from core.skills import SKILLS_ROOT, build_skill_tools, discover_skills
from core.subagent import DelegationManager, restore_subagents

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CUSTOM_SESSION_META = "sessionMeta"


@dataclass
class LiveSession:
    id: str
    agent: Agent
    bus: EventBus
    cancel: asyncio.Event
    manager: DelegationManager
    questions: QuestionBroker
    created_at: float = field(default_factory=time.time)
    task: asyncio.Task | None = None
    run_kind: str | None = None
    current_run_id: str | None = None
    runs: int = 0
    # 完整工具集（含 subagent 与技能工具）。**单次请求的开关只从这里派生**，
    # 绝不就地改 agent.tools —— 否则一次 use_sub_agent=False 会把这个会话
    # 之后的每次请求都变成"没有 subagent 工具"（开关粘住）。
    full_tools: list[Any] = field(default_factory=list)

    def tools_for(self, include_sub_agent: bool = True, allow_media_generation: bool = False) -> list[Any]:
        """按本次请求的需要给出工具列表（不修改任何会话状态）。"""
        tools = list(self.full_tools) if include_sub_agent else [t for t in self.full_tools if t.name != "subagent"]
        if not allow_media_generation:
            tools = [t for t in tools if t.name not in {"generate_media", "run_canvas"}]
        return tools


class SessionRegistry:
    def __init__(
        self,
        llm: LLM,
        workspace: str,
        event_store: EventStore | None = None,
        run_states: RunStateStore | None = None,
        question_states: QuestionStateStore | None = None,
        invocations: ToolInvocationStore | None = None,
        approvals: ApprovalStore | None = None,
        canvas_repository: CanvasRepository | None = None,
        session_repository: SessionRepository | None = None,
        media_assets: MediaAssetRepository | None = None,
        production_runs: ProductionRunRepository | None = None,
    ) -> None:
        self.llm = llm
        self.workspace = workspace
        self.tools: list[Any] = []
        self.base_context: ToolContext | None = None
        self.sessions: dict[str, LiveSession] = {}
        self.skills: list[Any] = []
        self.skill_errors: list[str] = []
        self.event_store = event_store or create_event_store()
        self.run_states = run_states or create_run_state_store()
        self.question_states = question_states or create_question_store()
        default_invocations, default_approvals = create_invocation_stores()
        self.invocations = invocations or default_invocations
        self.approvals = approvals or default_approvals
        self.canvas_repository = canvas_repository or create_canvas_repository(Path(workspace))
        self.session_repository = session_repository or create_session_repository(SESSIONS_DIR)
        self.media_assets = media_assets or create_media_asset_repository()
        self.production_runs = production_runs or create_production_run_repository(
            Path(workspace).resolve().parent / ".agent" / "production-runs"
        )
        self.outbox_publisher: PostgresRedisOutboxPublisher | None = None
        if isinstance(self.session_repository, PostgresSessionRepository):
            self.outbox_publisher = create_outbox_publisher(self.session_repository.database)
        # MCP 配置属于项目而不是工作区内容；缺少配置文件时保持空插件集。
        self.mcp = MCPManager(Path(workspace).resolve().parent / ".agent" / "mcp.json")

    async def ainit(self) -> None:
        """装载工具插件与技能清单（只需一次，对应服务启动时的 initializePlugins）。"""
        await self.event_store.start()
        await self.run_states.start()
        await self.question_states.start()
        await self.invocations.start()
        await self.approvals.start()
        await self.canvas_repository.start()
        await self.session_repository.start()
        await self.media_assets.start()
        await self.production_runs.start()
        if self.outbox_publisher is not None:
            await self.outbox_publisher.start()
        tools, _, ctx = await create_agent_tools(
            cwd=self.workspace, root=TOOLS_ROOT, canvas_repository=self.canvas_repository
        )
        await self.mcp.start()
        remote_tools = self.mcp.tool_definitions()
        local_names = {tool.name for tool in tools}
        collisions = [tool.name for tool in remote_tools if tool.name in local_names]
        if collisions:
            raise ValueError(f"MCP 工具与本地工具重名：{', '.join(collisions)}")
        self.tools = [*tools, *remote_tools]
        self.base_context = ctx
        # 技能只装载**清单**，正文等模型调用 read_skill 时再读
        self.skills, self.skill_errors = discover_skills(SKILLS_ROOT)

    async def aclose(self) -> None:
        """Close external MCP transports during application shutdown."""
        for live in list(self.sessions.values()):
            await live.bus.close()
        await self.mcp.close()
        await self.event_store.close()
        await self.run_states.close()
        await self.question_states.close()
        await self.invocations.close()
        await self.approvals.close()
        await self.canvas_repository.close()
        # Drain synchronous JSONL -> PostgreSQL projection writes before stopping
        # the publisher, otherwise shutdown could leave avoidable pending rows.
        await self.session_repository.flush()
        if self.outbox_publisher is not None:
            await self.outbox_publisher.close()
        await self.session_repository.close()
        await self.media_assets.close()
        await self.production_runs.close()

    # ------------------------------------------------------------------
    def session_path(self, session_id: str) -> Any:
        """校验客户端会话标识，并保证最终路径仍位于会话目录内。"""
        if not SESSION_ID_PATTERN.fullmatch(session_id or ""):
            raise ValueError("会话 ID 无效")
        root = SESSIONS_DIR.resolve()
        path = (root / f"{session_id}.jsonl").resolve()
        if path.parent != root:
            raise ValueError("会话路径越界")
        return path

    def get_or_create(self, session_id: str | None = None) -> LiveSession:
        if session_id and session_id in self.sessions:
            return self.sessions[session_id]

        sid = session_id or uuid.uuid4().hex[:8]
        self.session_path(sid)  # 新建和恢复都走同一信任边界
        bus = EventBus(store=self.event_store, session_id=sid)
        cancel = asyncio.Event()
        # 会话落盘：一条 entry 一行 JSONL，压缩前的原始对话也留在文件里。
        # 文件已存在就 load —— 服务重启后同一个 session_id 还能接着聊（对应 sessions.ts 的 getAgentSession）。
        path = self.session_path(sid)
        session = self.session_repository.get_or_create(sid, path)
        # 运行上下文必须继承启动阶段注入的 media/question/canvas 等能力。
        # 之前这里只复制写锁，导致工具虽然已加载，执行时 require_media() 却失败。
        ctx = self.base_context.derive() if self.base_context is not None else ToolContext(cwd=self.workspace)
        ctx.config = {**ctx.config, "agent_name": "main"}

        agent = Agent(
            llm=self.llm,
            session=session,
            tools=list(self.tools),
            context=ctx,
            bus=bus,
            cancel=cancel,
            invocations=self.invocations,
        )
        manager = DelegationManager(
            parent=agent,
            llm=self.llm,
            cwd=self.workspace,
            skills=self.skills,
        )
        questions = QuestionBroker(
            session,
            bus,
            on_status=manager.mark_question_status,
            store=self.question_states,
        )
        ctx.question = questions
        # 循环依赖的解法：先有 agent 才能建 manager。技能读取工具也交给委派
        # 管理器，子 Agent 才能按系统规则读取 workflow/drama_craft 等技能。
        skill_tools = build_skill_tools(SKILLS_ROOT)
        question_tool = build_question_tool()
        agent.tools.extend(skill_tools)
        agent.tools.append(question_tool)
        agent.tools.append(manager.parent_tool([*self.tools, *skill_tools, question_tool]))
        ctx.tool_names = {tool.name for tool in agent.tools}
        session.system_prompt = build_system_prompt(self.workspace, agent.tools, self.skills)

        live = LiveSession(
            id=sid,
            agent=agent,
            bus=bus,
            cancel=cancel,
            manager=manager,
            questions=questions,
            full_tools=list(agent.tools),
        )
        self.sessions[sid] = live
        return live

    def bind_canvas(self, live: LiveSession, canvas: str, title: str = "") -> None:
        """把会话归属写进 JSONL；画布切换和进程重启后都能恢复。"""
        canvas = (canvas or "main").strip()[:120]
        # 工具执行时从 context 读取当前画布。只把归属写进 sessionMeta 不够：
        # 团队/子 Agent 会从父 Context 派生运行上下文，如果这里不更新，
        # 它们会继续把节点写进服务启动时的默认画布（通常是 main）。
        live.agent.context.config["default_canvas"] = canvas
        latest = live.agent.session.customs_of(CUSTOM_SESSION_META)
        current = latest[-1] if latest else {}
        if current.get("canvas") == canvas and (not title or current.get("title") == title):
            return
        live.agent.session.append_custom(
            CUSTOM_SESSION_META,
            {"canvas": canvas, "title": (title or current.get("title") or "").strip()[:80]},
        )

    def create_session(self, canvas: str = "main", title: str = "") -> dict[str, Any]:
        live = self.get_or_create()
        self.bind_canvas(live, canvas, title)
        return self.session_detail(live.id)

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        value = str(title or "").strip()[:80]
        if not value:
            raise ValueError("会话名称不能为空")
        live = self.get_or_create(session_id)
        meta = self._meta(live.agent.session)
        self.bind_canvas(live, meta.get("canvas") or "main", value)
        return self.session_detail(session_id)

    def delete_session(self, session_id: str) -> list[str]:
        """删除主会话和它的子 Agent 文件；只接受已通过边界校验的精确 ID。"""
        self.session_path(session_id)
        live = self.sessions.get(session_id)
        if live and live.task and not live.task.done():
            raise RuntimeError("运行中的会话不能删除，请先停止")
        deleted = self.session_repository.delete(session_id)
        self.sessions.pop(session_id, None)
        return deleted

    @staticmethod
    def _meta(session: Session) -> dict[str, Any]:
        entries = session.customs_of(CUSTOM_SESSION_META)
        return entries[-1] if entries else {}

    @staticmethod
    def _active_message_entries(session: Session) -> list[dict[str, Any]]:
        """恢复当前分支的完整可见历史；compaction 只影响模型上下文，不隐藏旧对话。"""
        active: list[dict[str, Any]] = []
        entries = session.entries()
        all_by_id = {
            item["id"]: item for item in entries
            if item.get("type") == "message" and item.get("id")
        }

        def lineage(entry_id: str) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            seen: set[str] = set()
            cursor: str | None = entry_id
            while cursor and cursor in all_by_id and cursor not in seen:
                seen.add(cursor)
                item = all_by_id[cursor]
                out.append(item)
                cursor = item.get("parent")
            out.reverse()
            return out

        for entry in entries:
            if entry.get("type") == "message":
                active.append(entry)
            elif entry.get("type") == "branch":
                from_id = entry.get("from")
                if not from_id:
                    active = []
                    continue
                index = next((i for i, item in enumerate(active) if item.get("id") == from_id), -1)
                active = active[: index + 1] if index >= 0 else lineage(from_id)
        return active

    @staticmethod
    def _all_message_entries(session: Session) -> list[dict[str, Any]]:
        return [item for item in session.entries() if item.get("type") == "message" and item.get("id")]

    @classmethod
    def _feed(cls, session: Session) -> list[dict[str, Any]]:
        messages = cls._active_message_entries(session)
        all_messages = cls._all_message_entries(session)
        siblings: dict[str | None, list[str]] = {}
        for item in all_messages:
            if item.get("role") == "user" and item.get("content") != "[之前的对话摘要]":
                siblings.setdefault(item.get("parent"), []).append(item["id"])
        tool_results = {
            item.get("tool_call_id"): item
            for item in messages
            if item.get("role") == "tool" and item.get("tool_call_id")
        }
        feed: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role == "user":
                if content == "[之前的对话摘要]":
                    continue
                branch_ids = siblings.get(message.get("parent"), [])
                feed.append({
                    "type": "user",
                    "entryId": message.get("id"),
                    "parentId": message.get("parent"),
                    "text": content if isinstance(content, str) else str(content or ""),
                    "branches": branch_ids,
                    "branchIndex": branch_ids.index(message.get("id")) if message.get("id") in branch_ids else 0,
                })
                continue
            if role != "assistant":
                continue
            parts: list[dict[str, Any]] = []
            reasoning = message.get("reasoning_content")
            if reasoning:
                parts.append({"kind": "thinking", "text": reasoning, "streaming": False, "expanded": False})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                result = tool_results.get(call.get("id"))
                result_text = result.get("content") if result else None
                parts.append(
                    {
                        "kind": "tool",
                        "id": call.get("id"),
                        "name": function.get("name") or "tool",
                        "args": function.get("arguments") or "{}",
                        "status": "error" if isinstance(result_text, str) and result_text.startswith("Error:") else "success" if result else "interrupted",
                        "result": result_text,
                        "resultChars": len(result_text) if isinstance(result_text, str) else 0,
                        "durationMs": None,
                        "details": None,
                        "canvas": None,
                        "expanded": False,
                    }
                )
            if isinstance(content, str) and content:
                parts.append({"kind": "text", "text": content, "streaming": False})
            if parts:
                feed.append({
                    "type": "assistant",
                    "entryId": message.get("id"),
                    "parentId": message.get("parent"),
                    "parts": parts,
                    "streaming": False,
                })
        return feed

    def list_sessions(self, canvas: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for stored_session in self.session_repository.all():
            sid = stored_session.id
            if not SESSION_ID_PATTERN.fullmatch(sid):
                continue  # 子 Agent 文件含点号，不出现在主会话列表
            try:
                session = self.sessions[sid].agent.session if sid in self.sessions else stored_session
                meta = self._meta(session)
                bound_canvas = meta.get("canvas")
                # 项目会话必须严格隔离。未绑定画布的旧测试会话不能出现在
                # 任意项目的下拉框中，否则 read_file / acceptance 等测试记录
                # 会和真实短剧会话混在一起。调用方不传 canvas 时仍可查看全部
                # 会话，便于维护和迁移旧数据。
                if canvas is not None and bound_canvas != canvas:
                    continue
                first_user = next(
                    (m.get("content", "") for m in self._active_message_entries(session) if m.get("role") == "user" and m.get("content") != "[之前的对话摘要]"),
                    "",
                )
                rows.append(
                    {
                        "id": sid,
                        "title": meta.get("title") or str(first_user).strip()[:40] or "新对话",
                        "canvas": bound_canvas,
                        "messages": sum(1 for m in self._active_message_entries(session) if m.get("role") in {"user", "assistant"}),
                        "updated_at": max(
                            (float(item.get("ts") or 0) for item in session.entries()),
                            default=0.0,
                        ),
                        "running": bool(sid in self.sessions and self.sessions[sid].task and not self.sessions[sid].task.done()),
                    }
                )
            except (OSError, ValueError):
                continue
        return sorted(rows, key=lambda item: item["updated_at"], reverse=True)

    def session_detail(self, session_id: str) -> dict[str, Any]:
        live = self.get_or_create(session_id)
        session = live.agent.session
        run_records = session.customs_of("runCheckpoint")
        last_run = run_records[-1] if run_records else None
        if last_run is None:
            # 兼容升级前的会话：正常完成一定以“无工具调用的 assistant 正文”收尾；
            # 若活动分支最后停在 tool（或仍带 tool_calls 的 assistant），说明旧运行被轮次上限截断。
            active_messages = self._active_message_entries(session)
            tail = active_messages[-1] if active_messages else None
            if tail and (
                tail.get("role") == "tool"
                or (tail.get("role") == "assistant" and tail.get("tool_calls"))
            ):
                last_run = {
                    "status": "limited",
                    "reason": "legacy_incomplete_tool_chain",
                    "turn": None,
                    "max_turns": None,
                    "last_entry_id": tail.get("id"),
                    "resume_instruction": (
                        "继续当前任务：先读取当前画布和已有产物，核对未完成项；"
                        "不要重复创建已有节点，只补齐缺失内容并最终给出交付总结。"
                    ),
                }
        return {
            "id": live.id,
            "meta": self._meta(session),
            "feed": self._feed(session),
            "stats": self.stats(live.id),
            "sub_agents": self.subagents(live.id),
            "pending_questions": live.questions.snapshot(),
            "running": bool(live.task and not live.task.done()),
            "last_run": last_run,
        }

    async def session_detail_async(self, session_id: str) -> dict[str, Any]:
        detail = self.session_detail(session_id)
        questions = await self.question_states.list_pending(session_id)
        detail["pending_questions"] = [item.to_public() for item in questions]
        active = await self.run_states.active_run(session_id)
        detail["running"] = bool(active) or detail["running"]
        detail["production_run"] = await self.production_runs.latest_for_session(session_id)
        return detail

    async def answer_question(self, session_id: str, question_id: str, answer: str) -> dict[str, Any]:
        """答案先原子写共享存储；执行实例的 Broker 会立即或轮询恢复 Future。"""
        state = await self.question_states.get(question_id)
        if state is None or state.session_id != session_id:
            raise KeyError("问题不存在")
        run = await self.run_states.get(state.run_id) if state.run_id else None
        stale_seconds = float(os.environ.get("RUN_STALE_SECONDS", "20"))
        if run is None or run.status != "running" or time.time() - run.heartbeat_at > stale_seconds:
            await self.question_states.cancel(session_id, question_id)
            raise RuntimeError("对应的 Agent 运行已结束或中断，不能再回答此问题")
        live = self.sessions.get(session_id)
        if live is not None and question_id in live.questions.pending:
            return await live.questions.answer(question_id, answer)
        answered = await self.question_states.answer(session_id, question_id, answer.strip()[:2000])
        return answered.to_public()

    def mutate_message(self, session_id: str, entry_id: str, action: str) -> dict[str, Any]:
        live = self.get_or_create(session_id)
        if live.task and not live.task.done():
            raise RuntimeError("运行中不能修改历史消息")
        all_messages = self._all_message_entries(live.agent.session)
        target = next((item for item in all_messages if item.get("id") == entry_id), None)
        if target is None:
            raise ValueError("消息不存在")
        if action == "delete":
            live.agent.session.checkout(target.get("parent"), reason="delete")
        elif action == "resend":
            if target.get("role") != "user":
                raise ValueError("只能重发用户消息")
            live.agent.session.checkout(target.get("parent"), reason="resend")
        elif action == "branch":
            by_id = {item["id"]: item for item in all_messages}

            def descends_from(item: dict[str, Any], ancestor: str) -> bool:
                cursor = item.get("id")
                seen: set[str] = set()
                while cursor and cursor in by_id and cursor not in seen:
                    if cursor == ancestor:
                        return True
                    seen.add(cursor)
                    cursor = by_id[cursor].get("parent")
                return False

            leaf = next(
                (item for item in reversed(all_messages) if descends_from(item, entry_id)),
                target,
            )
            live.agent.session.checkout(leaf.get("id"), reason="branch")
        else:
            raise ValueError("未知消息操作")
        return {
            "ok": True,
            "action": action,
            "prompt": target.get("content") if action == "resend" else None,
            "feed": self._feed(live.agent.session),
            "stats": self.stats(session_id),
        }

    # ------------------------------------------------------------------
    def begin_run(self, live: LiveSession, kind: str = "agent") -> str:
        """开始一次运行前的守卫。两件事都必须做：

        1. **同一会话不许并发运行**：两次运行会往同一个事件总线里交错发事件，
           前端会把两条回复混在一起；更糟的是同一个 Session 的历史会交错写入，
           可能破坏 `assistant(tool_calls)` 与 `tool` 的配对（provider 直接 400）。
        2. **清掉上一次的取消信号**：`cancel` 是会话级 Event，用户点过一次"停止"后
           它是置位状态。不清的话，这个会话**之后的每次运行都会在第一个检查点
           （agent.py 的 _check_abort）被立刻中止** —— 表现为"发什么都没反应/秒退"。
        """
        if live.task is not None and not live.task.done():
            raise RuntimeError(f"会话 {live.id} already running（已有一次运行未结束，请等待或先停止）")
        live.cancel.clear()
        run_id = uuid.uuid4().hex
        live.current_run_id = run_id
        live.run_kind = kind
        live.bus.run_id = run_id
        return run_id

    async def begin_persisted_run(self, live: LiveSession, kind: str, start_event_id: str) -> str:
        """原子占用会话运行权；Redis 模式下可阻止不同服务实例同时跑同一会话。"""
        if live.task is not None and not live.task.done():
            raise RuntimeError(f"会话 {live.id} already running（已有一次运行未结束，请等待或先停止）")
        run_id = uuid.uuid4().hex
        state = RunState(
            run_id=run_id,
            session_id=live.id,
            kind=kind,
            start_event_id=start_event_id,
        )
        if not await self.run_states.claim(state):
            raise RuntimeError(f"会话 {live.id} already running（另一服务实例正在处理）")
        live.cancel.clear()
        live.current_run_id = run_id
        live.run_kind = kind
        live.bus.run_id = run_id
        return run_id

    def cancel(self, session_id: str) -> bool:
        live = self.sessions.get(session_id)
        if live is None:
            return False
        live.cancel.set()  # 逻辑取消（循环检查点）
        if live.task and not live.task.done():
            live.task.cancel()  # 硬取消（打断正在进行的模型请求）
        return True

    async def cancel_persisted(self, session_id: str) -> bool:
        """先写共享取消标志，再尽快打断当前实例上的任务。"""
        active = await self.run_states.active_run(session_id)
        live = self.sessions.get(session_id)
        if active is None and not (live and live.task and not live.task.done()):
            return False
        if active is not None:
            await self.run_states.request_cancel(active.run_id)
        if live is not None:
            live.cancel.set()
            if live.task and not live.task.done():
                live.task.cancel()
        return True

    # ------------------------------------------------------------------
    def stats(self, session_id: str) -> dict[str, Any]:
        self.session_path(session_id)
        live = self.sessions.get(session_id)
        if live is None:
            return {}
        main = live.agent.session.usage_total()
        settings = getattr(live.agent.llm, "settings", None)
        context_window = getattr(settings, "context_window", 128000)
        context_tokens = estimate_context_tokens(
            live.agent.session,
            [tool.to_openai_schema() for tool in live.agent.tools],
        )
        return {
            "session_id": session_id,
            "main": main.as_dict(),
            "sub_agents": {
                name: {
                    "status": rec.status,
                    "last": rec.last_usage.as_dict(),
                    "cumulative": rec.session.usage_total().as_dict(),
                }
                for name, rec in live.manager.registry.items()
            },
            "messages": len(live.agent.session.messages),
            "runs": live.runs,
            "compactions": live.agent.session.compact_count,
            "chars": live.agent.session.size_chars(),
            "context_tokens": context_tokens,
            "context_window": context_window,
            "context_percent": round(context_tokens / context_window * 100, 1) if context_window else None,
            "file": str(live.agent.session.path) if live.agent.session.path else None,
            # 报出当前工具集：诊断"怎么少了某个工具"这类问题时一眼可见
            "tools": [t.name for t in live.agent.tools],
            "busy": bool(live.task and not live.task.done()),
            "dropped_events": live.bus.dropped,
        }

    def entries(self, session_id: str, limit: int = 60) -> list[dict[str, Any]]:
        self.session_path(session_id)
        live = self.sessions.get(session_id)
        if live is None:
            session = self.session_repository.get(session_id)
            if session is None:
                return []
            return session.entries()[-limit:]
        return live.agent.session.entries()[-limit:]

    def subagents(self, session_id: str) -> list[dict[str, Any]]:
        """子 Agent 视图：活会话读内存，进程重启后从父会话 JSONL 恢复。

        恢复时"文件里写着 running"的一律标成中断 —— 进程重启后它不可能还活着。
        """
        path = self.session_path(session_id)
        live = self.sessions.get(session_id)
        if live is not None:
            return [
                {
                    "name": rec.name,
                    "status": rec.status,
                    "file": rec.session.path.name if rec.session.path else None,
                    "last": rec.last_usage.as_dict(),
                    "cumulative": rec.session.usage_total().as_dict(),
                    "reports": rec.reports[-3:],
                    "error": rec.error,
                    "interrupted": False,
                }
                for rec in live.manager.registry.values()
            ]
        if not path.exists():
            return []
        return restore_subagents(Session.load(path))

    def reload(self, name: str) -> dict[str, Any]:
        _, metadata = reload_tool(name)
        return {"name": metadata.name, "display_name": metadata.display_name, "version": metadata.version}
