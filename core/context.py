"""ToolContext：工具拿到的"能力包"。

对照 Toonflow：apps/server/src/agent/tools/index.ts 的 createAgentToolContext()
  · 工具拿到的不是裸 fs，而是一组**受约束的能力**（files / log / config）
  · 所有路径必须先过 resolve_path() 授权，才允许读写
  · 写操作带锁（对应 Toonflow 的 lockWorkspaceFiles）

为什么要这么绕（面试必答题 #1）：
  工具是"可信的服务端代码，不是沙箱"（Toonflow 的明确取舍），自由度最大；
  代价就是越界、符号链接、并发写这三个风险必须由能力包统一拦住，
  不能指望每个工具作者自己记得检查。
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class CapabilityUnavailable(RuntimeError):
    """能力未接入。

    对应 Toonflow 的场景：工具依赖 media（媒体生成）但服务端没配 provider。
    这里的选择是**显式抛错并说明原因**，而不是回退成 None / AttributeError ——
    工具作者能立刻知道"缺哪一项能力"，而不是调试一个 NoneType 报错。
    """


@runtime_checkable
class MediaCapability(Protocol):
    """媒体生成能力位（对应 Toonflow ToolContext.media）。本实现只有协议，没有 provider。"""

    def list_models(self) -> list[dict[str, Any]]: ...
    def generate(self, kind: str, request: dict[str, Any]) -> Any: ...


@runtime_checkable
class QuestionCapability(Protocol):
    """反问能力位（对应 Toonflow ToolContext.question / packages/tools/askUser）。"""

    def ask(self, call_id: str, title: str, question: str, options: list[str] | None = None) -> Any: ...


@runtime_checkable
class CanvasCapability(Protocol):
    """画布能力位（对应 Toonflow ToolContext.canvas / bridge/canvas.ts）。"""

    def call(self, tool: str, args: dict[str, Any]) -> Any: ...


@runtime_checkable
class CanvasRepositoryCapability(Protocol):
    async def load(self, name: str) -> Any: ...
    async def save(self, canvas: Any) -> int: ...
    async def list(self) -> list[str]: ...


class SandboxError(Exception):
    """路径越界 / 符号链接逃逸。对应 Toonflow 把文件系统错误映射成 403。"""


def _norm(path: Path) -> str:
    """Windows 不区分大小写，比较前先归一化。"""
    return os.path.normcase(os.path.abspath(str(path)))


def is_within(child: Path, root: Path) -> bool:
    return _norm(child).startswith(_norm(root) + os.sep) or _norm(child) == _norm(root)


@dataclass
class ToolFiles:
    """受限文件能力。工具只能用它，不直接 open()。"""

    ctx: "ToolContext"

    def read_text(self, path: str, read_only: bool = True) -> str:
        return self.ctx.resolve_path(path, read_only).read_text(encoding="utf-8", errors="replace")

    def list_dir(self, path: str = ".", read_only: bool = True) -> list[str]:
        target = self.ctx.resolve_path(path, read_only)
        if not target.is_dir():
            raise NotADirectoryError(f"不是目录: {path}")
        return sorted(x.name + ("/" if x.is_dir() else "") for x in target.iterdir())

    def stat(self, path: str, read_only: bool = True) -> os.stat_result:
        return self.ctx.resolve_path(path, read_only).stat()

    def iter_files(self, path: str = ".", pattern: str = "*") -> list[Path]:
        """递归列文件。每个结果都过一次沙箱检查 —— rglob 可能顺着符号链接跑出去。"""
        root = self.ctx.resolve_path(path)
        results: list[Path] = []
        for candidate in root.rglob(pattern):
            try:
                results.append(self.ctx.resolve_path(str(candidate)))
            except SandboxError:
                continue
        return [p for p in results if p.is_file()]

    def exists(self, path: str, read_only: bool = True) -> bool:
        return self.ctx.resolve_path(path, read_only).exists()

    async def write_text(self, path: str, content: str, exclusive: bool = False) -> None:
        async with self.ctx.write_lock(path):
            target = self.ctx.resolve_path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            if exclusive and target.exists():
                raise FileExistsError(f"文件已存在: {path}")
            target.write_text(content, encoding="utf-8")

    async def mkdir(self, path: str, recursive: bool = True) -> None:
        async with self.ctx.write_lock(path):
            self.ctx.resolve_path(path).mkdir(parents=recursive, exist_ok=True)

    async def remove(self, path: str, recursive: bool = False) -> None:
        async with self.ctx.write_lock(path):
            target = self.ctx.resolve_path(path)
            self.ctx.protect_root(target)
            if target.is_dir():
                if recursive:
                    import shutil

                    shutil.rmtree(target)
                else:
                    target.rmdir()
            else:
                target.unlink()


@dataclass
class ToolContext:
    cwd: str
    config: dict[str, Any] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, repr=False)
    # 可选能力位（对应 Toonflow 的 canvas / question / media / ffmpeg / sdk）。
    # 用 Protocol 声明 + 默认 None：能力包是**可扩展**的，接不接入由部署方决定。
    media: MediaCapability | None = None
    question: QuestionCapability | None = None
    canvas: CanvasCapability | None = None
    canvas_repository: CanvasRepositoryCapability | None = None
    # 真实媒体生成是有成本的外部副作用。默认永远为 False，只有宿主在收到
    # 用户本次明确审批后才能创建一个临时、已授权的执行上下文。
    media_approved: bool = False
    # 当前 Agent 实际拿到的工具名。部分能力（例如 Python 版画布）由独立工具
    # 提供，不经过 ToolContext.canvas；能力自检需要据此避免误报。
    tool_names: set[str] = field(default_factory=set)

    @property
    def files(self) -> ToolFiles:
        return ToolFiles(self)

    def derive(self, *, config: dict[str, Any] | None = None, tool_names: set[str] | None = None) -> "ToolContext":
        """派生运行上下文，同时保留宿主注入的能力与共享写锁。

        插件装载、主会话和子 Agent 都会创建新上下文。集中在这里复制能力，
        可避免只复制锁、却把 media/question/canvas 静默丢掉。
        """
        return ToolContext(
            cwd=self.cwd,
            config=dict(self.config if config is None else config),
            logs=self.logs,
            _locks=self._locks,
            media=self.media,
            question=self.question,
            canvas=self.canvas,
            canvas_repository=self.canvas_repository,
            # 审批不跨 Agent / 子 Agent / 下一轮传播，防止一次同意变成永久授权。
            media_approved=False,
            tool_names=set(self.tool_names if tool_names is None else tool_names),
        )

    # ------------------------------------------------------------------
    # 能力位：接入与否可查，缺失时给可诊断的错误
    # ------------------------------------------------------------------
    def capabilities(self) -> dict[str, bool]:
        return {
            "files": True,
            "log": True,
            "media": self.media is not None,
            "question": self.question is not None,
            "canvas": self.canvas is not None,
            "canvas_repository": self.canvas_repository is not None,
        }

    def require_media(self) -> MediaCapability:
        if self.media is None:
            raise CapabilityUnavailable(
                "media 能力未接入：本项目只实现了能力位协议，未配置媒体生成 provider"
                "（Toonflow 对应 ToolContext.media 的 generateImage/Video/Audio）"
            )
        return self.media

    def require_question(self) -> QuestionCapability:
        if self.question is None:
            raise CapabilityUnavailable("question 能力未接入：需要前端/调用方提供交互通道（对应 askUser 工具）")
        return self.question

    def require_canvas_repository(self) -> CanvasRepositoryCapability:
        if self.canvas_repository is None:
            raise CapabilityUnavailable("canvas_repository 能力未接入")
        return self.canvas_repository

    def require_canvas(self) -> CanvasCapability:
        if self.canvas is None:
            raise CapabilityUnavailable("canvas 能力未接入：需要画布服务提供桥接（对应 bridge/canvas.ts）")
        return self.canvas

    # ------------------------------------------------------------------
    # 路径沙箱
    # ------------------------------------------------------------------
    def resolve_path(self, path: str, read_only: bool = False) -> Path:
        """授权一个路径：越界和符号链接逃逸都会抛 SandboxError。

        两步缺一不可：
          1) resolve() 之后的绝对路径必须在 root 内（防 ../ 越界）
          2) realpath 之后也必须在 root 内（防符号链接指向 /etc/passwd）
        """
        root = Path(self.cwd).resolve()
        candidate = Path(path)
        absolute = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()

        if not is_within(absolute, root):
            raise SandboxError(f"路径越界（不在工作区内）: {path}")

        real = Path(os.path.realpath(absolute))
        if not is_within(real, root):
            raise SandboxError(f"符号链接指向工作区外: {path} -> {real}")
        return real

    def protect_root(self, target: Path) -> None:
        """禁止对 workspace 根目录本身下手（对应 Toonflow 的 protectWorkspaceRoot）。"""
        if _norm(target) == _norm(Path(self.cwd).resolve()):
            raise SandboxError("不允许操作工作区根目录本身")

    def write_lock(self, path: str) -> asyncio.Lock:
        """按路径加锁：并发委派时，两个子 Agent 写同一个文件不会互相覆盖。

        对应 Toonflow 的 lockWorkspaceFiles() —— 单进程也需要，因为 asyncio 并发是真的并发。
        """
        key = _norm(self.resolve_path(path))
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    # ------------------------------------------------------------------
    # 日志能力（工具不该直接 print）
    # ------------------------------------------------------------------
    def log(self, message: str, level: str = "info") -> None:
        line = f"[{level}] {message}"
        self.logs.append(line)
        print(f"    · {line}  ({time.strftime('%H:%M:%S')})")
