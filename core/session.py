"""会话：历史 + 用量记录 + JSONL 落盘。

对照 Toonflow：runtime/sessions.ts + Pi 的 JSONL 会话树（这里是单分支 + 落盘）。

★ 这个文件是模块二的地基：
  每次模型请求都往 usage_log 追加一条 Usage，于是"从某个偏移之后求和"
  就等于"本次新增的用量" —— 这正是 Toonflow subAgent.ts 里 previousEntries 技巧的 Python 版。

落盘格式（一行一条 entry，和 Pi 的 JSONL 一样可回放、可 diff）：
  {"type":"message", ...原消息}      —— 进上下文的消息
  {"type":"usage", ...}              —— 一次请求的用量
  {"type":"compaction","summary":…}  —— 压缩摘要（原始历史仍留在文件里）
  {"type":"custom","custom_type":…}  —— **不进上下文**的附加记录：子 Agent 状态、上报等
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .llm import Usage

SESSIONS_DIR = Path(__file__).resolve().parent.parent / ".agent" / "sessions"


@dataclass
class UsageEntry:
    """一条用量记录。label 用来区分是谁花的（main / sub:xxx / compact）。"""

    usage: Usage
    label: str = "main"
    seq: int = 0
    ts: float = field(default_factory=time.time)


class Session:
    def __init__(
        self,
        system_prompt: str = "",
        session_id: str | None = None,
        path: Path | None = None,
    ) -> None:
        self.id = session_id or uuid.uuid4().hex[:8]
        self.system_prompt = system_prompt
        self.messages: list[dict] = []
        self.usage_log: list[UsageEntry] = []
        self.compact_count = 0
        self.path = path
        # 会话树：每条消息一个 id，parent 指向上一条；分支就是"同一个 parent 有多个孩子"
        self.message_ids: list[str] = []
        self.current_id: str | None = None
        # custom entries：**不进上下文**的附加记录（对应 Toonflow 的 appendCustomEntry）
        self.customs: list[dict[str, Any]] = []

        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._write({"type": "session_start", "id": self.id, "cwd": str(self.path.parent)})

    # ---------------- 落盘 ----------------
    def _write(self, entry: dict[str, Any]) -> None:
        if self.path is None:
            return
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), **entry}, ensure_ascii=False) + "\n")

    def entries(self) -> list[dict[str, Any]]:
        """读出落盘的全部条目（压缩前的原始对话都还在，这就是 JSONL 的价值）。"""
        if self.path is None or not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def append_custom(self, custom_type: str, data: dict[str, Any], display: bool = False) -> str:
        """追加一条 custom entry（对应 Toonflow 的 appendCustomEntry）。

        **不进上下文**：它是给"人 / 前端 / 恢复逻辑"看的，不是给模型看的。
        子 Agent 的状态与上报就走这条路 —— 这样进程重启后还能还原出子 Agent 列表。
        """
        entry_id = self._new_id()
        self.customs.append({"custom_type": custom_type, "id": entry_id, "data": data})
        self._write(
            {
                "type": "custom",
                "custom_type": custom_type,
                "id": entry_id,
                "parent": self.current_id,
                "display": display,
                "data": data,
            }
        )
        return entry_id

    def customs_of(self, custom_type: str) -> list[dict[str, Any]]:
        return [c["data"] for c in self.customs if c.get("custom_type") == custom_type]

    @classmethod
    def load(cls, path: Path, system_prompt: str = "") -> "Session":
        """从 JSONL 恢复会话：消息与用量都能还原，压缩摘要不还原成消息。"""
        session = cls(system_prompt=system_prompt, session_id=path.stem)
        session.path = path
        if not path.exists():
            return session
        for entry in session.entries():
            kind = entry.get("type")
            if kind == "message":
                session.messages.append({k: v for k, v in entry.items() if k not in ("type", "ts", "id", "parent")})
                session.message_ids.append(entry.get("id") or session._new_id())
                session.current_id = entry.get("id")
            elif kind == "usage":
                session.usage_log.append(
                    UsageEntry(
                        usage=Usage(
                            input_tokens=entry.get("input_tokens", 0),
                            output_tokens=entry.get("output_tokens", 0),
                        ),
                        label=entry.get("label", "main"),
                        seq=len(session.usage_log),
                        ts=entry.get("ts", time.time()),
                    )
                )
            elif kind == "compaction":
                # 压缩恢复。注意文件里的**追加顺序**是 […原始…, 保留的 kept 条, 摘要条]，
                # 而逻辑顺序是"摘要 + 保留的 kept 条" —— 所以不能只做截断，要重建：
                #   · 保留的 kept 条 = 摘要条之前的那 kept 条
                #   · 摘要正文在 compaction 条目里（摘要那条 message 只写了占位文本）
                # 不这么做的话，重启后上下文会膨胀回压缩前（付过的压缩钱白花，还可能超窗口）。
                kept = int(entry.get("kept") or 0)
                summary_id = session.message_ids[-1] if session.message_ids else session._new_id()
                # 末尾那条是摘要自己，所以保留段是 [-(kept+1):-1]（不能把摘要算进"保留的 N 条"）
                kept_messages = session.messages[-(kept + 1) : -1] if kept else []
                kept_ids = session.message_ids[-(kept + 1) : -1] if kept else []
                session.messages = [
                    {"role": "user", "content": f"[之前的对话摘要]\n{entry.get('summary', '')}"}
                ] + kept_messages
                session.message_ids = [summary_id] + kept_ids
                session.current_id = session.message_ids[-1] if session.message_ids else None
                session.compact_count += 1
            elif kind == "branch":
                # 分叉 / 重发：截断到 from 为止（含 from）。form 为空表示截到空会话。
                from_id = entry.get("from")
                if from_id and from_id in session.message_ids:
                    cut = session.message_ids.index(from_id) + 1
                    session.messages = session.messages[:cut]
                    session.message_ids = session.message_ids[:cut]
                    session.current_id = from_id
                elif from_id:
                    # 允许切回已经不在当前活动分支里的历史节点。
                    all_messages = [
                        item for item in session.entries()
                        if item.get("type") == "message" and item.get("id")
                    ]
                    by_id = {item["id"]: item for item in all_messages}
                    lineage: list[dict[str, Any]] = []
                    seen: set[str] = set()
                    cursor = from_id
                    while cursor and cursor in by_id and cursor not in seen:
                        seen.add(cursor)
                        item = by_id[cursor]
                        lineage.append(item)
                        cursor = item.get("parent")
                    lineage.reverse()
                    visible = [item for item in lineage if item.get("content") != "[之前的对话摘要]"]
                    session.messages = [
                        {k: v for k, v in item.items() if k not in ("type", "ts", "id", "parent")}
                        for item in visible
                    ]
                    session.message_ids = [item["id"] for item in visible]
                    session.current_id = session.message_ids[-1] if session.message_ids else None
                else:
                    session.messages = []
                    session.message_ids = []
                    session.current_id = None
            elif kind == "custom":
                session.customs.append(
                    {
                        "custom_type": entry.get("custom_type"),
                        "id": entry.get("id"),
                        "data": entry.get("data", {}),
                    }
                )
        return session

    # ---------------- 历史（会话树） ----------------
    def _new_id(self) -> str:
        return uuid.uuid4().hex[:8]

    def append(self, message: dict) -> str:
        """追加一条消息，返回它的 entry id。"""
        entry_id = self._new_id()
        self.messages.append(message)
        self.message_ids.append(entry_id)
        self._write({"type": "message", "id": entry_id, "parent": self.current_id, **message})
        self.current_id = entry_id
        return entry_id

    def branch_from(self, entry_id: str) -> None:
        """从某条消息**分叉**：截断它之后的活动分支，后续消息挂到它下面。

        文件里两条分支都在（JSONL 只追加，不删），内存里只保留活动分支 —— 和 Pi 一致。
        """
        if entry_id not in self.message_ids:
            raise ValueError(f"entry 不存在: {entry_id}")
        index = self.message_ids.index(entry_id) + 1
        self.messages = self.messages[:index]
        self.message_ids = self.message_ids[:index]
        self.current_id = entry_id
        self._write({"type": "branch", "from": entry_id})

    def checkout(self, entry_id: str | None, reason: str = "branch") -> None:
        """切换到任意历史节点；允许进入已经不在当前活动分支上的旧节点。"""
        if entry_id is None:
            self.messages = []
            self.message_ids = []
            self.current_id = None
            self._write({"type": "branch", "from": None, "reason": reason})
            return

        entries = [item for item in self.entries() if item.get("type") == "message" and item.get("id")]
        by_id = {item["id"]: item for item in entries}
        if entry_id not in by_id:
            raise ValueError(f"entry 不存在: {entry_id}")
        lineage: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = entry_id
        while cursor:
            if cursor in seen or cursor not in by_id:
                break
            seen.add(cursor)
            item = by_id[cursor]
            lineage.append(item)
            cursor = item.get("parent")
        lineage.reverse()
        self.messages = [
            {key: value for key, value in item.items() if key not in ("type", "ts", "id", "parent")}
            for item in lineage
            if item.get("content") != "[之前的对话摘要]"
        ]
        self.message_ids = [item["id"] for item in lineage if item.get("content") != "[之前的对话摘要]"]
        self.current_id = self.message_ids[-1] if self.message_ids else None
        self._write({"type": "branch", "from": entry_id, "reason": reason})

    def resend_from(self, entry_id: str, message: dict) -> str:
        """重发：从某条消息的**父节点**分叉，再以新 id 追加同一条内容（对应 resendFrom）。"""
        if entry_id not in self.message_ids:
            raise ValueError(f"entry 不存在: {entry_id}")
        index = self.message_ids.index(entry_id)
        self.messages = self.messages[:index]
        self.message_ids = self.message_ids[:index]
        self.current_id = self.message_ids[-1] if self.message_ids else None
        self._write({"type": "branch", "from": self.current_id, "reason": "resend"})
        return self.append(message)

    def entry_id_at(self, index: int) -> str | None:
        return self.message_ids[index] if 0 <= index < len(self.message_ids) else None

    def request_messages(self) -> list[dict]:
        return ([{"role": "system", "content": self.system_prompt}] if self.system_prompt else []) + self.messages

    def size_chars(self) -> int:
        return len(json.dumps(self.request_messages(), ensure_ascii=False))

    # ---------------- 压缩 ----------------
    def replace_with_summary(self, summary: str, keep_recent: int) -> None:
        """把较早的历史换成一段摘要（原始内容仍留在 JSONL 文件里）。"""
        old = self.messages[:-keep_recent] if keep_recent else list(self.messages)
        recent = self.messages[-keep_recent:] if keep_recent else []
        self.messages = [{"role": "user", "content": f"[之前的对话摘要]\n{summary}"}] + recent
        # 会话树同步：摘要是一条新消息，挂在原 current 之下；被压掉的 id 只留在文件里
        parent = self.current_id
        summary_id = self._new_id()
        self.message_ids = [summary_id] + self.message_ids[len(old):]
        self.current_id = self.message_ids[-1] if self.message_ids else summary_id
        self.compact_count += 1
        self._write({"type": "message", "id": summary_id, "parent": parent, "role": "user", "content": "[之前的对话摘要]"})
        self._write(
            {"type": "compaction", "summary": summary, "compacted": len(old), "kept": len(recent)}
        )

    # ---------------- 用量 ----------------
    def record_usage(self, usage: Usage, label: str = "main") -> UsageEntry:
        entry = UsageEntry(usage=usage, label=label, seq=len(self.usage_log))
        self.usage_log.append(entry)
        self._write({"type": "usage", "label": label, **usage.as_dict()})
        return entry

    def usage_offset(self) -> int:
        """当前记录条数 —— 子 Agent 开跑前记下它，就是"起始偏移"。"""
        return len(self.usage_log)

    def usage_since(self, offset: int) -> Usage:
        """从偏移处开始求和 = **增量用量**（只对本次新增负责）。"""
        total = Usage()
        for entry in self.usage_log[offset:]:
            total.add(entry.usage)
        return total

    def usage_total(self) -> Usage:
        """全量用量（对照实验里的"错误做法"）。"""
        total = Usage()
        for entry in self.usage_log:
            total.add(entry.usage)
        return total
