"""pipeline 触发钩子：server/studio 注册实现，工具插件只读接口。

为什么绕一层：tools/ 依赖 core（不能反向 import server），而「在后台触发
剧本理解 / 资产解析」的实现细节（单飞约束、LLM 注入、落盘）都住在 server 层。
工具拿到的是 spawn(canvas, pipeline) 的布尔结果——它只该知道「触发成功与否」，
不该知道任务怎么跑。
"""
from __future__ import annotations

from typing import Callable

Spawner = Callable[[str, str], dict]
_spawner: Spawner | None = None

PIPELINES = ("story_analysis", "asset_parse")


def register_spawner(fn: Spawner) -> None:
    global _spawner
    _spawner = fn


def spawn(canvas: str, pipeline: str) -> dict:
    """触发后台 pipeline；未接入或未知名称时返回可读失败，不抛异常。"""
    if _spawner is None:
        return {"ok": False, "detail": "pipeline 服务未就绪"}
    if pipeline not in PIPELINES:
        return {"ok": False, "detail": f"未知 pipeline：{pipeline}"}
    return _spawner(canvas, pipeline)
