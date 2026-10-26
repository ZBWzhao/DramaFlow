"""改稿脑（阶段 2 Task 8）：带工具环的管线改稿助手 —— 提案过审批，落稿过体检。

三拍：提修改（agent loop 里的 propose_edit）→ 审批（用户在审批卡上点应用，
服务端审批端点才落库）→ 自动体检（script 落稿后自动重跑真质量门，门不过自动
回滚，不留脏数据）。

安全红线（brief 逐字）：
- agent 的工具只有 read_artifact / propose_edit / finish，**没有 apply 工具**——
  模型永远无法直接改产物；落稿只有 POST /api/pipeline/projects/{canvas}/editor/apply
  一个口，且该口先落库再对 script 重跑 10 道门（skill_executor.run_gate_only），
  门不过回滚重写旧 content。
- pending_edits 是服务端内存 dict（键 = 项目 canvas），10 分钟 TTL；重复 apply /
  过期 apply 一律 KeyError（端点转 404）。

为什么落稿走 pipeline_import.prepare 而不是裸 save：与阶段执行器同一份合并契约
（script 按 episodes[].ep 分批合并、characters 逐卡并入、outline latest-wins 整行
替换）——改稿脑不是第二条写入口径，只是审批闸门后面复用同一条落库路。

事件流（chat_stream 的 yield）：{"type":"tool_start"/"tool_end"/"assistant_text"}，
server/app.py 的 NDJSON 分支原样映射成 AiPanel 的工具卡/正文事件。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

from . import pipeline_artifacts, pipeline_import, pipeline_repository, skill_executor
from .pipeline_import import PipelineImportError

# 工具清单（brief 逐字）：没有 apply —— 落稿只经审批端点。
EDITOR_TOOLS = ("read_artifact", "propose_edit", "finish")

# 改稿环预算：max_iterations 的既定默认值保持 5（brief 未给编辑环另设值）。
MAX_ITERATIONS = 5

# pending 提案的 10 分钟 TTL（秒）；超时未审批自动失效。
EDIT_TTL_SECONDS = 600.0

# 改稿落库的来源标记（区别于阶段的 "stage:<stage>" / 导入的 "run:<id>"）。
EDITOR_SOURCE_RUN_ID = "editor"

# 工具回给模型的报告尾部预算（与执行器 GATE_TAIL 同量级）。
_TOOL_TAIL = 2000

# ---------------------------------------------------------------------------
# pending 提案账本（进程内内存；键 = 项目 canvas）
# ---------------------------------------------------------------------------

_PENDING: dict[str, dict[str, dict]] = {}
_HISTORY_CAP = 50   # 每项目保留的最近提案/结果条数（含已终结的），防内存无界


def _prune(project_id: str) -> None:
    """把过期 pending 标成 expired（保留在历史里给 UI 看），并裁掉超量历史。"""
    book = _PENDING.get(project_id)
    if not book:
        return
    now = time.time()
    for edit in book.values():
        if edit["status"] == "pending" and now - edit["proposed_at"] > EDIT_TTL_SECONDS:
            edit["status"] = "expired"
    if len(book) > _HISTORY_CAP:
        keep = sorted(book.values(), key=lambda e: e["proposed_at"])[-(_HISTORY_CAP - 1):]
        kept_ids = {e["edit_id"] for e in keep}
        for edit_id in [k for k in book if k not in kept_ids]:
            book.pop(edit_id, None)


def propose(project_id: str, artifact_type: str, summary: str, new_content: dict) -> dict:
    """记一条待审批提案；返回提案记录（含 edit_id）。"""
    book = _PENDING.setdefault(project_id, {})
    _prune(project_id)
    edit = {
        "edit_id": f"edit_{uuid.uuid4().hex[:12]}",
        "artifact_type": artifact_type,
        "summary": summary,
        "content": new_content,
        "proposed_at": time.time(),
        "status": "pending",
        "findings": [],
    }
    book[edit["edit_id"]] = edit
    return dict(edit)


def list_edits(project_id: str) -> list[dict]:
    """项目的提案流水（pending 在前，其余按时间倒序）；每条去掉正文只留预览。"""
    book = _PENDING.get(project_id)
    if not book:
        return []
    _prune(project_id)
    edits = sorted(book.values(), key=lambda e: e["proposed_at"], reverse=True)
    edits.sort(key=lambda e: 0 if e["status"] == "pending" else 1)
    return [_public_view(e) for e in edits]


def _public_view(edit: dict) -> dict:
    preview = json.dumps(edit["content"], ensure_ascii=False)
    return {
        "edit_id": edit["edit_id"],
        "artifact_type": edit["artifact_type"],
        "summary": edit["summary"],
        "proposed_at": edit["proposed_at"],
        "status": edit["status"],
        "findings": list(edit.get("findings") or []),
        "content_chars": len(preview),
        "preview": preview[:200],
    }


def _take_pending(project_id: str, edit_id: str) -> dict:
    """取走一条 pending 提案（同步先出账，天然防并发双 apply）；不在/过期 → KeyError。"""
    book = _PENDING.get(project_id) or {}
    edit = book.get(edit_id)
    _prune(project_id)
    if edit is None or edit["status"] != "pending":
        raise KeyError(f"提案不存在或已失效：{edit_id}")
    edit["status"] = "applying"
    return edit


def reject(project_id: str, edit_id: str) -> dict:
    """驳回一条 pending 提案；不在/非 pending → KeyError。"""
    book = _PENDING.get(project_id) or {}
    edit = book.get(edit_id)
    _prune(project_id)
    if edit is None or edit["status"] != "pending":
        raise KeyError(f"提案不存在或已失效：{edit_id}")
    edit["status"] = "rejected"
    return dict(edit)


# ---------------------------------------------------------------------------
# 落稿 + 自动体检（审批端点的实现体；模型侧没有任何入口能调到这里）
# ---------------------------------------------------------------------------

def _stage_spec_for(artifact_type: str) -> dict:
    for spec in skill_executor.STAGE_SPECS.values():
        if spec["artifact"] == artifact_type:
            return spec
    raise KeyError(f"未知产物类型：{artifact_type}（STAGE_SPECS 无对应阶段）")


async def _gate_upstreams(db, project_id: str, artifact_type: str) -> dict:
    """体检的 upstreams：按阶段规格的 gate_needs 从产物库取已落库上游。"""
    spec = _stage_spec_for(artifact_type)
    upstreams: dict = {}
    for need in spec.get("gate_needs") or []:
        row = await pipeline_repository.get(db, project_id, need)
        if row is not None:
            upstreams[need] = row["content"]
    return upstreams


async def apply_pending_edit(db, project_id: str, edit_id: str) -> dict:
    """审批落稿：校验 pending → prepare 合并 → save(source_run_id="editor")
    → script 再自动重跑真质量门；门不过回滚重写旧 content（无旧产物则删行），
    不留脏数据。返回 {ok, rolled_back, artifact_type, findings, gate?}。"""
    edit = _take_pending(project_id, edit_id)
    art = pipeline_artifacts.get(edit["artifact_type"])

    current = await pipeline_repository.get(db, project_id, art.name)
    baseline = current["content"] if current is not None else None
    baseline_source = current["source_run_id"] if current is not None else ""

    try:
        # 与阶段执行器/导入路由同一份合并契约：script 按 ep 并入（提案可以只发要改
        # 的集）、characters 逐卡并入、outline latest-wins 整行替换。
        prepared, findings = pipeline_import.prepare(art, edit["content"], baseline)
    except PipelineImportError as exc:
        edit["status"] = "failed"
        edit["findings"] = [{"gate": "import", "detail": exc.message}]
        return {"ok": False, "rolled_back": False, "artifact_type": art.name,
                "message": exc.message, "findings": exc.findings or [exc.message]}

    await pipeline_repository.save(db, project_id, art.name, prepared,
                                   source_run_id=EDITOR_SOURCE_RUN_ID)

    if art.name == "script":
        # 自动体检：真门（node 子进程）对刚落库的 content 全量重查。
        # 第三元 rules（哨兵规则清单）体检端暂不消费——分级判定只在阶段执行器
        # 生效，体检维持「ok 驱动回滚」的全 blocking 语义。
        ok, output, _rules = await skill_executor.run_gate_only(
            art.name, prepared, await _gate_upstreams(db, project_id, art.name))
        if not ok:
            if baseline is None:
                await pipeline_repository.delete(db, project_id, art.name)
            else:
                # 回滚保留原 provenance：写回旧 content 与旧 source_run_id。
                await pipeline_repository.save(db, project_id, art.name, baseline,
                                               source_run_id=baseline_source)
            edit["status"] = "failed"
            edit["findings"] = [{"gate": "validate", "detail": output[-_TOOL_TAIL:]}]
            return {"ok": False, "rolled_back": True, "artifact_type": art.name,
                    "gate": {"ok": False, "report_tail": output[-_TOOL_TAIL:]},
                    "findings": list(edit["findings"])}
        edit["status"] = "applied"
        edit["findings"] = []
        return {"ok": True, "rolled_back": False, "artifact_type": art.name,
                "findings": list(findings),
                "gate": {"ok": True, "report_tail": output[-_TOOL_TAIL:]}}

    edit["status"] = "applied"
    return {"ok": True, "rolled_back": False, "artifact_type": art.name,
            "findings": list(findings), "gate": None}


# ---------------------------------------------------------------------------
# 改稿环（自带工具环；复用 skill_executor 的 LLMPort 口径）
# ---------------------------------------------------------------------------

_ALLOWED_TYPES = tuple(
    spec["artifact"] for spec in skill_executor.STAGE_SPECS.values()
)


def _tool_specs() -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": "read_artifact",
            "description": "读取项目当前某类管线产物的 JSON 文本（落库的最新版）。",
            "parameters": {
                "type": "object",
                "properties": {"type": {"type": "string", "enum": list(_ALLOWED_TYPES),
                                        "description": "产物类型"}},
                "required": ["type"],
            },
        }},
        {"type": "function", "function": {
            "name": "propose_edit",
            "description": "提交一次改稿提案：进入待审批列表，等待用户在审批卡上批准。"
                           "批准前产物不会被修改；你没有任何直接落稿的工具。"
                           "script 的 new_content 可只发要改的集（episodes 按 ep 分批合并），"
                           "outline 是整篇替换（全量给）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(_ALLOWED_TYPES),
                             "description": "要修改的产物类型"},
                    "summary": {"type": "string", "description": "一句话说清改了什么、为什么"},
                    "new_content": {"description": "该产物的完整 JSON 数据本体（对象），"
                                                   "不是再编码过的 JSON 字符串"},
                },
                "required": ["type", "summary", "new_content"],
            },
        }},
        {"type": "function", "function": {
            "name": "finish",
            "description": "结束本轮改稿对话；在同一条回复的正文里把结论写给用户。",
            "parameters": {"type": "object", "properties": {}},
        }},
    ]


def _compose_system(project_id: str, inventory: list[dict]) -> str:
    """改稿脑系统提示：只读 + 提案边界（红线口径），外加产物清单与提案语义。"""
    rows = "\n".join(
        f"- {row['artifact_type']}：{'有（' + str(row['updated_at']) + ' 落库）' if row['present'] else '无'}"
        for row in inventory
    ) or "（该项目还没有任何管线产物）"
    return f"""你在 DramaFlow 的改稿脑里服务项目「{project_id}」的管线改稿对话。

