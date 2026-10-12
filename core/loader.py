"""工具动态装载器。

对照 Toonflow：apps/server/src/utils/plugins/tools.ts
  · listTools()      —— 元数据"静态解析"：只读文件头的元数据，不执行工具逻辑
  · loadTool()       —— 动态 import（TS: pathToFileURL + import | Py: importlib）
  · 热装载要清缓存   —— TS 删 require.cache | Py 弹 sys.modules
  · createAgentTools —— 遍历装载、校验配置、合并 promptGuidelines（见本文件底部）

Python 侧的差异：TS 把元数据写在文件头注释里（`/*! toonflowTool:{...} */`），
因为 Bun 可以不执行代码就读到它；Python 没有这个机制，用**模块级常量 TOOL_META**
——代价是要 import 一次模块，好处是元数据能被 Pydantic 直接校验、也能被 IDE 检查。
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .context import ToolContext
from .media import MediaCapabilityAdapter, default_registry
from .plugin import ToolDefinition, ToolMetadata, ToolPlugin

TOOLS_ROOT = Path(__file__).resolve().parent.parent / "tools"
_MODULE_PREFIX = "toonflow_tool_"


@dataclass
class ToolEntry:
    """一条"已发现的工具"，对应 Toonflow listTools() 的返回项。"""

    name: str
    path: Path
    metadata: ToolMetadata | None = None
    load_error: str | None = None
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.load_error is None and (self.metadata is None or self.metadata.enabled)

    @property
    def display_name(self) -> str:
        return self.metadata.display_name if self.metadata else self.name


def _module_name(tool_name: str) -> str:
    return f"{_MODULE_PREFIX}{tool_name}"


def _import_module(tool_name: str, path: Path, reload: bool = False) -> Any:
    """按文件路径动态装载一个模块（对应 TS 的 pathToFileURL + await import）。"""
    mod_name = _module_name(tool_name)
    if reload:
        sys.modules.pop(mod_name, None)  # 热装载：不清缓存会拿到旧代码
    if mod_name in sys.modules and not reload:
        return sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法构造模块 spec: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:  # 装载失败要记录下来，不能让一个坏工具拖垮整个 agent
        sys.modules.pop(mod_name, None)
        raise
    return module


def _read_metadata(module: Any) -> ToolMetadata:
    meta = getattr(module, "TOOL_META", None)
    if meta is None:
        raise ValueError("模块缺少 TOOL_META 常量")
    if isinstance(meta, ToolMetadata):
        return meta
    return ToolMetadata.model_validate(meta)


def _resolve_plugin(module: Any) -> Any:
    """从模块里找出插件实现：优先 Plugin 类，其次模块级 create_tools 函数。"""
    plugin_cls = getattr(module, "Plugin", None)
    if plugin_cls is not None:
        return plugin_cls()
    if hasattr(module, "create_tools"):
        return _FunctionPlugin(module)
    raise ValueError("模块既没有 Plugin 类，也没有 create_tools 函数")


class _FunctionPlugin:
    """把模块级函数适配成 ToolPlugin 的形状。"""

    def __init__(self, module: Any) -> None:
        self._module = module

    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        fn = getattr(self._module, "validate_config", None)
        return fn(config) if fn else config

    def create_tools(self, context: ToolContext) -> Any:
        return self._module.create_tools(context)


def list_tools(root: Path = TOOLS_ROOT) -> list[ToolEntry]:
    """扫描工具目录 + 静态解析元数据（不执行 create_tools）。"""
    entries: list[ToolEntry] = []
    if not root.exists():
        return entries
    for tool_file in sorted(root.glob("*/tool.py")):
        name = tool_file.parent.name
        entry = ToolEntry(name=name, path=tool_file)
        try:
            module = _import_module(name, tool_file)
            entry.metadata = _read_metadata(module)
            if entry.metadata.name != name:
                entry.load_error = f"元数据 name={entry.metadata.name} 与目录名 {name} 不一致"
        except Exception as exc:
            entry.load_error = f"{type(exc).__name__}: {exc}"
        entries.append(entry)
    return entries


def load_tool(name: str, root: Path = TOOLS_ROOT, reload: bool = False) -> tuple[Any, ToolMetadata]:
    """装载一个工具插件，返回 (plugin, metadata)。"""
    path = root / name / "tool.py"
    if not path.exists():
        raise FileNotFoundError(f"找不到工具: {name}（{path}）")
    module = _import_module(name, path, reload=reload)
    metadata = _read_metadata(module)
    plugin = _resolve_plugin(module)
    if not isinstance(plugin, ToolPlugin):
        raise TypeError(f"{name} 的插件不符合 ToolPlugin 契约")
    return plugin, metadata


def validate_tool_config(plugin: Any, config: dict[str, Any]) -> dict[str, Any]:
    """对应 Toonflow 的 validateToolConfig()：配置错了要在装载期炸，不要等到运行时。"""
    return plugin.validate_config(config or {})


def reload_tool(name: str, root: Path = TOOLS_ROOT) -> tuple[Any, ToolMetadata]:
    """热装载：改了工具代码不用重启进程。"""
    return load_tool(name, root, reload=True)


async def create_agent_tools(
    cwd: str,
    config: dict[str, dict[str, Any]] | None = None,
    root: Path = TOOLS_ROOT,
    canvas_repository: Any | None = None,
) -> tuple[list[ToolDefinition], list[ToolEntry], ToolContext]:
    """对应 createAgentTools()：装载 → 校验配置 → 造工具 → 合并 promptGuidelines。

    返回 (工具列表, 装载清单, context)，清单里带着每个工具是否启用/是否出错，
    这样出问题时能直接告诉用户"哪个工具没装起来"，而不是静默少一个工具。
    """
    config = config or {}
    # 能力位在这里注入：工具只依赖 MediaCapability 协议，换 provider 不用改工具代码
    if canvas_repository is None:
        from core.canvas_repository import FileCanvasRepository

        canvas_repository = FileCanvasRepository(Path(cwd))
        await canvas_repository.start()
    ctx = ToolContext(
        cwd=cwd,
        media=MediaCapabilityAdapter(default_registry, Path(cwd)),
        canvas_repository=canvas_repository,
    )
    tools: list[ToolDefinition] = []
    entries = list_tools(root)
    seen: set[str] = set()

    for entry in entries:
        if not entry.enabled:
            ctx.log(f"跳过 {entry.name}：{entry.load_error or '已禁用'}", level="warn")
            continue
        plugin, metadata = load_tool(entry.name, root)
        tool_config = validate_tool_config(plugin, config.get(entry.name, {}))
        scoped = ctx.derive(config=tool_config)

        created = plugin.create_tools(scoped)
        if inspect.isawaitable(created):
            created = await created

        for tool in created:
            if not tool.name or not callable(tool.execute):
                raise ValueError(f"{entry.name} 返回了无效的工具")
            if tool.name in seen:
                raise ValueError(f"工具名称重复：{tool.name}")
            seen.add(tool.name)
            tools.append(
                ToolDefinition(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.parameters,
                    execute=tool.execute,
                    args_model=tool.args_model,
                    prompt_guidelines=[*( [metadata.prompt] if metadata.prompt else []), *tool.prompt_guidelines],
                    side_effect=tool.side_effect,
                )
            )
        entry.metadata = metadata
    ctx.tool_names = {tool.name for tool in tools}
    return tools, entries, ctx
