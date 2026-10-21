"""P0-12：Agent 工具与画布生成必须同源（参数、写回、历史版本）。

审查发现两条双轨：
1. ``tools/media/tool.py`` 的 ``run_canvas`` 自己拼 Provider 请求，硬编码
   ``1024x576`` / ``1280x720``，完全忽略画布解析出的 duration/ratio/resolution，
   也不写 ``takes`` 历史；
2. Agent 写回用裸 ``repository.save``，并发编辑时直接失败（画布 Worker 已经
   改成 CAS 重试，Agent 侧还是老写法）。

这组用例锁住「同一套参数构造器 + 同一套写回逻辑」。
"""

from __future__ import annotations

import asyncio

from core.context import ToolContext
from core.media import MediaCapabilityAdapter
from tools.media.tool import build_tools


def _tools(p0, *, approved: bool = True):
    context = ToolContext(
        cwd=str(p0.workspace),
        config={"default_canvas": "main"},
        media=MediaCapabilityAdapter(p0.provider, p0.workspace),
        canvas_repository=p0.repository,
        media_approved=approved,
    )
    return context, {tool.name: tool for tool in build_tools(context)}


async def _run(p0, name: str, args: dict, *, approved: bool = True):
    context, tools = _tools(p0, approved=approved)
    return await tools[name].execute(args, context)


async def _set_project_and_node(p0, *, project: dict, data: dict) -> None:
    canvas = await p0.repository.load("main")
    canvas.meta["project"] = project
    canvas.update_node(p0.video_id, data=data)
    await p0.repository.save(canvas)


def test_p0_12_agent_run_canvas_uses_resolved_params(p0):
    """Agent 触发的生成，必须拿到画布解析出的 size/duration/ratio/resolution。"""
    asyncio.run(
        _set_project_and_node(
            p0,
            project={"aspect_ratio": "9:16", "quality": "720P"},
            data={"duration": 6},
        )
    )

    result = asyncio.run(_run(p0, "run_canvas", {"canvas": "main", "only": [p0.video_id]}))

    assert not result.is_error, result.content
    assert len(p0.provider.calls) == 1
    request = p0.provider.calls[-1]["request"]
    assert request["size"] == "720x1280"
    assert request["duration"] == 6
    assert request["ratio"] == "9:16"
    assert request["resolution"] == "720p"
    # 不再硬编码 mock-video-v1：默认模型来自当前 Provider 的模型表
    assert request["model"] == "fake-video-v1"

    node = p0.canvas_node(p0.video_id)
    assert node["status"] == "done"
    assert node["outputs"]["video"]["value"]["path"]
    takes = node["data"]["takes"]
    assert len(takes) == 1
    assert takes[0]["duration_s"] == 6.0
    assert takes[0]["mode"] == "canvas"


def test_p0_12_agent_run_canvas_records_history(p0):
    """Agent 连续生成两次 → takes 必须累积两版（画布 Worker 的语义）。"""
    first = asyncio.run(_run(p0, "run_canvas", {"canvas": "main", "only": [p0.video_id]}))
    assert not first.is_error, first.content
    second = asyncio.run(_run(p0, "run_canvas", {"canvas": "main", "only": [p0.video_id]}))
    assert not second.is_error, second.content
    assert len(p0.provider.calls) == 2

    node = p0.canvas_node(p0.video_id)
    takes = node["data"]["takes"]
    assert len(takes) == 2
    # 采用语义（P1 修复后）：首次生成自动采用，重生成只追加、永不抢占——
    # 当前版仍指向第一版，新版留在历史里等人工挑选。
    assert node["data"]["current_take_id"] == takes[0]["take_id"]
    assert node["outputs"]["video"]["value"]["path"] == takes[0]["path"]


def test_p0_12_agent_run_canvas_survives_concurrent_edit(p0):
    """Agent 写回期间用户拖了节点 → 产物与用户编辑都要保留（CAS 重试）。"""
    race = p0.inflate_competing_edit_before_writeback(p0.prompt_id)
    result = asyncio.run(_run(p0, "run_canvas", {"canvas": "main", "only": [p0.video_id]}))
    assert race["fired"] is True

    assert not result.is_error, result.content
    assert len(p0.provider.calls) == 1
    node = p0.canvas_node(p0.video_id)
    assert node["status"] == "done"
    assert len(node["data"]["takes"]) == 1
    path = node["outputs"]["video"]["value"]["path"]
    assert (p0.workspace / path).is_file()
    assert p0.canvas_node(p0.prompt_id)["position"] == {"x": 999.0, "y": 999.0}


def test_p0_12_generate_media_records_take(p0):
    """单条 generate_media 也要写 takes：不能只覆盖 outputs。"""
    result = asyncio.run(
        _run(
            p0,
            "generate_media",
            {
                "prompt": "雨夜霓虹街道，女孩撑伞走过积水。",
                "canvas": "main",
                "node_id": p0.video_id,
                "kind": "video",
                "size": "",
                "duration": 5,
            },
        )
    )
    assert not result.is_error, result.content
    request = p0.provider.calls[-1]["request"]
    assert request["size"] == "1280x720"  # 节点没有参数时的兼容默认，不是硬编码分支
    assert request["duration"] == 5

    node = p0.canvas_node(p0.video_id)
    takes = node["data"]["takes"]
    assert len(takes) == 1
    assert takes[0]["mode"] == "canvas"
    assert node["outputs"]["video"]["value"]["path"] == takes[0]["path"]


def test_p0_12_agent_tools_still_require_approval(p0):
    """参数统一不能绕过审批：未审批时一次 Provider 都不能调。"""
    result = asyncio.run(_run(p0, "run_canvas", {"canvas": "main"}, approved=False))
    assert result.is_error
    assert result.details.get("approval_required") is True
    assert p0.provider.calls == []


def test_p0_12_build_media_request_is_shared_contract():
    """共享构造器：冻结 job（flat 字段）与画布 job（params 字段）都要能翻译。"""
    from core import canvas as canvas_module

    builder = getattr(canvas_module, "build_media_request")

    frozen = builder(
        {
            "kind": "video",
            "prompt": "镜头 1",
            "duration": 7,
            "ratio": "9:16",
            "resolution": "720p",
            "size": "720x1280",
            "references": [{"path": "assets/a.png", "type": "IMAGE", "mime_type": "image/png"}],
        },
        provider="mock",
        model="fake-video-v1",
        generate_audio=False,
    )
    assert frozen["size"] == "720x1280"
    assert frozen["duration"] == 7
    assert frozen["ratio"] == "9:16"
    assert frozen["resolution"] == "720p"
    assert frozen["provider"] == "mock"
    assert frozen["model"] == "fake-video-v1"
    assert frozen["references"] == [
        {"path": "assets/a.png", "type": "IMAGE", "mime_type": "image/png"}
    ]

    canvas_job = builder(
        {
            "kind": "image",
            "prompt": "关键帧",
            "params": {"duration": None, "ratio": None, "resolution": None, "size": None},
            "refs": [{"path": "assets/ref.png", "type": "IMAGE", "mime_type": "image/png"}],
        },
        model="fake-image-v1",
    )
    assert canvas_job["size"] == "1024x576"  # 无参数时的兼容值
    assert "duration" not in canvas_job or canvas_job["duration"] is None
    assert canvas_job["references"][0]["path"] == "assets/ref.png"