## 当前管线产物清单
{rows}

## 边界（红线，逐条遵守）
1. 你只有三个工具：read_artifact / propose_edit / finish。没有写入产物、执行脚本、
   生成媒体等任何其他能力。
2. 你不能直接修改任何产物。propose_edit 只是把提案放进「待审批」列表；真正落稿
   必须由用户在审批卡上点「应用」，服务端落稿后会对 script 自动重跑质量门，
   门不过会自动回滚。绝不向用户声称已经改好了产物。
3. 修改前先 read_artifact 看当前内容；提案的 new_content 给该产物的 JSON 数据本体
   （对象），绝不要把 JSON 再编码成字符串。
4. 每条提案 summary 用一句话说清改了什么、为什么；一轮可以并行发多个工具调用。
5. 改稿结论写进回复正文，并调用 finish 结束本轮；没有需要动产物的问题就直接
   用正文回答（不调工具也是合法的一轮）。
6. 产物字段口径以内容现状为准，保守修改：不删既有集/卡，不发明上游没有的角色。"""


class PipelineEditor:
    """改稿脑实例：持有 LLMPort 与按 session 的对话记忆（进程内）。"""

    def __init__(self, db, *, llm: Any, max_iterations: int = MAX_ITERATIONS,
                 history_cap: int = 30) -> None:
        self.db = db
        self.llm = llm
        self.max_iterations = max_iterations
        self._sessions: dict[str, list[dict]] = {}
        self._history_cap = history_cap   # 每会话保留的最近消息条数（不含 system）

    async def _read_artifact(self, project_id: str, artifact_type: str) -> str:
        try:
            pipeline_artifacts.get(artifact_type)
        except KeyError:
            return f"错误：未知产物类型 {artifact_type}。可选：{'、'.join(_ALLOWED_TYPES)}"
        row = await pipeline_repository.get(self.db, project_id, artifact_type)
        if row is None:
            return f"该项目还没有 {artifact_type} 产物。"
        # 全文不截断：read_artifact 是改稿脑唯一的"眼睛"，截断会让模型对着
        # 半份产物提修改（与阶段执行器把上游全文塞进 user 消息同一理由）。
        return json.dumps(row["content"], ensure_ascii=False, indent=2)

    async def _dispatch(self, project_id: str, name: str, args: dict) -> tuple[bool, str]:
        """返回 (ok, 给模型的工具结果)。"""
        if name == "read_artifact":
            return True, await self._read_artifact(project_id, str(args.get("type", "")))
        if name == "propose_edit":
            artifact_type = str(args.get("type", ""))
            if artifact_type not in _ALLOWED_TYPES:
                return False, (f"错误：未知产物类型 {artifact_type}。"
                               f"可选：{'、'.join(_ALLOWED_TYPES)}")
            summary = str(args.get("summary", "")).strip()
            if not summary:
                return False, "错误：summary 不能为空——一句话说清改了什么、为什么。"
            new_content = args.get("new_content")
            if not isinstance(new_content, dict):
                return False, ("错误：new_content 必须是产物 JSON 数据本体（对象），"
                               + ("不要把 JSON 再编码成字符串，直接给对象。"
                                  if isinstance(new_content, str)
                                  else "按该产物 schema.md 的顶层结构给对象。"))
            edit = propose(project_id, artifact_type, summary, new_content)
            return True, (f"已提案，等待用户审批（edit_id={edit['edit_id']}，"
                          f"产物 {artifact_type}）。批准前产物不会被修改；"
                          "批准后服务端落稿，script 会自动重跑质量门。")
        if name == "finish":
            return True, "ok"
        return False, f"未知工具 {name}（可选：{'、'.join(EDITOR_TOOLS)}）"

    async def _inventory_async(self, project_id: str) -> list[dict]:
        rows = await pipeline_repository.list_all(self.db, project_id)
        present = {r["artifact_type"]: r["updated_at"] for r in rows}
        return [{"artifact_type": name, "present": name in present,
                 "updated_at": present.get(name)} for name in _ALLOWED_TYPES]

    async def chat_stream(self, project_id: str, session_id: str,
                          user_text: str) -> AsyncIterator[dict]:
        """跑一轮改稿对话，逐事件 yield（tool_start/tool_end/assistant_text）。

        一轮 = 一次 llm.complete + 顺序执行该轮全部工具调用；finish 或「无工具
        调用的纯文本回答」结束本轮。对话记忆按 session 存进程内（裁剪到最近
        _history_cap 条），system 每轮重建（产物清单保持新鲜）。
        """
        messages: list[dict] = [{"role": "system",
                                 "content": _compose_system(
                                     project_id, await self._inventory_async(project_id))}]
        messages += self._sessions.get(session_id, [])
        messages.append({"role": "user", "content": user_text})

        text = ""
        finished = False
        for _round in range(1, self.max_iterations + 1):
            resp = await self.llm.complete(messages, tools=_tool_specs())
            calls = list(getattr(resp, "tool_calls", None) or [])
            text = getattr(resp, "text", "") or ""
            assistant: dict[str, Any] = {"role": "assistant", "content": text}
            if calls:
                assistant["tool_calls"] = [
                    {"id": str(call.get("id") or f"call_{_round}_{i}"),
                     "type": "function",
                     "function": {"name": call["name"],
                                  "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False)}}
                    for i, call in enumerate(calls)
                ]
            messages.append(assistant)
            if not calls:
                finished = True   # 纯文本回答：合法收尾
                break
            for i, call in enumerate(calls):
                call_id = str(call.get("id") or f"call_{_round}_{i}")
                yield {"type": "tool_start", "tool_call_id": call_id,
                       "name": call["name"], "args": call.get("args") or {}}
                started = time.monotonic()
                ok, out = await self._dispatch(project_id, call["name"], call.get("args") or {})
                yield {"type": "tool_end", "tool_call_id": call_id, "name": call["name"],
                       "ok": ok, "result_tail": out[-_TOOL_TAIL:],
                       "ms": int((time.monotonic() - started) * 1000)}
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "name": call["name"], "content": out})
                if call["name"] == "finish":
                    finished = True
            if finished:
                break

        if not finished:
            text = (text + "\n\n（本轮改稿预算已用完，先到这里；请继续对话或重新描述需求。）").strip()

        # 会话记忆：整段**赋值**回写（不能 extend —— messages[1:] 里已经含着取出来的
        # 旧历史，再 extend 会把上一轮历史整段复制一遍，逐轮平方膨胀）。
        # 本轮 user 消息也在 messages[1:] 里，下一轮的上下文需要它。
        next_history = [m for m in messages[1:] if m.get("role") != "system"]
        del next_history[:-self._history_cap]
        self._sessions[session_id] = next_history

        yield {"type": "assistant_text", "text": text}

    async def chat(self, project_id: str, session_id: str, user_text: str) -> dict:
        """chat_stream 的聚合版（测试/内部用）：返回最终文本与事件流水。"""
        events: list[dict] = []
        text = ""
        async for event in self.chat_stream(project_id, session_id, user_text):
            events.append(event)
            if event["type"] == "assistant_text":
                text = event["text"]
        return {"text": text, "events": events}
