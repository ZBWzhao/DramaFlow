"""制片台适配层：把画布翻译成「制作者视角」的对象，再翻译回来。

为什么是适配层而不是新数据模型
------------------------------
画布已经是项目的完整持久化表示（节点 = 资产/分镜/提示词/生成槽，边 = 依赖）。
制作者要的 Project / Episode / Asset / Look / Segment / Take 全部**可派生**，
所以不新建表、不改 `core/canvas.py`，只在外面套一层翻译：

    画布（引擎视角）  ←→  studio.py（翻译）  ←→  前端（制作者视角）

写入方向只有一个例外：把制作者的操作写回节点 `data.*`
（looks / episode_no / tags / takes / current_take_id），都是**附加字段**，
老画布缺这些字段时按空值降级，Agent 侧的工具完全无感。
"""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import quote
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from core.canvas import Canvas, CanvasError
from core.canvas_repository import mutate_canvas_with_retry
from core.asset_tables import AssetTables, merge_asset_tables, parse_asset_tables
from core.skills import discover_skills
from core.story_analysis import CARDS, StoryAnalysis, run_story_analysis
from core.studio_rules import (
    THRESHOLDS,
    dialogue_from_shots,
    lint_segment,
    parse_dialogue,
    refs_hash,
    shots_hash,
)

router = APIRouter(prefix="/api/studio", tags=["studio"])

# 由 server/app.py 在装配阶段注入，避免 studio 反向 import app 造成循环依赖。
_registry: Any = None
_workspace: Path | None = None
_media: Any = None
# Agent 运行时：制片台要能「让 Agent 开始制作」，但不能反向 import app。
_agent_runner: Any = None
_prompt_builder: Any = None

# 生成任务在进程内跟踪（Lite 模式；完整的媒体任务队列仍走 MediaJobStore）。
_jobs: dict[str, dict[str, Any]] = {}
_tasks: dict[str, asyncio.Task[Any]] = {}


def configure(
    *,
    registry: Any,
    workspace: Path,
    media_registry: Any,
    agent_runner: Any = None,
    prompt_builder: Any = None,
) -> None:
    global _registry, _workspace, _media, _agent_runner, _prompt_builder
    _registry = registry
    _workspace = workspace
    _media = media_registry
    _agent_runner = agent_runner
    _prompt_builder = prompt_builder


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class ProjectPatch(BaseModel):
    region: str | None = None
    genre: str | None = None
    style_preset: str | None = None
    aspect_ratio: str | None = None
    video_model: str | None = None
    quality: str | None = None
    story_title: str | None = None
    story_summary: str | None = None
    episode: int | None = None


class PromptPatch(BaseModel):
    prompt: str
    duration_s: float | None = None
    references: list[dict[str, Any]] | None = None


class LookPatch(BaseModel):
    name: str = ""
    image_path: str | None = None
    prompt: str | None = None
    locked: bool | None = None
    appearances: list[dict[str, Any]] | None = None
    # 新增形象时生成用的参数
    generate: bool = False
    size: str = "768x1024"
    model: str | None = None


class GenerateRequest(BaseModel):
    model: str | None = None
    size: str | None = None
    duration: float | None = None
    provider: str | None = None
    # 从第几个子镜头生成（默认第 1 个）。有首帧时以该镜头的时长为准。
    shot_no: int = 1
    # 显式关闭首帧模式（默认有首帧就用）。关掉会退回「参考图 + 整段提示词」。
    use_keyframe: bool | None = None
    # 用户手改/前端显式传入的正文。非空时优先于服务端渲染——首帧模式也生效。
    # 不传时，若片段已被人改过（rendered_by=human），同样用已保存的人改正文。
    prompt: str | None = None
    # 显式「忽略校验问题，强制生成」。以前靠「传个 duration 就能绕过硬校验」的
    # 旁路——语义错位且容易误触；现在绕过必须显式声明。
    force: bool = False


class KeyframeRequest(BaseModel):
    shot_no: int = 1
    prompt: str = ""
    model: str | None = None
    size: str | None = None


class TakeSelect(BaseModel):
    take_id: str


class AssetPatch(BaseModel):
    """资产字段编辑（本期仅 voice_id）。"""

    voice_id: str | None = None


class TagsPatch(BaseModel):
    tags: list[str] = Field(default_factory=list)


class ImportScriptRequest(BaseModel):
    text: str
    split: bool = True


class EpisodePatch(BaseModel):
    title: str | None = None
    status: str | None = None


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _require_configured() -> None:
    if _registry is None or _workspace is None:
        raise HTTPException(status_code=503, detail="制片台适配层尚未装配")


_canvas_locks: dict[str, asyncio.Lock] = {}


def _lock_for(canvas: str) -> asyncio.Lock:
    """同一张画布的写操作排队。

    画布仓库用 revision 做乐观锁：两个请求各自 load 同一版本再 save，后写的会收到
    409。制片台的写全是「读-改-写」，在单进程里用一把锁把它们串起来，比让前端去
    猜什么时候该重试可靠得多（批量出片就是连着发十几个写请求）。
    """
    lock = _canvas_locks.get(canvas)
    if lock is None:
        lock = asyncio.Lock()
        _canvas_locks[canvas] = lock
    return lock


async def _load(canvas: str) -> Canvas:
    _require_configured()
    try:
        return await _registry.canvas_repository.load(canvas)
    except (KeyError, FileNotFoundError) as exc:  # pragma: no cover - 取决于仓库实现
        raise HTTPException(status_code=404, detail=f"画布不存在：{canvas}") from exc


async def _save(canvas: Canvas) -> int:
    try:
        await _registry.canvas_repository.save(canvas)
    except Exception as exc:  # 冲突/写入失败都转成可读错误
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return canvas.revision


_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)
_CLOCK_RANGE_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[–—\-~至]\s*(\d{1,2}):(\d{2})")


def _parse_duration(value: Any) -> float | None:
    """把 `17s` / `17` / `00:45-01:00` 统一成秒。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    match = _DURATION_RE.search(text)
    if match:
        return float(match.group(1))
    match = _CLOCK_RANGE_RE.search(text)
    if match:
        end = int(match.group(3)) * 60 + int(match.group(4))
        start = int(match.group(1)) * 60 + int(match.group(2))
        if end > start:
            return float(end - start)
    try:
        return float(text)
    except ValueError:
        return None


def _node_text(canvas: Canvas, node: Any) -> str:
    """读节点正文：**委托给 Canvas.read_text**，不再维护第二套读取顺序。

    这里以前自己实现了一份（text → prompt，且不认 data.prompt），和画布侧
    read_text（文件 → prompt → text → data.prompt）不一致：同一节点在画布能读到
    正文、在制片台的片段列表里却是空的。正文读取只能有一个来源。
    """
    if node is None:
        return ""
    return canvas.read_text(node.id)


def _first_media(canvas: Canvas, node: Any, data_type: str) -> dict[str, Any]:
    return canvas.media_output(node.id, data_type)


def _infer_duration(canvas: Canvas, node: Any, prompt: str, shots: list[dict[str, Any]]) -> tuple[float | None, str]:
    """片段时长推断：显式字段 → 提示词头 → 分镜合计。返回 (秒, 来源)。"""
    for key in ("duration_s", "duration", "target_duration"):
        parsed = _parse_duration(node.data.get(key))
        if parsed:
            return parsed, f"data.{key}"

    match = _CLOCK_RANGE_RE.search(prompt or "")
    if match:
        start = int(match.group(1)) * 60 + int(match.group(2))
        end = int(match.group(3)) * 60 + int(match.group(4))
        if end > start:
            return float(end - start), "prompt.header"

    total = 0.0
    for shot in shots:
        try:
            total += _shot_seconds(shot)
        except (TypeError, ValueError):
            continue
    if total > 0:
        return round(total, 2), "shots_sum"
    return None, "unknown"


def _segment_no(node: Any, fallback: int) -> int:
    try:
        return int(node.data.get("segment_no") or fallback)
    except (TypeError, ValueError):
        return fallback


_LABEL_G_RE = re.compile(r"\bG(\d+)\b")


def _episode_no(node: Any) -> int:
    try:
        return int(node.data.get("episode_no") or 1)
    except (TypeError, ValueError):
        return 1


# ---------------------------------------------------------------------------
# 项目 / 分集
# ---------------------------------------------------------------------------

DEFAULT_PROJECT: dict[str, Any] = {
    "region": "国内",
    "genre": "",
    "style_preset": "",
    "aspect_ratio": "9:16",
    "video_model": "mock-video-v1",
    "quality": "480P",
}


# 角色名（连字符）→ 项目选择键（下划线）：meta.project.skills = {script_analysis, director, asset}
_ROLE_KEYS = {"script-analysis": "script_analysis", "director": "director", "asset": "asset"}


def _validated_skills(skills: dict[str, str]) -> dict[str, str]:
    """只接受真实存在的 "name@version"，且 role 必须匹配——防止弹窗/接口塞进任意字符串。"""
    entries, _ = discover_skills()
    known: dict[str, tuple[str, str]] = {}
    for entry in entries:
        if entry.role in _ROLE_KEYS:
            known[f"{entry.name}@{entry.version}"] = (entry.role, _ROLE_KEYS[entry.role])
    out: dict[str, str] = {}
    for key, value in (skills or {}).items():
        if not str(value).strip():
            continue
        if str(value) not in known:
            raise HTTPException(status_code=400, detail=f"未知技能：{value}")
        _role, canonical_key = known[str(value)]
        if key in out and out[key] != value:  # pragma: no cover - 同键双值仅在脏输入出现
            raise HTTPException(status_code=400, detail=f"技能角色冲突：{key}")
        out[canonical_key] = str(value)
    return out


def _project_meta(canvas: Canvas) -> dict[str, Any]:
    merged = dict(DEFAULT_PROJECT)
    project = (canvas.meta or {}).get("project")
    if isinstance(project, dict):
        merged.update({k: v for k, v in project.items() if v not in (None, "")})
    if not merged.get("style_preset"):
        for node in canvas.nodes.values():
            if node.type == "asset" and node.data.get("kind") == "style":
                merged["style_preset"] = node.data.get("name") or node.label
                break
    return merged


def _episodes(canvas: Canvas) -> list[dict[str, Any]]:
    raw = (canvas.meta or {}).get("episodes")
    if isinstance(raw, list) and raw:
        return [dict(item) for item in raw]
    # 老画布没有分集：整张画布当作第 1 集。
    return [
        {
            "no": 1,
            "title": (canvas.meta or {}).get("story_title") or canvas.name,
            "status": "in_progress",
        }
    ]


def _segments_all(canvas: Canvas) -> list[dict[str, Any]]:
    segments = []
    prompt_nodes = [n for n in canvas.nodes.values() if n.type == "prompt"]
    # 按 (集, 段号, 建图顺序) 排，保证编号稳定
    order = {nid: index for index, nid in enumerate(canvas.nodes)}
    prompt_nodes.sort(key=lambda n: (_episode_no(n), _segment_no(n, 0), order.get(n.id, 0)))
    for index, node in enumerate(prompt_nodes, 1):
        segments.append(_segment_view(canvas, node, fallback_no=index))
    return segments


def _clean_asset_name(name: str) -> str:
    """去掉节点标签里的「角色·/场景·/道具·/资产·」前缀，制作者看到的是人名。"""
    return re.sub(r"^(资产|角色|场景|道具|风格|音色)\s*·\s*", "", (name or "").strip()).strip()


def _active_look_image(canvas: Canvas, asset: Any) -> str:
    """资产当前形象的图；没有显式 looks 时退回节点自带的图片产物。"""
    if asset is None:
        return ""
    looks = _asset_looks(canvas, asset)
    active_id = str(asset.data.get("active_look_id") or "")
    chosen = next((look for look in looks if look.get("id") == active_id), None)
    if chosen is None:
        chosen = next((look for look in looks if look.get("image_path")), looks[0] if looks else None)
    if chosen and chosen.get("image_path"):
        return str(chosen["image_path"])
    media = _first_media(canvas, asset, "IMAGE")
    return str(media.get("path") or "")


def _reference_rows(canvas: Canvas, node: Any) -> list[dict[str, Any]]:
    """片段引用的参考素材。资产还没生成图时 path 为空，界面据此显示「未生成」。"""
    rows: list[dict[str, Any]] = []
    for ref in canvas.refs_for(node.id):
        asset = canvas.nodes.get(ref["node_id"])
        label = ""
        if asset is not None:
            label = str(asset.data.get("name") or asset.label or ref["node_id"])
        rows.append(
            {
                "ordinal": ref["ref"],
                "asset_id": ref["node_id"],
                "name": _clean_asset_name(label) or ref["node_id"],
                "path": str(ref.get("path") or "") or _active_look_image(canvas, asset),
                "type": str(ref.get("type") or ""),
                "kind": str((asset.data.get("kind") if asset is not None else "") or ""),
            }
        )
    return rows


def _segment_view(canvas: Canvas, node: Any, *, fallback_no: int = 0, with_detail: bool = False) -> dict[str, Any]:
    seg_no = _segment_no(node, fallback_no)
    prompt = _node_text(canvas, node)
    shots: list[dict[str, Any]] = []
    storyboard_node = None
    for item in canvas.text_inputs(node.id):
        source = canvas.nodes.get(item["node_id"])
        if source is not None and source.type == "storyboard":
            storyboard_node = source
            raw_shots = source.data.get("shots")
            if isinstance(raw_shots, list):
                # 分镜表和片段的对应关系有两种真实形态，必须分两级处理：
                # ① 分镜节点自己带 segment_no（Agent 逐段建节点时会写）——
                #    此时节点内的 shots 全是本段的，再按 shot.segment_no 过滤
                #    反而会因为 Agent 把每条都写成 1 而把 2..N 段过滤成空。
                # ② 一张分镜表覆盖全片（老画布，节点无 segment_no）——
                #    必须按 shot.segment_no 只取自己那一段，否则时长合计和台词
                #    会被别的片段污染。
                if source.data.get("segment_no") not in (None, ""):
                    shots = list(raw_shots)
                else:
                    shots = [
                        shot
                        for shot in raw_shots
                        if shot.get("segment_no") in (None, seg_no)
                    ]
    if storyboard_node is None:
        # 图上没有「分镜 → 提示词」边时（模型偶尔会漏连）按集号+片段号回找，
        # 否则片段详情会静默变成空表。写入侧的 _repair_links 负责把边补齐。
        episode = _episode_no(node)
        for candidate in canvas.nodes.values():
            if candidate.type != "storyboard":
                continue
            if _episode_no(candidate) != episode:
                continue
            if int(candidate.data.get("segment_no") or 0) != int(seg_no or 0):
                continue
            storyboard_node = candidate
            raw_shots = candidate.data.get("shots")
            if isinstance(raw_shots, list):
                shots = list(raw_shots)
            break
    duration, duration_source = _infer_duration(canvas, node, prompt, shots)
    refs = _reference_rows(canvas, node)
    name_by_id: dict[str, str] = {ref["asset_id"]: ref["name"] for ref in refs}
    # 正文由服务端从结构化字段渲染；人改过的（rendered_by=human）不覆盖。
    if shots and str(node.data.get("rendered_by") or "") != "human":
        rendered = _render_segment_text(_first_shot(shots, _segment_no(node, fallback_no)), name_by_id)
        if rendered:
            prompt = rendered

    gen_node = None
    for edge in canvas.outgoing(node.id):
        target = canvas.nodes.get(edge.target)
        if target is not None and target.type in {"video_gen", "image_gen"}:
            gen_node = target
            break

    takes = list(gen_node.data.get("takes") or []) if gen_node is not None else []
    current_take_id = str(gen_node.data.get("current_take_id") or "") if gen_node is not None else ""
    video = canvas.media_output(gen_node.id, "VIDEO") if gen_node is not None else {}

    view: dict[str, Any] = {
        "node_id": node.id,
        "generation_node_id": gen_node.id if gen_node is not None else "",
        "episode_no": _episode_no(node),
        "segment_no": _segment_no(node, fallback_no),
        "title": node.label,
        "status": _segment_status(gen_node, video, takes, current_take_id),
        "duration_s": duration,
        "duration_source": duration_source,
        "has_video": bool(video.get("path")),
        "video_path": video.get("path") or "",
        "thumbnail": str(gen_node.data.get("thumbnail") or "") if gen_node is not None else "",
        "keyframe_path": str((gen_node.data.get("keyframe") or {}).get("path") or "")
        if gen_node is not None
        else "",
        "take_count": len(takes),
        "current_take_id": current_take_id,
        "tags": list(gen_node.data.get("tags") or []) if gen_node is not None else [],
        "references": refs,
        "storyboard_node_id": storyboard_node.id if storyboard_node is not None else "",
        "orphaned": bool(node.data.get("orphaned")),
        **_stale_flags(canvas, gen_node, shots, refs),
    }

    if with_detail:
        findings = lint_segment(
            duration_s=duration,
            shots=shots,
            prompt=prompt,
            references=[{"asset_id": ref["asset_id"], "ordinal": ref["ordinal"]} for ref in refs],
            name_by_id={ref["asset_id"]: ref["name"] for ref in refs},
        )
        view.update(
            {
                "prompt": prompt,
                "shots": shots,
                "dialogue": findings["dialogue"],
                "lint": findings,
                "takes": takes,
                "keyframe": dict(gen_node.data.get("keyframe") or {}) if gen_node is not None else {},
            }
        )
    return view


def _fingerprint_refs(graph: Canvas, refs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """refs_hash 的输入：资产 id + 当前形象 id + 图路径（换形象/重生成都会变）。"""
    rows: list[dict[str, str]] = []
    for ref in refs:
        asset = graph.nodes.get(str(ref.get("asset_id") or ""))
        rows.append(
            {
                "asset_id": str(ref.get("asset_id") or ""),
                "active_look_id": str((asset.data.get("active_look_id") if asset is not None else "") or ""),
                "image_path": str(ref.get("path") or ""),
            }
        )
    return rows


def _stale_flags(graph: Canvas, gen_node: Any, shots: list[Any], refs: list[dict[str, Any]]) -> dict[str, Any]:
    """生成物（首帧/当前采用版）对「当前分镜 + 当前引用」的过期判定。

    None = 产物没有指纹（旧数据），显示「无指纹」而不算过期，避免升级即全体飘红。
    """
    cur_shots = shots_hash(shots)
    cur_refs = refs_hash(_fingerprint_refs(graph, refs))
    keyframe = dict((gen_node.data.get("keyframe") if gen_node is not None else {}) or {})
    takes = [t for t in ((gen_node.data.get("takes") if gen_node is not None else []) or []) if isinstance(t, Mapping)]
    current_take = next(
        (t for t in takes if t.get("take_id") == (gen_node.data.get("current_take_id") if gen_node is not None else None)),
        None,
    )

    def _compare(stored_shots: Any, stored_refs: Any) -> tuple[bool | None, str]:
        if not stored_shots and not stored_refs:
            return None, ""
        reasons: list[str] = []
        if stored_shots and cur_shots and stored_shots != cur_shots:
            reasons.append("分镜已变更")
        if stored_refs and cur_refs and stored_refs != cur_refs:
            reasons.append("形象已更换")
        if not reasons:
            return False, ""
        return True, "、".join(reasons)

    kf_stale, kf_reason = _compare(keyframe.get("shots_hash"), keyframe.get("refs_hash"))
    tk_stale, tk_reason = _compare(
        (current_take or {}).get("shots_hash"), (current_take or {}).get("refs_hash")
    )
    latest = str(takes[-1].get("take_id") or "") if takes else ""
    return {
        "keyframe_stale": kf_stale,
        "video_stale": tk_stale,
        "stale_reason": kf_reason or tk_reason,
        "latest_take_id": latest,
    }


def _segment_status(gen_node: Any, video: dict[str, Any], takes: list[Any], current_take_id: str) -> str:
    if gen_node is None:
        return "no_slot"
    run = _jobs.get(str(gen_node.data.get("active_job_id") or ""))
    if run and run.get("status") == "running":
        return "generating"
    if video.get("path") or any(str(t.get("path") or "") for t in takes if isinstance(t, Mapping)):
        return "done"
    return "pending"


def _asset_looks(canvas: Canvas, node: Any) -> list[dict[str, Any]]:
    """形象的统一形状。

    画布里的 `data.looks` 是 Agent 写的，字段不保证齐（实测漏过 `id`，前端一取就 500）。
    形状的兜底放在这里，不要指望模型每次都写全。
    """
    explicit = node.data.get("looks")
    if isinstance(explicit, list) and explicit:
        looks: list[dict[str, Any]] = []
        for index, item in enumerate(explicit):
            if not isinstance(item, dict):
                continue
            look = dict(item)
            look.setdefault("id", f"look_{node.id}_{index + 1}")
            look.setdefault("name", f"形象 {index + 1}")
            look.setdefault("prompt", "")
            look.setdefault("image_path", "")
            looks.append(look)
        if looks:
            return looks
    media = _first_media(canvas, node, "IMAGE")
    return [
        {
            "id": f"look_{node.id}_default",
            "name": node.data.get("look_name") or "默认形象",
            "image_path": media.get("path") or "",
            "prompt": str(node.data.get("image_prompt") or ""),
            "locked": bool(node.data.get("locked")),
            "appearances": list(node.data.get("appearances") or []),
            "generated": bool(media.get("path")),
        }
    ]


def _tag_list(value: Any) -> list[str]:
    """一致性标签在画布里可能是字符串（顿号分隔）也可能是数组，统一成数组。"""
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[、,，;；]", value) if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _assets_view(canvas: Canvas) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {"character": [], "scene": [], "prop": [], "style": []}
    for node in canvas.nodes.values():
        if node.type != "asset":
            continue
        kind = str(node.data.get("kind") or "prop")
        looks = _asset_looks(canvas, node)
        item = {
            "node_id": node.id,
            "kind": kind,
            "name": _clean_asset_name(str(node.data.get("name") or node.label or node.id)),
            "role": str(node.data.get("role") or "supporting"),
            "description": str(node.data.get("description") or ""),
            "consistency_tags": _tag_list(node.data.get("consistency_tags")),
            "voice_id": str(node.data.get("voice_id") or ""),
            "spec": str(node.data.get("spec") or "").strip() or canvas.asset_desc(node.id),
            "looks": looks,
            "look_total": len(looks),
            "look_ready": sum(1 for look in looks if look.get("image_path")),
            "active_look_id": str(node.data.get("active_look_id") or (looks[0]["id"] if looks else "")),
            "status": node.status,
        }
        buckets.setdefault(kind, []).append(item)
    for items in buckets.values():
        items.sort(key=lambda item: (item["look_ready"] == 0, item["name"]))
    return buckets


def _counts(canvas: Canvas) -> dict[str, Any]:
    assets = _assets_view(canvas)
    segments = _segments_all(canvas)

    def tally(items: Iterable[dict[str, Any]]) -> dict[str, int]:
        items = list(items)
        return {"ready": sum(1 for item in items if item["look_ready"] > 0), "total": len(items)}

    return {
        "assets": {
            "character": tally(assets["character"]),
            "scene": tally(assets["scene"]),
            "prop": tally(assets["prop"]),
            "voice": {"ready": 0, "total": sum(1 for a in assets["character"] if a["voice_id"])},
        },
        "segments": {
            "done": sum(1 for seg in segments if seg["status"] == "done"),
            "total": len(segments),
        },
    }


PIPELINE_STEPS = [
    ("script", "剧本"),
    ("rewrite", "AI 改编"),
    ("assets", "资产"),
    ("videos", "片段生成"),
    ("export", "导出"),
]


def _pipeline(canvas: Canvas) -> list[dict[str, str]]:
    counts = _counts(canvas)
    has_script = any(
        node.type == "text" and _node_text(canvas, node).strip() for node in canvas.nodes.values()
    )
    has_storyboard = any(node.type == "storyboard" for node in canvas.nodes.values())
    asset_total = sum(counts["assets"][k]["total"] for k in ("character", "scene", "prop"))
    asset_ready = sum(counts["assets"][k]["ready"] for k in ("character", "scene", "prop"))
    seg_done = counts["segments"]["done"]
    seg_total = counts["segments"]["total"]
    exported = bool((canvas.meta or {}).get("production", {}).get("export_done"))

    states = {
        "script": "done" if has_script else "pending",
        "rewrite": "done" if has_storyboard else ("doing" if has_script else "pending"),
        "assets": "done" if asset_total and asset_ready >= asset_total else ("doing" if asset_total else "pending"),
        "videos": "done" if seg_total and seg_done >= seg_total else ("doing" if seg_done else ("pending" if not seg_total else "pending")),
        "export": "done" if exported else ("doing" if seg_total and seg_done else "pending"),
    }
    return [{"key": key, "label": label, "state": states[key]} for key, label in PIPELINE_STEPS]


# ---------------------------------------------------------------------------
# 读取接口
# ---------------------------------------------------------------------------


@router.get("/project")
async def get_project(canvas: str) -> dict[str, Any]:
    node = await _load(canvas)
    episodes = _episodes(node)
    counts = _counts(node)
    return {
        "canvas": node.name,
        "revision": node.revision,
        "project": _project_meta(node),
        "title": (node.meta or {}).get("story_title") or node.name,
        "summary": (node.meta or {}).get("story_summary") or "",
        "episode": int((node.meta or {}).get("episode") or (episodes[0].get("no") or 1)),
        "episodes": [
            {
                **episode,
                "segment_total": sum(1 for s in _segments_all(node) if s["episode_no"] == int(episode.get("no") or 1)),
                "segment_done": sum(
                    1 for s in _segments_all(node)
                    if s["episode_no"] == int(episode.get("no") or 1) and s["status"] == "done"
                ),
            }
            for episode in episodes
        ],
        "pipeline": _pipeline(node),
        "counts": counts,
        "thresholds": dict(THRESHOLDS),
        "story_analysis": dict((node.meta or {}).get("story_analysis") or {}),
        "asset_tables": dict((node.meta or {}).get("asset_tables") or {}),
        "skills": dict(_project_meta(node).get("skills") or {}),
    }


@router.patch("/project")
async def patch_project(canvas: str, body: ProjectPatch) -> dict[str, Any]:
    node = await _load(canvas)
    meta = dict(node.meta or {})
    project = dict(meta.get("project") or {})
    for field, value in body.model_dump(exclude_none=True).items():
        if field in {"story_title", "story_summary", "episode"}:
            meta[field] = value
        else:
            project[field] = value
    meta["project"] = project
    node.meta = meta
    revision = await _save(node)
    return {"ok": True, "revision": revision, "project": _project_meta(node)}


# ---------------------------------------------------------------------------
# 剧本理解 pipeline（确定性四卡链）：NDJSON 流式、单飞 409、卡间可打断。
# 与资产解析共用 _story_runs 注册表——同一画布同时只允许一个分析/解析任务。
# ---------------------------------------------------------------------------
_story_runs: dict[str, dict[str, Any]] = {}


class StoryAnalysisRunRequest(BaseModel):
    canvas: str
    cards: list[str] | None = None  # 缺省全量；重跑可只指定要重生成的卡


class CancelBody(BaseModel):
    canvas: str


def _ndjson_line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + chr(10)).encode("utf-8")


def _skill_version(node: Canvas, key: str, fallback: str) -> str:
    selected = str((_project_meta(node).get("skills") or {}).get(key) or "")
    return selected.split("@", 1)[1] if "@" in selected else fallback


def _story_script_text(node: Canvas) -> str:
    """全剧本 = 各集正文按集序拼接（读取统一走 Canvas.read_text）。"""
    parts = []
    for episode in _episodes(node):
        node_id = str(episode.get("script_node_id") or "")
        body = node.read_text(node_id) if node_id and node_id in node.nodes else ""
        if not body.strip():
            continue
        parts.append(f"第{episode.get('no')}集 {episode.get('title') or ''}\n{body}")
    return chr(10).join(parts)


async def _merge_story_analysis(canvas_name: str, patch: dict[str, Any], *, skill_tag: dict | None = None) -> None:
    """把已完成卡合并落盘——打断/崩溃只丢未完成卡，不丢已完成的。

    per-card 落盘与收尾落盘可能交错，必须走统一的冲突重放，不能拿旧 revision 硬写。
    """
    async_repo = _registry.canvas_repository

    def mutate(graph: Canvas) -> bool:
        meta = dict(graph.meta or {})
        analysis = dict(meta.get("story_analysis") or {})
        analysis.update(patch)
        if skill_tag:
            analysis["skill"] = skill_tag
        analysis["updated_at"] = int(time.time())
        # 归一到 schema 全形状：前端拿到的是完整四卡结构（未完成的卡是空值）
        meta["story_analysis"] = StoryAnalysis(**analysis).model_dump()
        graph.meta = meta
        return True

    await mutate_canvas_with_retry(async_repo, canvas_name, mutate)


@router.post("/story-analysis/run")
async def story_analysis_run(body: StoryAnalysisRunRequest) -> StreamingResponse:
    _require_configured()
    node = await _load(body.canvas)
    active = _story_runs.get(body.canvas)
    if active is not None and not active["task"].done():
        raise HTTPException(status_code=409, detail="这个项目正在解析剧本/生成资产表")
    script_text = _story_script_text(node)
    if not script_text.strip():
        raise HTTPException(status_code=400, detail="还没有剧本正文，先在剧本页导入")
    if body.cards:
        unknown = [c for c in body.cards if c not in CARDS]
        if unknown:
            raise HTTPException(status_code=400, detail=f"未知卡片：{', '.join(unknown)}")

    cancel = asyncio.Event()
    queue: asyncio.Queue = asyncio.Queue()
    version = _skill_version(node, "script_analysis", "1.0.0")
    prior = dict((node.meta or {}).get("story_analysis") or {})
    llm = _registry.llm
    canvas_name = body.canvas

    async def pipeline() -> None:
        def on_event(event: dict[str, Any]) -> None:
            if event.get("type") == "analysis_done":
                return  # 落盘后由这里统一发，避免双份
            queue.put_nowait(event)

        try:
            analysis = await run_story_analysis(
                script_text,
                llm,
                cards=body.cards or None,
                prior=prior if body.cards else None,  # 全量重跑不背旧产物；指定卡才保留其余卡
                skill_tag={"name": "script-analysis", "version": version},
                on_event=on_event,
                cancel=cancel,
            )
            await _merge_story_analysis(canvas_name, {}, skill_tag={"name": "script-analysis", "version": version})
            graph = await _load(canvas_name)
            final = dict((graph.meta or {}).get("story_analysis") or {})
            queue.put_nowait({"type": "analysis_done", "analysis": final, "cancelled": cancel.is_set()})
        except asyncio.CancelledError:  # pragma: no cover - 任务被外层取消
            queue.put_nowait({"type": "analysis_done", "analysis": {}, "cancelled": True})
        finally:
            queue.put_nowait(None)

    task = asyncio.create_task(pipeline())
    _story_runs[canvas_name] = {"task": task, "cancel": cancel}

    async def stream():
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                if event.get("type") == "card_done":
                    await _merge_story_analysis(canvas_name, {event["key"]: event["data"]})
                yield _ndjson_line(event)
        finally:
            _story_runs.pop(canvas_name, None)

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@router.post("/story-analysis/cancel")
async def story_analysis_cancel(body: CancelBody) -> dict[str, Any]:
    run = _story_runs.get(body.canvas)
    if run is None or run["task"].done():
        return {"ok": False}
    run["cancel"].set()   # 卡间检查用：已完成卡保留
    run["task"].cancel()  # 打断在飞行中的 LLM 调用（await 点直接抛 CancelledError）
    return {"ok": True}


# ---------------------------------------------------------------------------
# 资产表（草稿区）：parse 只写 meta.asset_tables；sync 幂等只增不改不删。
# 与剧本理解共用 _story_runs 单飞约束。
# ---------------------------------------------------------------------------
def _episodes_texts(node: Canvas) -> list[dict[str, Any]]:
    out = []
    for episode in _episodes(node):
        node_id = str(episode.get("script_node_id") or "")
        body = node.read_text(node_id) if node_id and node_id in node.nodes else ""
        if body.strip():
            out.append({"no": int(episode.get("no") or 1), "text": body})
    return out


def _existing_assets_projection(graph: Canvas) -> list[dict[str, Any]]:
    """资产库投影（merge 纯函数的输入形状）。图片/锁等库字段不进投影——同步永不碰它们。"""
    out = []
    for node in graph.nodes.values():
        if node.type != "asset":
            continue
        kind = str(node.data.get("kind") or "")
        if kind not in {"character", "scene", "prop"}:
            continue
        looks = _asset_looks(graph, node)
        out.append({
            "node_id": node.id,
            "kind": kind,
            "name": _clean_asset_name(str(node.data.get("name") or node.label or node.id)),
            "description": str(node.data.get("description") or ""),
            "bio": str(node.data.get("bio") or ""),
            "looks": [{"name": str(lk.get("name") or ""), "prompt": str(lk.get("prompt") or "")} for lk in looks],
        })
    return out


class ParseRequest(BaseModel):
    canvas: str


@router.post("/asset-tables/parse")
async def asset_tables_parse(body: ParseRequest) -> StreamingResponse:
    _require_configured()
    node = await _load(body.canvas)
    active = _story_runs.get(body.canvas)
    if active is not None and not active["task"].done():
        raise HTTPException(status_code=409, detail="这个项目正在解析剧本/生成资产表")
    episodes = _episodes_texts(node)
    if not episodes:
        raise HTTPException(status_code=400, detail="还没有剧本正文，先在剧本页导入")

    cancel = asyncio.Event()
    queue: asyncio.Queue = asyncio.Queue()
    version = _skill_version(node, "asset", "2.0.0")
    known = [str(c.get("name") or "") for c in ((node.meta or {}).get("asset_tables") or {}).get("characters", [])]
    llm = _registry.llm
    canvas_name = body.canvas

    async def pipeline() -> None:
        def on_event(event: dict[str, Any]) -> None:
            if event.get("type") == "parse_done":
                return  # 落盘后由这里统一发，避免双份
            queue.put_nowait(event)

        try:
            tables = await parse_asset_tables(
                episodes,
                llm,
                known=known,
                on_event=on_event,
                cancel=cancel,
            )
            tables["skill"] = {"name": "asset", "version": version}
            tables["updated_at"] = int(time.time())
            # 只覆写草稿表，绝不触碰资产库（原则：表是草稿区）
            await mutate_canvas_with_retry(
                _registry.canvas_repository,
                canvas_name,
                lambda graph: _write_asset_tables(graph, tables),
            )
            queue.put_nowait({"type": "parse_done", "tables": tables})
        except asyncio.CancelledError:  # pragma: no cover
            queue.put_nowait({"type": "parse_done", "tables": {}})
        finally:
            queue.put_nowait(None)

    task = asyncio.create_task(pipeline())
    _story_runs[canvas_name] = {"task": task, "cancel": cancel}

    async def stream():
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield _ndjson_line(event)
        finally:
            _story_runs.pop(canvas_name, None)

    return StreamingResponse(stream(), media_type="application/x-ndjson")


def _write_asset_tables(graph: Canvas, tables: dict[str, Any]) -> bool:
    meta = dict(graph.meta or {})
    meta["asset_tables"] = AssetTables(**tables).model_dump()
    graph.meta = meta
    return True


class AssetTablesRequest(BaseModel):
    canvas: str
    tables: dict[str, Any]


@router.put("/asset-tables")
async def put_asset_tables(body: AssetTablesRequest) -> dict[str, Any]:
    """用户在网格里编辑整表提交：schema 校验后覆写（skill/synced_at 沿用旧值）。"""
    node = await _load(body.canvas)
    existing = dict((node.meta or {}).get("asset_tables") or {})
    tables = AssetTables(**{**existing, **body.tables}).model_dump()
    await mutate_canvas_with_retry(
        _registry.canvas_repository, body.canvas, lambda graph: _write_asset_tables(graph, tables)
    )
    return {"ok": True, "tables": tables}


class SyncRequest(BaseModel):
    canvas: str


@router.post("/asset-tables/sync")
async def sync_asset_tables(body: SyncRequest) -> dict[str, Any]:
    """表 → 库：幂等合并（只增不改不删），报告可见。"""
    node = await _load(body.canvas)
    tables_raw = (node.meta or {}).get("asset_tables") or {}
    if not isinstance(tables_raw, dict) or not (
        tables_raw.get("characters") or tables_raw.get("scenes") or tables_raw.get("props")
    ):
        raise HTTPException(status_code=400, detail="还没有资产表，先解析或手工编辑")
    tables = AssetTables(**tables_raw)
    plan = merge_asset_tables(_existing_assets_projection(node), tables)
    report: dict[str, Any] = {"merged": plan["merged"], "skipped": plan["untouched"], "synced_at": int(time.time())}

    def mutate(graph: Canvas) -> bool:
        created_assets = created_looks = 0
        for item in plan["create_assets"]:
            data: dict[str, Any] = {
                "kind": item["kind"],
                "name": item["name"],
                "description": str(item.get("description") or ""),
            }
            if item.get("bio"):
                data["bio"] = str(item["bio"])
            node_new = graph.add_node("asset", item["name"], data=data)
            looks = []
            for index, look in enumerate(item.get("looks", []), 1):
                looks.append({"id": f"look_{node_new.id}_{index}", "name": look["name"], "prompt": ""})
                created_looks += 1
            if looks:
                # add_node 对 data 是浅拷贝：回写必须落在返回的 node 上，不能写回局部 dict
                node_new.data["looks"] = looks
                node_new.data["active_look_id"] = looks[0]["id"]
            created_assets += 1
        for item in plan["add_looks"]:
            target = graph.nodes.get(item["node_id"])
            if target is None or target.type != "asset":
                continue
            looks = target.data.get("looks")
            if not isinstance(looks, list):
                looks = []
            looks.append({"id": f"look_{target.id}_{len(looks) + 1}", "name": item["look"]["name"], "prompt": ""})
            target.data["looks"] = looks
            created_looks += 1
        for item in plan["fill_fields"]:
            target = graph.nodes.get(item["node_id"])
            if target is None or target.type != "asset":
                continue
            if item["field"] == "bio" and not str(target.data.get("bio") or "").strip():
                target.data["bio"] = str(item["value"])
            elif item["field"] == "description" and not str(target.data.get("description") or "").strip():
                target.data["description"] = str(item["value"])
        meta = dict(graph.meta or {})
        tables_meta = dict(meta.get("asset_tables") or {})
        tables_meta["synced_at"] = report["synced_at"]
        meta["asset_tables"] = AssetTables(**tables_meta).model_dump()
        graph.meta = meta
        report["created_assets"] = created_assets
        report["created_looks"] = created_looks
        return True

    await mutate_canvas_with_retry(_registry.canvas_repository, body.canvas, mutate)
    return report


@router.get("/looks/export-prompt")
async def export_look_prompt(canvas: str, asset: str, look: str) -> PlainTextResponse:
    """生图占位：不接 API，导出提示词文本文件，执行权在用户手里。"""
    graph = await _load(canvas)
    node = graph.nodes.get(asset)
    if node is None or node.type != "asset":
        raise HTTPException(status_code=404, detail=f"资产不存在：{asset}")
    target = next((lk for lk in _asset_looks(graph, node) if lk.get("id") == look), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"形象不存在：{look}")
    prompt = str(target.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="该形象还没有提示词，先在资产页写好描述")
    name = _clean_asset_name(str(node.data.get("name") or node.label or node.id))
    filename = quote(f"{name}-{target.get('name', '形象')}-提示词.txt")
    return PlainTextResponse(
        prompt,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


class SkillsRequest(BaseModel):
    canvas: str
    skills: dict[str, str]


@router.post("/project/skills")
async def set_project_skills(body: SkillsRequest) -> dict[str, Any]:
    """保存项目的 Skill 选择（新建弹窗或项目设置提交）。"""
    node = await _load(body.canvas)
    meta = dict(node.meta or {})
    project = dict(meta.get("project") or {})
    project["skills"] = _validated_skills(body.skills)
    meta["project"] = project
    node.meta = meta
    revision = await _save(node)
    return {"ok": True, "revision": revision, "skills": project["skills"]}


@router.get("/assets")
async def get_assets(canvas: str) -> dict[str, Any]:
    node = await _load(canvas)
    buckets = _assets_view(node)
    counts = _counts(node)
    return {"canvas": node.name, "revision": node.revision, **buckets, "counts": counts}


@router.get("/segments")
async def get_segments(canvas: str, episode: int | None = None) -> dict[str, Any]:
    node = await _load(canvas)
    segments = _segments_all(node)
    if episode is not None:
        segments = [seg for seg in segments if seg["episode_no"] == episode]
    return {
        "canvas": node.name,
        "revision": node.revision,
        "episode": episode,
        "total": len(segments),
        "done": sum(1 for seg in segments if seg["status"] == "done"),
        "segments": segments,
    }


@router.get("/segments/{node_id}")
async def get_segment(node_id: str, canvas: str) -> dict[str, Any]:
    graph = await _load(canvas)
    target = graph.nodes.get(node_id)
    if target is None or target.type != "prompt":
        raise HTTPException(status_code=404, detail=f"片段不存在：{node_id}")
    return _segment_view(graph, target, with_detail=True)


# ---------------------------------------------------------------------------
# 写入接口
# ---------------------------------------------------------------------------


@router.get("/episodes/{episode_no}/storyboard")
async def episode_storyboard(episode_no: int, canvas: str) -> dict[str, Any]:
    """首帧墙：整集片段按段序返回缩略图/状态/过期/台词首句。

    首帧是全链路最便宜的全局质检点——批量出视频之前，先把整集静帧过一遍。
    """
    graph = await _load(canvas)
    cells: list[dict[str, Any]] = []
    for view in _segments_all(graph):
        if int(view.get("episode_no") or 1) != int(episode_no):
            continue
        detail = _segment_view(graph, graph.nodes.get(view["node_id"]), with_detail=True)
        dialogue = detail.get("dialogue") or []
        if detail.get("keyframe_stale") or detail.get("video_stale"):
            staleness = "stale"
        elif not detail.get("keyframe_path"):
            staleness = "none"
        elif detail.get("keyframe_stale") is None:
            staleness = "unknown"  # 旧产物无指纹，不当过期处理
        else:
            staleness = "fresh"
        cells.append(
            {
                "node_id": detail.get("node_id"),
                "segment_no": detail.get("segment_no"),
                "title": detail.get("title"),
                "duration_s": detail.get("duration_s"),
                "thumb": detail.get("keyframe_path") or detail.get("thumbnail") or "",
                "has_video": bool(detail.get("has_video")),
                "status": detail.get("status"),
                "staleness": staleness,
                "stale_reason": detail.get("stale_reason") or "",
                "dialogue": (str(dialogue[0].get("line") or "") if dialogue else ""),
                "take_count": detail.get("take_count") or 0,
            }
        )
    return {"ok": True, "episode_no": episode_no, "cells": cells}


@router.post("/segments/{node_id}/prompt")
async def save_prompt(node_id: str, canvas: str, body: PromptPatch) -> dict[str, Any]:
    """保存片段提示词。写回 content.md（若有）或 outputs 内联值。"""
    graph = await _load(canvas)
    target = graph.nodes.get(node_id)
    if target is None or target.type != "prompt":
        raise HTTPException(status_code=404, detail=f"片段不存在：{node_id}")

    relative = target.data.get("content_path")
    if relative:
        path = graph.root() / str(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.prompt, encoding="utf-8")
    else:
        outputs = dict(target.outputs)
        # 与画布路由 server/app.py:save_canvas_node_text 保持同一契约：只留一个
        # STRING 槽位。read_text() 先读 outputs.prompt，另一槽位的旧值会盖住新值。
        slot = dict(outputs.get("prompt") or outputs.get("text") or {})
        slot.setdefault("data_type", "STRING")
        slot["value"] = body.prompt
        outputs["prompt"] = slot
        outputs.pop("text", None)
        target.outputs = outputs

    # 人改过的正文优先，服务端之后不再覆盖（渲染只在 rendered_by != "human" 时进行）
    target.data["rendered_by"] = "human"
    target.data["prompt"] = body.prompt

    if body.duration_s is not None:
        target.data["duration_s"] = float(body.duration_s)
    if body.references is not None:
        target.data["references"] = body.references
    target.status = "ready"
    revision = await _save(graph)
    return {"ok": True, "revision": revision, **_segment_view(graph, target, with_detail=True)}


@router.post("/segments/{node_id}/tags")
async def save_tags(node_id: str, canvas: str, body: TagsPatch) -> dict[str, Any]:
    graph = await _load(canvas)
    target = graph.nodes.get(node_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"片段不存在：{node_id}")
    gen = _generation_node(graph, target)
    if gen is None:
        raise HTTPException(status_code=400, detail="该片段还没有生成节点，无法打标")
    gen.data["tags"] = list(dict.fromkeys([str(tag) for tag in body.tags]))
    revision = await _save(graph)
    return {"ok": True, "revision": revision, "tags": gen.data["tags"]}


def _generation_node(graph: Canvas, prompt_node: Any) -> Any:
    for edge in graph.outgoing(prompt_node.id):
        target = graph.nodes.get(edge.target)
        if target is not None and target.type in {"video_gen", "image_gen"}:
            return target
    return None


@router.post("/segments/{node_id}/take")
async def select_take(node_id: str, canvas: str, body: TakeSelect) -> dict[str, Any]:
    """把某一版设为当前版：takes 存历史，outputs 存当前指针（对齐主流做法）。"""
    graph = await _load(canvas)
    prompt_node = graph.nodes.get(node_id)
    if prompt_node is None:
        raise HTTPException(status_code=404, detail=f"片段不存在：{node_id}")
    gen = _generation_node(graph, prompt_node)
    if gen is None:
        raise HTTPException(status_code=400, detail="该片段还没有生成节点")
    takes = list(gen.data.get("takes") or [])
    chosen = next((item for item in takes if item.get("take_id") == body.take_id), None)
    if chosen is None:
        raise HTTPException(status_code=404, detail=f"版本不存在：{body.take_id}")
    gen.data["current_take_id"] = body.take_id
    outputs = dict(gen.outputs)
    outputs["video"] = {
        "data_type": "VIDEO",
        "value": {
            "path": chosen.get("path", ""),
            "mime": chosen.get("mime") or "video/mp4",
            "provider": chosen.get("provider") or "",
            "mock": bool(chosen.get("mock")),
        },
    }
    gen.outputs = outputs
    gen.status = "done"
    revision = await _save(graph)
    return {"ok": True, "revision": revision, **_segment_view(graph, prompt_node, with_detail=True)}


@router.patch("/assets/{node_id}")
async def patch_asset(node_id: str, body: AssetPatch, canvas: str) -> dict[str, Any]:
    """资产字段编辑（本期仅 voice_id）：音色是「声音的参考图」，逐句配音按角色绑定。"""
    graph = await _load(canvas)
    node = graph.nodes.get(node_id)
    if node is None or node.type != "asset":
        raise HTTPException(status_code=404, detail=f"资产不存在：{node_id}")
    if body.voice_id is not None:
        node.data["voice_id"] = str(body.voice_id).strip()
    revision = await _save(graph)
    return {"ok": True, "revision": revision, "node_id": node_id}


@router.post("/assemble")
async def assemble_episode(canvas: str) -> dict[str, Any]:
    """按片段顺序把已出片的视频接进一条时间线，供成片管线合成整集。"""
    graph = await _load(canvas)
    segments = _segments_all(graph)
    if not segments:
        raise HTTPException(status_code=400, detail="还没有任何片段")
    ordered = [seg for seg in segments if seg.get("generation_node_id")]
    if not ordered:
        raise HTTPException(status_code=400, detail="片段还没有生成节点，先跑一次生成")
    missing = [seg["segment_no"] for seg in ordered if not seg.get("has_video")]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"第 {', '.join(str(no) for no in missing)} 段还没有出片，补齐后再拼接",
        )

    clip_order = [seg["generation_node_id"] for seg in ordered]
    title = str((graph.meta or {}).get("story_title") or graph.name)
    timeline = next((node for node in graph.nodes.values() if node.type == "timeline"), None)
    if timeline is None:
        max_x = max((float(node.position.get("x") or 0) for node in graph.nodes.values()), default=0.0)
        timeline = _new_node(
            node_id=f"n_{uuid.uuid4().hex[:10]}",
            node_type="timeline",
            label=f"{title}·整集",
            data={"episode_no": ordered[0].get("episode_no") or 1, "clip_order": []},
            position={"x": max_x + 340.0, "y": 120.0},
            status="ready",
        )
        graph.nodes[timeline.id] = timeline

    timeline.data["clip_order"] = clip_order
    timeline.status = "ready"
    # 输出规格跟着项目走：竖屏项目必须出竖屏成片，否则白做
    project = _project_meta(graph)
    width, height = _size_pair(
        _default_size(str(project.get("aspect_ratio") or "9:16"), str(project.get("quality") or "480P"))
    )
    timeline.data["output"] = {"width": width, "height": height, "fps": 24}

    wanted = set(clip_order)
    stale = [
        edge.id
        for edge in graph.incoming(timeline.id)
        if edge.target_handle == "video" and edge.source not in wanted
    ]
    if stale:
        graph.remove_edges(stale)
    linked = {edge.source for edge in graph.incoming(timeline.id) if edge.target_handle == "video"}
    connected = 0
    for node_id in clip_order:
        if node_id in linked:
            continue
        graph.connect(node_id, "video", timeline.id, "video")
        connected += 1

    revision = await _save(graph)
    return {
        "ok": True,
        "revision": revision,
        "timeline_node_id": timeline.id,
        "clip_count": len(clip_order),
        "connected": connected,
        "duration_s": round(sum(float(seg.get("duration_s") or 0) for seg in ordered), 2),
    }


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def _first_shot(shots: list[dict[str, Any]], shot_no: int) -> dict[str, Any]:
    """按镜头号取子镜头；取不到就退回第一个（老画布可能没有 no 字段）。"""
    for shot in shots:
        try:
            if int(shot.get("no") or 0) == int(shot_no):
                return shot
        except (TypeError, ValueError):
            continue
    return shots[0] if shots else {}


_SHOT_SIZE_CODE = {
    "极特写": "ECU",
    "大特写": "ECU",
    "特写": "CU",
    "近景": "MCU",
    "中景": "MS",
    "全景": "LS",
    "大全景": "ELS",
    "远景": "LS",
    "大远景": "ELS",
    "空镜": "ELS",
}


def _shot_size_text(size: str) -> str:
    """景别写成「代码 + 中文」，模型两头都认（skills/director 第三节）。"""
    size = (size or "").strip()
    if not size:
        return ""
    code = _SHOT_SIZE_CODE.get(size, "")
    return f"{code}（{size}）" if code else size


def _shot_seconds(shot: dict[str, Any]) -> float:
    """一镜时长：新字段 duration_s 优先，老画布退回 duration。"""
    for key in ("duration_s", "duration"):
        try:
            value = float(shot.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def _name_of(item: dict[str, Any], name_by_id: dict[str, str]) -> str:
    """主体/道具名字：资产名优先，其次资产 id，最后 item 自带的 name。"""
    key = str(item.get("id") or "").strip()
    return name_by_id.get(key) or str(item.get("name") or "").strip() or key


def _camera_text(shot: dict[str, Any]) -> str:
    camera = shot.get("camera") if isinstance(shot.get("camera"), dict) else {}
    height = str(camera.get("height") or "").strip()
    angle = str(camera.get("angle") or "").strip()
    move = str(camera.get("movement") or shot.get("camera_move") or "").strip()
    bits: list[str] = []
    if height:
        bits.append(f"机位高度{height}")
    if angle:
        bits.append(f"角度{angle}")
    if move:
        bits.append(f"运镜{move}")
    return "，".join(bits)


def _subject_text(shot: dict[str, Any], name_by_id: dict[str, str]) -> str:
    """主体块：站位、视线、起止状态都从字段读；没有新字段才退回老 subject。"""
    subjects = shot.get("subjects")
    if isinstance(subjects, list) and subjects:
        rows: list[str] = []
        for item in subjects:
            if not isinstance(item, dict):
                continue
            name = _name_of(item, name_by_id)
            if not name:
                continue
            attrs: list[str] = []
            if item.get("screen_pos"):
                attrs.append(str(item["screen_pos"]))
            if item.get("gaze"):
                attrs.append(f"视线：{item['gaze']}")
            if item.get("start_state"):
                attrs.append(f"起始状态：{item['start_state']}")
            if item.get("end_state"):
                attrs.append(f"末态：{item['end_state']}")
            rows.append(f"@{name}" + (f"（{'；'.join(attrs)}）" if attrs else ""))
        if rows:
            return "画面主体：" + "；".join(rows)
    legacy = str(shot.get("subject") or "").strip()
    return f"画面主体：{legacy}" if legacy else ""


def _props_text(shot: dict[str, Any], name_by_id: dict[str, str]) -> str:
    props = shot.get("props")
    if not isinstance(props, list):
        return ""
    rows: list[str] = []
    for item in props:
        if not isinstance(item, dict):
            continue
        name = _name_of(item, name_by_id)
        if not name:
            continue
        state = str(item.get("state") or "").strip()
        rows.append(f"@{name}" + (f"（{state}）" if state else ""))
    return "道具：" + "；".join(rows) if rows else ""


def _light_text(shot: dict[str, Any]) -> str:
    light = shot.get("light") if isinstance(shot.get("light"), dict) else {}
    bits: list[str] = []
    for label, key in (("方向", "direction"), ("色温", "color"), ("对比", "contrast")):
        value = str(light.get(key) or "").strip()
        if value:
            bits.append(f"{label}{value}")
    return "光线：" + "，".join(bits) if bits else ""


def _dialogue_text(shot: dict[str, Any]) -> str:
    dialogue = shot.get("dialogue") if isinstance(shot.get("dialogue"), dict) else {}
    words = str(dialogue.get("text") or "").strip()
    if not words:
        return ""
    try:
        seconds = float(dialogue.get("duration_s") or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds:
        tail = f"（约 {seconds:g} 秒）" if words[-1] in "。！？…" else f"，约 {seconds:g} 秒"
    else:
        tail = ""
    return f"台词（同期声，原文一字不改）：{words}{tail}"


def _forbidden_text(shot: dict[str, Any]) -> str:
    forbidden = shot.get("forbidden")
    if not isinstance(forbidden, list):
        return ""
    rows = [str(item).strip() for item in forbidden if str(item or "").strip()]
    return "禁止：" + "；".join(rows) if rows else ""


def _shot_prompt(
    shot: dict[str, Any], ref_names: list[str], *, name_by_id: dict[str, str] | None = None
) -> str:
    """把这一镜的结构化字段渲染成视频段正文（服务端渲染，人不再手写）。

    视频层铁律（skills/director/SKILL.md 第三节）：
    - 只写动作，不重描外貌——ref 与文字是权重冲突，重描会让参考图失效、角色换脸；
    - 动作要写「起点 → 可见末态」，「他很愤怒」不算末态，「下颌绷紧、手背青筋」才算；
    - 否定式要写成正面陈述，被禁词根留在提示词里就会被当画面词渲染。
    """
    mapping = name_by_id or {}
    size = _shot_size_text(str(shot.get("shot_size") or ""))
    camera = _camera_text(shot)
    action = str(shot.get("action") or "").strip()
    focus = str(shot.get("focus") or "").strip()
    sound = str(shot.get("sound") or "").strip()
    lines: list[str] = []
    if size or camera:
        lines.append(f"镜头：{size}{('，' + camera) if camera else ''}。")
    subject_text = _subject_text(shot, mapping)
    if subject_text:
        lines.append(subject_text + "。")
    props_text = _props_text(shot, mapping)
    if props_text:
        lines.append(props_text + "。")
    if action:
        lines.append(f"动作（本镜只演这一条，从起始状态演到可见末态）：{action}。")
    if focus:
        lines.append(f"焦点：{focus}。")
    if ref_names:
        lines.append(
            "身份与外观完全按参考素材（" + "、".join(f"@{name}" for name in ref_names) + "），"
            "不改变脸型、服装、泥污与人物位置。"
        )
    light_text = _light_text(shot)
    if light_text:
        lines.append(light_text + "。")
    dialogue_text = _dialogue_text(shot)
    if dialogue_text:
        lines.append(dialogue_text + "。")
    if sound:
        lines.append(f"声音：{sound}")
    forbidden_text = _forbidden_text(shot)
    if forbidden_text:
        lines.append(forbidden_text + "。")
    lines.append("单镜头连续拍摄，中间不切镜、不换机位、不出现新角色。")
    lines.append("画面中不出现任何文字、字幕、水印；不生成 BGM。")
    return "\n".join(lines)


def _render_segment_text(shot: dict[str, Any], name_by_id: dict[str, str] | None = None) -> str:
    """服务端渲染的片段正文：只由字段决定。"""
    if not isinstance(shot, dict) or not shot:
        return ""
    return _shot_prompt(shot, [], name_by_id=name_by_id)


_ASPECT_LABEL = {
    "9:16": "竖屏 9:16",
    "3:4": "竖屏 3:4",
    "16:9": "横屏 16:9",
    "4:3": "横屏 4:3",
    "1:1": "方形 1:1",
}


def _keyframe_prompt(
    shot: dict[str, Any],
    ref_names: list[str],
    *,
    full_prompt: str = "",
    name_by_id: dict[str, str] | None = None,
    aspect: str = "9:16",
) -> str:
    """为首帧生成写的提示词：一张静止剧照，按 8 段式写（skills/director/SKILL.md 第二节）。

    首帧是三层里最便宜也最要紧的一层：构图错了，视频再重跑一百次都是错的。
    """
    mapping = name_by_id or {}
    size = _shot_size_text(str(shot.get("shot_size") or ""))
    camera = _camera_text(shot)
    action = str(shot.get("action") or "").strip()
    text = (full_prompt or "").strip()
    lines = [f"电影级写实剧照，{_ASPECT_LABEL.get(str(aspect or '9:16'), '竖屏 9:16')} 构图。这是下面这一镜的第 0 秒画面，静止定格。"]
    subject_text = _subject_text(shot, mapping)
    lines.append((subject_text or "画面主体见下方画面描述，人数与站位严格按描述") + "。")
    props_text = _props_text(shot, mapping)
    if props_text:
        lines.append(props_text + "。")
    if text:
        lines.append(f"ACTION：定格在动作起点。画面描述如下：\n{text}")
    elif action:
        lines.append(f"ACTION：定格在动作起点——{action}。")
    lines.append(
        f"CAMERA：{size or '中景'}"
        + (f"，{camera}" if camera else "，机位固定")
        + "；构图按上面的画面描述，主体完整入画、不裁掉关键动作。"
    )
    light_text = _light_text(shot)
    lines.append(light_text + "。" if light_text else "LIGHT：光源方向与色温按场景参考素材，保持同一场戏的光线方向一致。")
    lines.append("TEXT：画面中不出现任何文字。")
    lines.append("STYLE：写实电影质感，胶片颗粒，色调统一。")
    if ref_names:
        lines.append(
            "身份与外观严格按参考素材（" + "、".join(f"@{name}" for name in ref_names) + "）："
            "脸型、发型、服装、泥污一致，不重描、不替换。"
        )
    lines.append("构图完整、主体清晰、有前中后景层次；不出现水印、边框、多格排版、拼图。")
    return "\n".join(lines)


def _materialize_prompt(prompt: str, refs: list[dict[str, Any]]) -> str:
    """把引用标记换成语义化的参考标记，并把引用清单写进提示词头。

    两种输入都要认：
    - `@名字`：外部模型（火宝/Seedance）的写法，按名字查序号；
    - `{{ref N}}`：本系统画布资产规范里的写法，按序号直接对应。

    真正的图片是通过 API 的 references 参数传的；这里只是让提示词本身自解释，
    这样换一个模型/换一个工具仍然知道第 N 张图是谁。
    """
    if not refs:
        return prompt
    index_by_name = {str(ref.get("name")): ref["ordinal"] for ref in refs if ref.get("name")}
    # `@资产·灰蓝士兵` 这类带前缀/带空白的写法也要认，否则引用会被原样留在正文里
    index_by_loose = {_clean_asset_name(str(ref.get("name"))): ref["ordinal"] for ref in refs if ref.get("name")}
    name_by_ordinal = {int(ref["ordinal"]): (ref.get("name") or ref.get("asset_id")) for ref in refs}
    lines = [f"参考素材：图片{ref['ordinal']} = {ref.get('name') or ref.get('asset_id')}" for ref in refs]

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        ordinal = index_by_name.get(name) or index_by_loose.get(_clean_asset_name(name))
        return f"图片{ordinal}{name}" if ordinal else match.group(0)

    def replace_ref(match: re.Match[str]) -> str:
        ordinal = int(match.group(1))
        label = name_by_ordinal.get(ordinal)
        return f"图片{ordinal}{label}" if label else match.group(0)

    # 左括号也要截断：服务端渲染成 `@林直人（画面左三分之一；…）`，
    # 否则物化时会把括号里的描述当成名字的一部分查不到，引用就留在正文里了。
    body = re.sub(r"(?<!图片)(?<!\d)@([^\s@，,。；;：:、（）()【】\[\]\"'“”]{1,64})", replace, prompt)
    body = re.sub(r"\{\{\s*ref\s*(\d+)\s*\}\}", replace_ref, body, flags=re.IGNORECASE)
    return "\n".join(lines) + "\n\n" + body


# 与 `_materialize_prompt` 里替换用的正则保持同一套边界，避免「能渲染」但「查不到」。
_MENTION_SCAN_RE = re.compile(r"(?<!图片)(?<!\d)@([^\s@，,。；;：:、（）()【】\[\]\"'“”]{1,64})")
_REF_SLOT_SCAN_RE = re.compile(r"\{\{\s*ref\s*(\d+)\s*\}\}", re.IGNORECASE)


def _dangling_references(prompt: str, refs: list[dict[str, Any]]) -> list[str]:
    """正文里引用了、但不在本片段引用入边里的名字/序号。

    判据是**入边集合**，不是 request["references"]：首帧模式入边存在但只发首帧，
    按发出参数判会把每一镜都误报成悬空引用。真正的「首帧模式正文宣称有参考图」
    问题在渲染侧修（首帧模式不再写那句），不靠这个断言兜。
    """
    known = {str(ref.get("name") or "").strip() for ref in refs if ref.get("name")}
    known |= {_clean_asset_name(str(ref.get("name") or "")) for ref in refs if ref.get("name")}
    dangling: list[str] = []
    for name in _MENTION_SCAN_RE.findall(prompt):
        if name not in known and _clean_asset_name(name) not in known:
            dangling.append(f"@{name}")
    for raw in _REF_SLOT_SCAN_RE.findall(prompt):
        if not 1 <= int(raw) <= len(refs):
            dangling.append(f"{{{{ref {raw}}}}}")
    return dangling


def _default_image_model() -> str:
    if _media is None:
        return "mock-image-v1"
    try:
        models = [item for item in _media.list_models() if item.get("kind") == "image"]
        active = str(getattr(_media, "active", "mock") or "mock")
        mine = [item for item in models if str(item.get("provider") or "mock") == active]
        models = mine or models
    except Exception:  # pragma: no cover
        models = []
    preferred = next((item for item in models if str(item.get("id", "")).lower().startswith("mock")), None)
    return str((preferred or (models[0] if models else {})).get("id") or "mock-image-v1")


def _default_video_model() -> str:
    if _media is None:
        return "mock-video-v1"
    try:
        models = [item for item in _media.list_models() if item.get("kind") == "video"]
        active = str(getattr(_media, "active", "mock") or "mock")
        mine = [item for item in models if str(item.get("provider") or "mock") == active]
        models = mine or models
    except Exception:  # pragma: no cover - provider 探测失败时退回 mock
        models = []
    mock = next((item for item in models if str(item.get("id", "")).lower().startswith("mock")), None)
    if mock is not None:
        return str(mock.get("id") or "mock-video-v1")
    # 没指定模型时不要随手挑列表第一个：云端列表第一个往往是 omni 系旗舰，
    # 既贵、又不接受普通的 first_frame 参考形状（实测会报
    # “omni_reference_task_type ... is not valid”）。默认给最便宜的稳定档。
    ids = [str(item.get("id") or "") for item in models]
    for name in ("Seedance 2.0 mini", "Seedance 2.0 fast", "Seedance 2.0"):
        if name in ids:
            return name
    return ids[0] if ids else "mock-video-v1"


async def _run_generation(job_id: str, canvas_name: str, node_id: str, request: dict[str, Any], model: str) -> None:
    job = _jobs[job_id]
    job.update(status="running", started_at=time.time())
    try:
        result = await asyncio.to_thread(_media.generate, "video", request, _workspace)
        async with _lock_for(canvas_name):
            graph = await _load(canvas_name)
            prompt_node = graph.nodes.get(node_id)
            if prompt_node is None:
                raise RuntimeError("生成过程中画布被修改，片段已不存在")
            gen = _generation_node(graph, prompt_node)
            if gen is None:
                raise RuntimeError("生成过程中生成节点被删除")
            # 走画布同款 record_generated_media：追加历史 + 「首次才自动采用」的规则只有一份，
            # outputs 跟随已采用版本；这里不再手写第二份 take 逻辑（两边语义曾经长歪）。
            take = graph.record_generated_media(
                gen.id,
                kind="video",
                path=result.path,
                mime=result.mime,
                provider=result.provider,
                mock=bool(result.mock),
                model=model,
                duration_s=request.get("duration"),
                take_id=f"take_{uuid.uuid4().hex[:12]}",
                shot_no=int(request.get("shot_no") or 1),
                mode=str(request.get("mode") or ""),
                # 生成凭据：记录「当时看到的分镜与引用」，读取时比对即可判过期
                extra={
                    "shots_hash": request.get("shots_hash") or None,
                    "refs_hash": request.get("refs_hash") or None,
                },
            )
            gen.data.pop("active_job_id", None)
            gen.status = "done"
            await _save(graph)
        job.update(status="succeeded", finished_at=time.time(), result=take, take=take)
    except Exception as exc:  # noqa: BLE001 - 生成失败要原样回给前端
        job.update(status="failed", finished_at=time.time(), error=f"{type(exc).__name__}: {exc}")
        try:
            async with _lock_for(canvas_name):
                graph = await _load(canvas_name)
                prompt_node = graph.nodes.get(node_id)
                gen = _generation_node(graph, prompt_node) if prompt_node is not None else None
                if gen is not None:
                    gen.data.pop("active_job_id", None)
                    await _save(graph)
        except Exception:  # pragma: no cover - 清理失败不影响主流程
            pass
    finally:
        _tasks.pop(job_id, None)


@router.post("/segments/{node_id}/generate")
async def generate_segment(node_id: str, canvas: str, body: GenerateRequest) -> dict[str, Any]:
    async with _lock_for(canvas):
        graph = await _load(canvas)
        prompt_node = graph.nodes.get(node_id)
        if prompt_node is None or prompt_node.type != "prompt":
            raise HTTPException(status_code=404, detail=f"片段不存在：{node_id}")

        view = _segment_view(graph, prompt_node, with_detail=True)
        blocking = [item for item in view["lint"]["findings"] if item["severity"] == "error"]
        if blocking and not body.force:
            raise HTTPException(
                status_code=422,
                detail={"message": "片段未通过硬校验，先修好再生成（确实要跳过请显式 force=true）", "findings": blocking},
            )

        refs = view["references"]
        project = _project_meta(graph)
        model = body.model or project.get("video_model") or _default_video_model()
        if model and str(model).lower().startswith("mock"):
            model = _default_video_model()
        size = body.size or _default_size(project.get("aspect_ratio"), project.get("quality"))

        gen = _generation_node(graph, prompt_node)
        keyframe = dict(gen.data.get("keyframe") or {}) if gen is not None else {}
        shot = _first_shot(view["shots"], body.shot_no)
        # 有首帧就走首帧模式：构图与人物位置由首帧决定，模型只负责「演」。
        # 没有首帧才退回参考图 + 整段提示词（老画布、纯文本镜头的兜底）。
        use_keyframe = bool(keyframe.get("path")) and body.use_keyframe is not False

        # 人改正文优先：显式传参 > 已保存的人改正文（rendered_by=human）> 服务端渲染。
        # 以前首帧分支完全忽略 data.prompt，用户在制作页改完再点生成 = 白改白花钱。
        human_prompt = (body.prompt or "").strip()
        if not human_prompt and str(prompt_node.data.get("rendered_by") or "") == "human":
            human_prompt = str(view.get("prompt") or "").strip()

        if use_keyframe:
            shot_names = {ref["asset_id"]: ref["name"] for ref in refs}
            # 首帧模式只发 first_frame，request 里没有 references；正文里就不该再写
            # 「身份与外观完全按参考素材（@A、@B）」——没有图支撑，模型只会自己编。
            # 身份由首帧图承担；这里只渲染字段，不再宣称参考素材。
            prompt_text = human_prompt or _render_segment_text(shot, shot_names)
            shot_duration = _shot_seconds(shot) or float(view["duration_s"] or 5)
            duration = float(body.duration or shot_duration)
            request = {
                "prompt": prompt_text,
                "model": model,
                "provider": str(getattr(_media, "active", "") or ""),
                "size": size,
                "duration": max(1, min(30, int(round(duration)))),
                # 首帧模式与参考图模式互斥：frames 模式下 TF-router 只收 first/last frame。
                "first_frame": str(keyframe["path"]),
                "references": [],
                "ratio": project.get("aspect_ratio"),
                "aspect_ratio": project.get("aspect_ratio"),
                "resolution": str(project.get("quality") or "480P").lower(),
            }
        else:
            prompt_text = _materialize_prompt(human_prompt or view["prompt"], refs)
            duration = float(body.duration or view["duration_s"] or 8)
            request = {
                "prompt": prompt_text,
                "model": model,
                "provider": str(getattr(_media, "active", "") or ""),
                "size": size,
                "duration": max(1, min(30, int(round(duration)))),
                "references": [ref["path"] for ref in refs if ref.get("path")],
                "ratio": project.get("aspect_ratio"),
                "aspect_ratio": project.get("aspect_ratio"),
                # 远端 Provider 的分辨率枚举是小写（480p / 720p / 1080p）
                "resolution": str(project.get("quality") or "480P").lower(),
            }

        # 30s 是视频生成请求的硬上限：超限直接拒绝（以前静默钳到 30s，尾段内容无声丢失）
        if duration > 30:
            raise HTTPException(
                status_code=422,
                detail=f"单次视频生成上限 30s，当前要生成 {duration:.0f}s；请把片段拆短（一镜一段 3-8 秒）",
            )
        request["shot_no"] = int(shot.get("no") or body.shot_no or 1)
        request["mode"] = "first_frame" if use_keyframe else "references"
        # stale 凭据：记下本次生成看到的分镜与引用，之后分镜/形象一变即可判过期
        request["shots_hash"] = shots_hash(view.get("shots") or [])
        request["refs_hash"] = refs_hash(_fingerprint_refs(graph, view.get("references") or []))

        # 引用闭包：正文引用的名字必须来自本片段的引用入边，否则宁可报错也不静默发出去。
        dangling = _dangling_references(prompt_text, refs)
        if dangling:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": (
                        "提示词引用了不在本片段引用入边里的素材："
                        + "、".join(dangling)
                        + "。请连上「资产 → 片段」的引用边，或删掉正文里的这句引用后再生成。"
                    ),
                    "dangling": dangling,
                    "mode": request["mode"],
                },
            )

        job_id = f"sjob_{uuid.uuid4().hex[:12]}"
        _jobs[job_id] = {
            "job_id": job_id,
            "canvas": canvas,
            "node_id": node_id,
            "status": "queued",
            "model": model,
            "size": size,
            "duration": request["duration"],
            "reference_count": len(request["references"]),
            "shot_no": int(shot.get("no") or body.shot_no or 1),
            "mode": "first_frame" if use_keyframe else "references",
            "prompt": prompt_text,
            "created_at": time.time(),
            "error": "",
        }
        if gen is not None:
            gen.data["active_job_id"] = job_id
            await _save(graph)

    _tasks[job_id] = asyncio.create_task(_run_generation(job_id, canvas, node_id, request, model))
    return {"ok": True, "job": _jobs[job_id]}


@router.post("/segments/{node_id}/keyframe")
async def generate_keyframe(node_id: str, canvas: str, body: KeyframeRequest) -> dict[str, Any]:
    """先出一张「这一镜的第一帧」，再用它驱动视频。

    参考图模式下模型只会让设定图轻微动起来；首帧模式下这一帧就是第 0 秒的画面，
    构图和人物位置由我们（关键帧）决定，模型只负责往后演。
    """
    async with _lock_for(canvas):
        graph = await _load(canvas)
        node = graph.nodes.get(node_id)
        if node is None or node.type != "prompt":
            raise HTTPException(status_code=404, detail=f"片段不存在：{node_id}")
        view = _segment_view(graph, node, with_detail=True)
        gen = _generation_node(graph, node)
        if gen is None:
            raise HTTPException(status_code=400, detail="这个片段还没有生成节点，先跑一次生成")
        shot = _first_shot(view["shots"], body.shot_no)
        ref_names = [str(ref.get("name") or "") for ref in view["references"] if ref.get("name")]
        full_prompt = _materialize_prompt(view["prompt"], view["references"])
        # _materialize_prompt 会在正文前加一段「参考素材：图片N = 名字」的清单，
        # 那是给视频提示词用的头部；嵌进首帧的 ACTION 段里会读起来像断句，先剥掉。
        if full_prompt.startswith("参考素材："):
            head, _, rest = full_prompt.partition("\n\n")
            full_prompt = rest.strip() or full_prompt
        shot_names = {str(ref.get("asset_id") or ""): str(ref.get("name") or "") for ref in view["references"]}
        project = _project_meta(graph)
        prompt = (body.prompt or "").strip() or _keyframe_prompt(
            shot, ref_names, full_prompt=full_prompt, name_by_id=shot_names,
            aspect=str(project.get("aspect_ratio") or "9:16"),
        )
        model = body.model or _default_image_model()
        size = body.size or _default_size(project.get("aspect_ratio"), project.get("quality"))
        request = {
            "prompt": prompt,
            "model": model,
            "provider": str(getattr(_media, "active", "") or ""),
            "size": size,
            # 首帧图本身还是要靠角色/场景设定图当参考，这样人物长相才能对上。
            "references": [ref["path"] for ref in view["references"] if ref.get("path")],
            "ratio": project.get("aspect_ratio"),
            "aspect_ratio": project.get("aspect_ratio"),
        }
        shot_no = int(shot.get("no") or body.shot_no or 1)

    result = await asyncio.to_thread(_media.generate, "image", request, _workspace)

    async with _lock_for(canvas):
        graph = await _load(canvas)
        node = graph.nodes.get(node_id)
        gen = _generation_node(graph, node) if node is not None else None
        if gen is None:
            raise HTTPException(status_code=400, detail="生成期间片段被删除")
        keyframe = {
            "path": result.path,
            "prompt": prompt,
            "shot_no": shot_no,
            "model": model,
            "mock": bool(result.mock),
            "created_at": time.time(),
            # stale 凭据：首帧记录「按哪份分镜/哪些参考图画的」
            "shots_hash": shots_hash(view.get("shots") or []),
            "refs_hash": refs_hash(_fingerprint_refs(graph, view.get("references") or [])),
        }
        gen.data["keyframe"] = keyframe
        if not gen.data.get("thumbnail"):
            gen.data["thumbnail"] = result.path
        await _save(graph)
    return {"ok": True, "keyframe": keyframe}


def _default_size(aspect_ratio: str | None, quality: str | None) -> str:
    ratio = str(aspect_ratio or "9:16")
    height = {"480P": 854, "720P": 1280, "1080P": 1920}.get(str(quality or "480P").upper(), 854)
    if ratio == "9:16":
        return f"{int(height * 9 / 16)}x{height}"
    if ratio == "1:1":
        return f"{height}x{height}"
    return f"{int(height * 16 / 9)}x{height}"


def _size_pair(size: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", size or "")
    if not match:
        return 480, 854
    width, height = int(match.group(1)), int(match.group(2))
    # ffmpeg 的 yuv420p 要求偶数边长
    return width - width % 2, height - height % 2


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{job_id}")
    return job


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, Any]:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{job_id}")
    task = _tasks.get(job_id)
    if task is not None and not task.done():
        task.cancel()
    job.update(status="cancelled", finished_at=time.time())
    return {"ok": True, "job": job}


# ---------------------------------------------------------------------------
# 资产形象
# ---------------------------------------------------------------------------


@router.post("/assets/{node_id}/looks")
async def upsert_look(node_id: str, canvas: str, body: LookPatch) -> dict[str, Any]:
    graph = await _load(canvas)
    target = graph.nodes.get(node_id)
    if target is None or target.type != "asset":
        raise HTTPException(status_code=404, detail=f"资产不存在：{node_id}")

    looks = _asset_looks(graph, target)
    # 隐式默认形象在第一次编辑时升级为显式列表
    if not isinstance(target.data.get("looks"), list):
        looks = [dict(look) for look in looks]

    if body.image_path and body.name:
        existing = next((look for look in looks if look.get("name") == body.name), None)
        if existing is None:
            existing = {
                "id": f"look_{uuid.uuid4().hex[:10]}",
                "name": body.name,
                "image_path": body.image_path,
                "prompt": body.prompt or "",
                "locked": bool(body.locked),
                "appearances": body.appearances or [],
            }
            looks.append(existing)
        else:
            existing["image_path"] = body.image_path
            if body.prompt is not None:
                existing["prompt"] = body.prompt
            if body.locked is not None:
                existing["locked"] = body.locked
            if body.appearances is not None:
                existing["appearances"] = body.appearances

    for look in looks:
        if look.get("name") == body.name:
            if body.prompt is not None:
                look["prompt"] = body.prompt
            if body.locked is not None:
                look["locked"] = body.locked
            if body.appearances is not None:
                look["appearances"] = body.appearances

    target.data["looks"] = looks
    if not target.data.get("active_look_id") and looks:
        target.data["active_look_id"] = looks[0]["id"]
    if body.generate:
        look = looks[-1] if looks else None
        if look is not None and not look.get("image_path"):
            result = await asyncio.to_thread(
                _media.generate,
                "image",
                {
                    "prompt": look.get("prompt") or target.data.get("name") or target.label,
                    "model": body.model or "mock-image-v1",
                    "size": body.size,
                },
                _workspace,
            )
            look["image_path"] = result.path
            look["mime"] = result.mime
            look["generated"] = True
    revision = await _save(graph)
    return {
        "ok": True,
        "revision": revision,
        "node_id": node_id,
        "looks": _asset_looks(graph, target),
    }


class NewLookRequest(BaseModel):
    name: str
    prompt: str = ""
    generate: bool = True
    size: str = ""
    model: str = ""


@router.post("/assets/{node_id}/looks/new")
async def create_look(node_id: str, canvas: str, body: NewLookRequest) -> dict[str, Any]:
    """给一个角色追加一套新形象（换装/换年龄段）并可直接出图。

    真实业务里一个角色常有多套形象（日常/校服/礼服），每套独立锁图与出场章节，
    单形象模型没法表达这个差异，所以这里显式建列表。
    """
    graph = await _load(canvas)
    target = graph.nodes.get(node_id)
    if target is None or target.type != "asset":
        raise HTTPException(status_code=404, detail=f"资产不存在：{node_id}")

    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="形象名不能为空")
    looks = [dict(look) for look in _asset_looks(graph, target)]
    if any(str(look.get("name") or "") == name for look in looks):
        raise HTTPException(status_code=400, detail=f"已经有同名形象：{name}")

    kind = str(target.data.get("kind") or "character")
    look: dict[str, Any] = {
        "id": f"look_{uuid.uuid4().hex[:10]}",
        "name": name,
        "image_path": "",
        "prompt": (body.prompt or "").strip() or _look_prompt(graph, target, kind),
        "locked": False,
        "appearances": [],
    }

    if body.generate:
        size = body.size or _DEFAULT_LOOK_SIZE.get(kind, "768x1024")
        result = await asyncio.to_thread(
            _media.generate,
            "image",
            {
                "prompt": look["prompt"],
                "model": body.model or "mock-image-v1",
                "size": size,
            },
            _workspace,
        )
        look["image_path"] = result.path
        look["mime"] = result.mime
        look["generated"] = True

    looks.append(look)
    target.data["looks"] = looks
    if not target.data.get("active_look_id"):
        target.data["active_look_id"] = look["id"]
    revision = await _save(graph)
    return {
        "ok": True,
        "revision": revision,
        "node_id": node_id,
        "look": look,
        "looks": _asset_looks(graph, target),
    }


# ---------------------------------------------------------------------------
# 资产形象生成
# ---------------------------------------------------------------------------

# 素材图 = 身份证，不是首帧（skills/asset/SKILL.md 第一节）。
# 素材层只回答「长什么样」，所以：平光、无强阴影、单角度、纯底、信息优先。
# 两条实测教训写在这里，别再犯：
# 1. 正/侧/背并排的三视图，图片模型画得对，视频模型却会读成「三个不同的人」——
#    于是成片里出现重复角色或人物分裂。一个角色要拆成多张单角度单图。
# 2. 素材图里出现戏剧光/环境，首帧和视频阶段会被这套光锁死，改不动。
_LOOK_RULES: dict[str, str] = {
    "character": (
        "单角度角色设定图：正面全身，平视机位，中性表情，双手自然下垂，全身入画、脚下留少量空间；"
        "棚拍柔光、无强阴影、纯中性灰背景、地面无投影；"
        "不出现文字、水印、边框、多格排版、拼图、第二个人、动态姿势、手持道具"
    ),
    "scene": (
        "场景设定图：无人物广角空镜，画面中没有任何人物与人物剪影；平视，交代空间结构、"
        "出入口位置、光源方向与前后景层次；不出现文字、不出现水印"
    ),
    "prop": (
        "道具设定图：白底单品，单件物品居中、完整、无遮挡；正视或 45 度视角；"
        "不出现人物、不出现文字与 logo、不加环境背景"
    ),
    "style": (
        "风格样张：只交代配色、质感与光影倾向，不出现具体人物、场景内容与文字"
    ),
}

_DEFAULT_LOOK_SIZE = {"character": "768x1024", "scene": "1024x576", "prop": "768x768", "style": "1024x576"}


class AssetGenerateRequest(BaseModel):
    look_id: str = ""
    prompt: str = ""
    model: str = ""
    size: str = ""
    # 以哪个形象为参考出图（默认第一项「正面全身」）：全身定脸→特写参考全身，
    # 同一角色两张图才不会长成两张脸。参考对象自身或无图时自动退化为无参考。
    based_on: str = ""


def _look_prompt(graph: Canvas, node: Any, kind: str) -> str:
    """把资产规格压成一条生图提示词：外观锚点 + 一致性标签 + 图种规矩 + 全剧风格。

    形状固定成「信息 → 规矩 → 禁止」三段，长度压在 300 字以内。素材层的目标是
    「可辨认、可复现」，不是好看——写太多形容词会让模型开始自由发挥。
    """
    parts: list[str] = []
    spec = str(node.data.get("spec") or "").strip()
    if spec:
        # spec 是给人看的长文，只取「外观锚点」这一行最有价值
        for line in spec.splitlines():
            text = line.strip()
            if text.startswith(("外观锚点", "构成", "场景锚点")):
                parts.append(text.split("：", 1)[-1].split(":", 1)[-1].strip())
        if not parts:
            parts.append(spec.splitlines()[0][:120])
    tags = str(node.data.get("consistency_tags") or "").strip()
    if tags:
        parts.append(tags)
    head = "，".join(item.strip("， ") for item in parts if item and item.strip())
    if kind == "character" and head and not head.startswith(("男", "女", "少年", "少女", "中年", "老")):
        head = f"角色 {head}"
    rules = _LOOK_RULES.get(kind, _LOOK_RULES["prop"])
    style = str(_project_meta(graph).get("style_preset") or "").strip()
    lines = [item for item in (head, style, rules) if item]
    return "\n".join(lines)


def _look_request(
    graph: Canvas,
    target: Any,
    looks: list[dict[str, Any]],
    index: int,
    body: "AssetGenerateRequest",
) -> tuple[dict[str, Any], str, str]:
    """素材图生成请求组装：锁定检查 + 参考链 + 提示词/尺寸。纯函数，可测。

    返回 (request, based_on_id, ref_path)；ref_path 非空表示这次以另一形象的图为参考。
    """
    look = looks[index]
    # 锁定=真锁：locked 形象禁止重新生成，防止手滑重画打乱已在镜头里用着的形象
    if look.get("locked"):
        raise HTTPException(status_code=409, detail="该形象已锁定；先解锁再重新生成")
    kind = str(target.data.get("kind") or "prop")
    # 提示词优先级：用户手改 > 画布里 Agent 写的 > 系统按规范合成。
    # 「三视图」是旧规范留下的文本：视频模型会把并排的三个视图读成三个不同的人，
    # 这套文本不能再进生成链路，遇到就用系统模板重算（画布里的原文不动，只是不采用）。
    written = (body.prompt or look.get("prompt") or "").strip()
    if "三视图" in written:
        written = ""
    project = _project_meta(graph)
    # 参考链：默认以第一项（正面全身锚点）为参考出图；全身/特写同源，脸才不会漂
    based_on_id = str(body.based_on or "") or (str(looks[0].get("id") or "") if looks else "")
    ref_path = ""
    if based_on_id and based_on_id != str(look.get("id") or ""):
        for cand in looks:
            if str(cand.get("id") or "") == based_on_id and cand.get("image_path"):
                ref_path = str(cand["image_path"])
                break
    request = {
        "prompt": written or _look_prompt(graph, target, kind),
        "model": body.model or _default_image_model(),
        "provider": str(getattr(_media, "active", "") or ""),
        "size": body.size or _DEFAULT_LOOK_SIZE.get(kind, "1024x576"),
        "ratio": project.get("aspect_ratio") or "9:16",
    }
    if ref_path:
        # 参考图进生图请求（TF-router 转 data URL）；mock 忽略
        request["references"] = [ref_path]
    return request, based_on_id, ref_path


@router.post("/assets/{node_id}/generate")
async def generate_look(node_id: str, canvas: str, body: AssetGenerateRequest) -> dict[str, Any]:
    """给资产的一个形象生成参考图。Mock 下也会产出真实 PNG，链路与真模型一致。"""
    graph = await _load(canvas)
    target = graph.nodes.get(node_id)
    if target is None or target.type != "asset":
        raise HTTPException(status_code=404, detail=f"资产不存在：{node_id}")

    looks = [dict(look) for look in _asset_looks(graph, target)]
    index = next((i for i, look in enumerate(looks) if look.get("id") == body.look_id), None)
    if index is None:
        index = 0
    look = looks[index]
    request, based_on_id, ref_path = _look_request(graph, target, looks, index, body)
    model = request["model"]
    size = request["size"]

    try:
        result = await asyncio.to_thread(_media.generate, "image", request, _workspace)
    except Exception as exc:  # noqa: BLE001 - 直接回给前端
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc

    look.update(
        {
            "image_path": result.path,
            "mime": result.mime,
            "prompt": prompt,
            "generated": True,
            "provider": result.provider,
            "mock": bool(result.mock),
            "model": model,
            "size": size,
            "created_at": time.time(),
            # 记录参考来源：审计「这张特写是按哪张全身画的」
            "based_on_id": based_on_id if ref_path else "",
            "based_on_image": ref_path,
        }
    )
    looks[index] = look
    target.data["looks"] = looks
    if not target.data.get("active_look_id"):
        target.data["active_look_id"] = look["id"]
    revision = await _save(graph)
    return {"ok": True, "revision": revision, "node_id": node_id, "look": look, "looks": looks}


# ---------------------------------------------------------------------------
# 剧本（分集正文 + 台词表）
# ---------------------------------------------------------------------------


@router.get("/script")
async def get_script(canvas: str, episode: int | None = None) -> dict[str, Any]:
    """剧本页数据：分集正文、台词逐条、资产识别摘要。"""
    graph = await _load(canvas)
    episodes = _episodes(graph)

    # 先定当前集：正文、台词、资产全部按集收窄，不能把整本剧本混在一起。
    current = next((item for item in episodes if int(item.get("no") or 1) == int(episode or 0)), None)
    if current is None:
        current = episodes[0] if episodes else None
    episode_no = int((current or {}).get("no") or 1)

    script = ""
    script_node_id = ""
    wanted = str((current or {}).get("script_node_id") or "")
    if wanted and wanted in graph.nodes:
        script, script_node_id = _node_text(graph, graph.nodes[wanted]), wanted
    if not script:
        # 老画布没有 script_node_id：退回「最长的那段正文」，但只在第 1 集时这么做。
        if episode_no == 1:
            for node in graph.nodes.values():
                if node.type != "text" or node.data.get("segment_no"):
                    continue
                body = _node_text(graph, node)
                if len(body) > len(script):
                    script, script_node_id = body, node.id

    shots: list[dict[str, Any]] = []
    for node in graph.nodes.values():
        if node.type != "storyboard" or _episode_no(node) != episode_no:
            continue
        raw = node.data.get("shots")
        if isinstance(raw, list):
            shots.extend(raw)

    dialogue = []
    for shot in sorted(shots, key=lambda s: (s.get("segment_no") or 0, s.get("no") or 0)):
        for line in parse_dialogue(str(shot.get("sound") or "")):
            dialogue.append({**line, "segment_no": shot.get("segment_no"), "shot_no": shot.get("no")})

    # 还没拆分镜的集：直接从剧本正文里读台词，让「台词清单」在第一步就有东西看。
    if not dialogue and script:
        for index, line in enumerate(script.splitlines(), 1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            for item in parse_dialogue(text):
                dialogue.append({**item, "segment_no": None, "shot_no": index})

    assets = _assets_view(graph)
    counts = _counts(graph)

    # 每集带片段完成度：剧本页要能判断「这一集是不是还没拆过分镜」。
    by_episode: dict[int, list[dict[str, Any]]] = {}
    for segment in _segments_all(graph):
        by_episode.setdefault(int(segment.get("episode_no") or 1), []).append(segment)
    for item in episodes:
        bucket = by_episode.get(int(item.get("no") or 1), [])
        item["segment_total"] = len(bucket)
        item["segment_done"] = sum(1 for seg in bucket if seg.get("status") == "done")
    if current is not None:
        current = next(
            (item for item in episodes if int(item.get("no") or 1) == episode_no), current
        )

    return {
        "canvas": canvas,
        "script": script,
        "script_node_id": script_node_id,
        "chars": len(script),
        "episodes": episodes,
        "episode": current,
        "dialogue": dialogue,
        "dialogue_count": len(dialogue),
        "assets": {key: len(value) for key, value in assets.items()},
        "asset_items": [
            {
                "node_id": item["node_id"],
                "kind": item["kind"],
                "name": item["name"],
                "role": item["role"],
                "ready": item["look_ready"],
                "total": item["look_total"],
            }
            for bucket in ("character", "scene", "prop")
            for item in assets[bucket]
        ],
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# 老画布升级 + 剧本导入
# ---------------------------------------------------------------------------

# 中文标题（第N集/章/回/节/话）或英文标题（Episode N / EP N）都认，
# 否则「Episode 2」整本落进第 1 集还无告警
_SPLIT_EPISODE = re.compile(
    r"^\s*(?:#{1,4}\s*)?(?:第\s*[0-9一二三四五六七八九十百]+\s*[集章回节话]|(?:Episode|EP)\s*\.?\s*\d+)\b",
    re.IGNORECASE | re.MULTILINE,
)


def _split_episodes(text: str) -> list[dict[str, Any]]:
    """按标题行切集；标题取整行（第2集 门缝里的灯 / Episode 2 xxx），正文取标题行之后。"""
    raw = text or ""
    if not any(_SPLIT_EPISODE.match(line) for line in raw.splitlines()):
        # 长剧本却一个标题都没切到：多半是格式不认识，提示一句而不是静默当单集
        warning = (
            "没找到分集标题（支持「第N集/章/回/节/话」「Episode N / EP N」），已按 1 集导入"
            if len(raw) > 3000
            else ""
        )
        return [{"no": 1, "title": "第1集", "chars": len(raw), "body": raw, "warning": warning}]
    lines = raw.splitlines(keepends=True)
    heads: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if _SPLIT_EPISODE.match(line):
            heads.append((index, line.strip().lstrip("#").strip()))
    if not heads:
        return [{"no": 1, "title": "第1集", "chars": len(raw), "body": raw}]
    episodes: list[dict[str, Any]] = []
    for position, (line_index, head) in enumerate(heads):
        end_index = heads[position + 1][0] if position + 1 < len(heads) else len(lines)
        body = "".join(lines[line_index + 1 : end_index]).strip()
        episodes.append({"no": position + 1, "title": head or f"第{position + 1}集", "chars": len(body), "body": body})
    if len(episodes) == 1 and len(raw) > 3000:
        # 长剧本只切出 1 集：可能标题格式不认识，宁可多说一句
        episodes[0]["warning"] = "这篇剧本很长但只切出 1 集；请确认分集标题格式（第N集/Episode N）"
    return episodes


@router.post("/import-script")
async def import_script(canvas: str, body: ImportScriptRequest) -> dict[str, Any]:
    """粘贴整本剧本 → 按「第N集」切分 → 每集建一个 text 节点。"""
    graph = await _load(canvas)
    if not (body.text or "").strip():
        raise HTTPException(status_code=400, detail="剧本内容为空")

    episodes = _split_episodes(body.text) if body.split else [
        {"no": 1, "title": "第1集", "chars": len(body.text), "body": body.text}
    ]

    meta = dict(graph.meta or {})
    existing = [int(item.get("no") or 0) for item in meta.get("episodes") or []]
    next_no = max(existing or [0]) + 1
    script_nodes: list[dict[str, Any]] = []

    for index, episode in enumerate(episodes):
        no = next_no + index if existing else episode["no"]
        title = episode["title"]
        if existing and no != episode["no"]:
            # 续编到已有项目时集号会顺延，标题里的「第N集」也要跟着改，否则界面全是错的编号
            title = re.sub(r"第\s*[0-9一二三四五六七八九十百]+\s*[集章回]", f"第{no}集", title, count=1)
        node_id = f"n_{uuid.uuid4().hex[:10]}"
        relative = f"assets/{node_id}/content.md"
        path = graph.root() / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(episode["body"], encoding="utf-8")
        graph.nodes[node_id] = _new_node(
            node_id=node_id,
            node_type="text",
            label=title,
            data={
                "type": "script",
                "episode_no": no,
                "chars": episode["chars"],
                "content_path": relative,
                "note": "制片台导入",
            },
            status="ready",
        )
        script_nodes.append({"node_id": node_id, "no": no, "title": title, "chars": episode["chars"]})

    meta["episodes"] = list(meta.get("episodes") or []) + [
        {"no": item["no"], "title": item["title"], "status": "in_progress", "script_node_id": item["node_id"]}
        for item in script_nodes
    ]
    graph.meta = meta
    revision = await _save(graph)
    return {
        "ok": True,
        "revision": revision,
        "episodes": script_nodes,
        "warnings": [str(item.get("warning")) for item in episodes if item.get("warning")],
    }


def _new_node(
    *,
    node_id: str,
    node_type: str,
    label: str,
    data: dict[str, Any],
    status: str = "draft",
    position: dict[str, float] | None = None,
) -> Any:
    from core.canvas import Node

    node = Node(id=node_id, type=node_type, label=label, data=data, status=status)
    if position:
        node.position = position
    return node


@router.post("/upgrade")
async def upgrade_canvas(canvas: str) -> dict[str, Any]:
    """把 Agent 建的画布补上制片台需要的派生字段（只加不改，可反复执行）。

    - `meta.project` 填默认项目属性（9:16 / 480P / Mock 模型）
    - `meta.episodes` 按现有 storyboard 归出第 1 集
    - 每个 prompt 节点补 `episode_no` / `duration_s`（从提示词时间轴推）
    - 每个 asset 节点补 `role` 与 `looks`（无图时给出待生成的空形象）
    """
    graph = await _load(canvas)
    meta = dict(graph.meta or {})
    touched: list[str] = []

    if not isinstance(meta.get("project"), dict):
        meta["project"] = dict(DEFAULT_PROJECT)
        touched.append("meta.project")
    if not isinstance(meta.get("episodes"), list) or not meta["episodes"]:
        meta["episodes"] = [
            {"no": 1, "title": meta.get("story_title") or graph.name, "status": "in_progress"}
        ]
        touched.append("meta.episodes")
    graph.meta = meta

    segments = _segments_all(graph)
    by_episode: dict[int, list[dict[str, Any]]] = {}
    for segment in segments:
        by_episode.setdefault(segment["episode_no"], []).append(segment)
    for episode in meta["episodes"]:
        episode["segment_total"] = len(by_episode.get(int(episode.get("no") or 1), []))
        episode["segment_done"] = sum(
            1 for seg in by_episode.get(int(episode.get("no") or 1), []) if seg["status"] == "done"
        )

    for node in graph.nodes.values():
        if node.type == "prompt":
            if "episode_no" not in node.data:
                node.data["episode_no"] = 1
            if "duration_s" not in node.data:
                parsed = _parse_duration(node.data.get("duration"))
                prompt = _node_text(graph, node)
                shots: list[dict[str, Any]] = []
                for item in graph.text_inputs(node.id):
                    source = graph.nodes.get(item["node_id"])
                    if source is not None and isinstance(source.data.get("shots"), list):
                        shots = source.data["shots"]
                duration, _ = _infer_duration(graph, node, prompt, shots)
                if parsed or duration:
                    node.data["duration_s"] = float(parsed or duration)
            touched.append(node.id)
        elif node.type == "asset":
            if not node.data.get("role"):
                node.data["role"] = "protagonist" if node.data.get("kind") == "character" else "supporting"
            if not isinstance(node.data.get("looks"), list):
                node.data["looks"] = _asset_looks(graph, node)
            touched.append(node.id)

    revision = await _save(graph)
    return {"ok": True, "revision": revision, "touched": touched, "project": _project_meta(graph)}


# ---------------------------------------------------------------------------
# 导出（复用既有成片管线）
# ---------------------------------------------------------------------------


def _renders(graph: Canvas) -> list[dict[str, Any]]:
    """成片清单：先取画布元数据里登记的，再扫 assets/rendered/*/final.mp4 兜底。"""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    recorded = ((graph.meta or {}).get("production") or {}).get("renders") or []
    if isinstance(recorded, list):
        for entry in recorded:
            path = str(entry.get("path") if isinstance(entry, dict) else entry or "").strip()
            if path and path not in seen:
                seen.add(path)
                items.append({"path": path, "recorded": True})
    root = graph.root() / "assets" / "rendered"
    # 合成产物的目录名是 `media_<job>-<timeline_node_id>`：只认时间轴节点属于本画布的，
    # 否则每个项目都会看到别的项目的成片（成片是全局目录，按 mtime 扫全盘最容易串号）。
    own_nodes = {node.id for node in graph.nodes.values() if node.type == "timeline"}
    if root.is_dir():
        videos = sorted(root.glob("*/final.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        for file in videos:
            owner = file.parent.name.rsplit("-", 1)[-1]
            if owner not in own_nodes:
                continue
            path = file.relative_to(graph.root()).as_posix()
            if path in seen:
                continue
            seen.add(path)
            stat = file.stat()
            items.append(
                {
                    "path": path,
                    "size": stat.st_size,
                    "created_at": stat.st_mtime,
                    "name": file.parent.name,
                }
            )
    return items


@router.get("/preview")
async def get_preview(canvas: str, episode: int | None = None) -> dict[str, Any]:
    graph = await _load(canvas)
    segments = _segments_all(graph)
    if episode is not None:
        segments = [seg for seg in segments if seg["episode_no"] == episode]
    return {
        "canvas": graph.name,
        "episode": episode,
        "total": len(segments),
        "done": sum(1 for seg in segments if seg["status"] == "done"),
        "clips": [
            {
                "node_id": seg["node_id"],
                "segment_no": seg["segment_no"],
                "duration_s": seg["duration_s"],
                "video_path": seg["video_path"],
                "ready": seg["has_video"],
                "title": seg["title"],
                "tags": seg["tags"],
            }
            for seg in segments
        ],
        "rendered": _renders(graph),
    }


# ---------------------------------------------------------------------------
# 项目层：系统入口（首页 → 项目 → 制片台），画布降级为「结构视图」
# ---------------------------------------------------------------------------

_SLUG_STRIP = re.compile(r"[^\w\u4e00-\u9fff-]+")
_RATIO_CHOICES = ("9:16", "16:9", "1:1", "4:3", "3:4")
_QUALITY_CHOICES = ("480P", "720P", "1080P")


class NewProjectRequest(BaseModel):
    title: str
    alt_title: str = ""
    region: str = "国内"
    genre: str = ""
    style_preset: str = ""
    aspect_ratio: str = "9:16"
    quality: str = "480P"
    video_model: str = ""
    script: str = ""
    split: bool = True
    skills: dict[str, str] = Field(default_factory=dict)  # {script_analysis, director, asset} → "name@version"
    auto_asset_table: bool = False  # 首次导入剧本后自动跑「剧本理解+资产解析」


def _slug_name(title: str) -> str:
    """画布名只作内部 ID：中文标题 + 随机后缀，人不看它，只看 story_title。"""
    base = _SLUG_STRIP.sub("-", str(title or "").strip()).strip("-")[:40] or "project"
    return f"story-{base}-{uuid.uuid4().hex[:8]}"


def _normalise_ratio(value: str) -> str:
    text = str(value or "").strip().replace("：", ":")
    return text if text in _RATIO_CHOICES else "9:16"


def _normalise_quality(value: str) -> str:
    text = str(value or "").strip().upper()
    return text if text in _QUALITY_CHOICES else "480P"


def _cover_image(canvas: Canvas) -> str:
    """项目封面：第一个有图的资产形象。首页卡片靠它区分项目。"""
    for node in canvas.nodes.values():
        if node.type != "asset":
            continue
        path = _active_look_image(canvas, node)
        if path:
            return path
    return ""


def _project_summary(canvas: Canvas) -> dict[str, Any]:
    counts = _counts(canvas)
    segments = counts["segments"]
    assets = counts["assets"]
    episodes = _episodes(canvas)
    project = _project_meta(canvas)
    updated = 0.0
    try:
        if canvas.path is not None and canvas.path.exists():
            updated = float(canvas.path.stat().st_mtime)
    except OSError:  # pragma: no cover - 文件系统异常不应影响列表
        updated = 0.0
    return {
        "canvas": canvas.name,
        "title": str((canvas.meta or {}).get("story_title") or canvas.name),
        "alt_title": str((canvas.meta or {}).get("alt_title") or ""),
        "summary": str((canvas.meta or {}).get("story_summary") or ""),
        "episodes": len(episodes),
        "episode_titles": [str(item.get("title") or f"第{item.get('no')}集") for item in episodes],
        "segments_total": segments["total"],
        "segments_done": segments["done"],
        "assets_ready": sum(assets[k]["ready"] for k in ("character", "scene", "prop")),
        "assets_total": sum(assets[k]["total"] for k in ("character", "scene", "prop")),
        "cover": _cover_image(canvas),
        "aspect_ratio": project.get("aspect_ratio"),
        "quality": project.get("quality"),
        "video_model": project.get("video_model"),
        "revision": canvas.revision,
        "updated_at": updated,
    }


@router.get("/projects")
async def list_projects() -> dict[str, Any]:
    """首页项目列表。一次把所有画布摘要算好，前端不用 N+1 请求。"""
    _require_configured()
    names = list(await _registry.canvas_repository.list())
    items: list[dict[str, Any]] = []
    for name in names:
        try:
            canvas = await _registry.canvas_repository.load(name)
        except Exception:  # pragma: no cover - 单个坏画布不该拖垮首页
            continue
        items.append(_project_summary(canvas))
    items.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
    return {"projects": items, "total": len(items)}


@router.post("/projects")
async def create_project(body: NewProjectRequest) -> dict[str, Any]:
    """新建项目：先落项目设定，再把剧本按集切分进来。"""
    _require_configured()
    title = str(body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="项目名称不能为空")

    project = dict(DEFAULT_PROJECT)
    project.update(
        {
            "region": str(body.region or project["region"]).strip(),
            "genre": str(body.genre or "").strip(),
            "style_preset": str(body.style_preset or "").strip(),
            "aspect_ratio": _normalise_ratio(body.aspect_ratio),
            "quality": _normalise_quality(body.quality),
            "auto_asset_table": bool(body.auto_asset_table),
        }
    )
    if body.video_model.strip():
        project["video_model"] = body.video_model.strip()
    if body.skills:
        project["skills"] = _validated_skills(body.skills)

    canvas = Canvas(_slug_name(title))
    canvas.meta = {
        "story_title": title,
        "alt_title": str(body.alt_title or "").strip(),
        "story_summary": "",
        "project": project,
        # 空列表：等 import-script 从第 1 集开始编号；没剧本时 _episodes() 回退成单集。
        "episodes": [],
        "created_at": time.time(),
    }
    await _save(canvas)

    imported: list[dict[str, Any]] = []
    if str(body.script or "").strip():
        imported = list(
            (
                await import_script(
                    canvas.name, ImportScriptRequest(text=body.script, split=bool(body.split))
                )
            ).get("episodes")
            or []
        )
    graph = await _load(canvas.name)
    return {"ok": True, "canvas": canvas.name, "project": _project_summary(graph), "episodes": imported}


@router.delete("/projects/{canvas}")
async def delete_project(canvas: str) -> dict[str, Any]:
    graph = await _load(canvas)
    try:
        await _registry.canvas_repository.delete(graph.name)
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "canvas": graph.name}


# ---------------------------------------------------------------------------
# 让 Agent 开始制作：剧本 → 分镜 / 资产 / 提示词 / 生成槽
# ---------------------------------------------------------------------------

_PRODUCE_INSTRUCTION = """把第{no}集做成可以进入「制作」的状态。这是一次**只做策划、不生成任何媒体**的任务。

**目标画布已经确定（就是当前绑定的这一张），不要调用任何新建画布 / 识别剧本 / 切换画布的工具**，
也不要改画布标题；直接在当前画布上写节点和连线。

剧本正文在 `assets/{node_id}/content.md`（也用 read_node 读一次，不要凭标题猜）。
流程与门禁、首帧与视频提示词工艺见技能 `director`，资产抽取与素材图工艺见技能 `asset`
（三层工艺分离：素材图=身份证 / 首帧=画面 / 视频段=动作）。**先读它们再动手。**

请依次完成：
1. 按「**一条分镜 = 一个镜头 = 一次生成**」把这一集拆成片段：每段 3-8 秒（常规 4-6 秒），只有一个子镜头，不跨场景。
   不要把几个镜头装进同一段——视频模型一次只能演一个连续镜头，它不会切镜；
   **也不要在一段里写时间轴**（「0-3 秒…3-7 秒…」），那等于让它自己猜怎么填满时长。
2. 每个片段建一个 storyboard 节点，`data.shots[]` 只写**一项**（就是这一镜），**只交结构化字段，不要写长正文**：
   - `no` 镜号；`duration_s` 本镜秒数（3-8）
   - `shot_size`：极特写/大特写/特写/近景/中景/全景/大全景/远景/大远景/空镜 之一
   - `camera`：{{"height": "与胸口齐平", "angle": "左前侧 30°", "movement": "固定"}}，三个字段都写
   - `subjects`：本镜出镜主体数组，每人
     {{"id": "<资产 node id>", "screen_pos": "画面左三分之一", "gaze": "看向对方喉咙", "start_state": "…", "end_state": "…"}}；
     **同框 ≥2 个主体时 gaze 与 screen_pos 必填**
   - `props`：{{"id": "<资产 node id>", "state": "螺纹段贴住皮肤"}}
   - `action`：一件连续动作，写明起点与可见末态（「下颌绷紧、手背青筋凸起」算末态，「他很愤怒」不算），资产用 `@资产名` 引用
   - `focus` 焦点；`light`：{{"direction": "右后方", "color": "暖橙 2700K", "contrast": "高"}}
   - `sound` 环境音/拟音；`dialogue`：{{"text": "原文台词", "speaker": "<说话角色的资产 node id>", "duration_s": 0}}，没有台词就留空串。
     **有台词必须写 speaker**（谁在说），旁白/画外音留空——配音按 speaker 绑定角色音色。
   - `forbidden`：本镜禁止出现的具体东西（不能为空）
   另写 `data.episode_no` = {no}，`data.segment_no` 从 1 连续编号。
3. 从剧本里提取角色 / 场景 / 道具资产（每类都要），各建一个 asset 节点，**只交字段**：
   - `data.kind`：character / scene / prop / style；`data.name` 资产名（**不要带「资产·」前缀**，正文里用 `@这个名字` 引用）
   - `data.anchors`：2-4 条能被画出来的外观锚点（角色：脸型/发型/服装材质/识别特征；场景：空间构成与光源；道具：材质与磨损）
   - `data.consistency_tags`：3-6 个顿号分隔、**能被画出来**的短语（`左虎口旧疤`✓、`有故事感`✗）
   - `data.forbidden`：这类资产禁止出现的具体项
   - `data.spec`：素材图要画成什么样——**单角度单图**，禁止「三视图」或拼图
   - `data.role`：角色填 protagonist/supporting/extra
   - **每个角色按单角度拆图**：`data.looks` 放两项独立单图，第一项「正面全身」（锚点，平光、纯底、中性表情），第二项「正脸特写」。
     系统生成第二项时会自动以第一项全身图为参考（全身定脸→特写参考全身），无需在提示词里重复描述长相。
4. 为每个片段建一个 prompt 节点：只写 `data.episode_no`、`data.segment_no`、`data.duration_s`（= 这一镜的 duration_s）。
   **正文不要手写**——服务端会按第 2 步的字段渲染 `data.prompt` 并置 `rendered_by="server"`；
   人改过的正文标 `rendered_by="human"`，服务端不再覆盖。
5. 每个 prompt 节点连一个 video_gen 节点（prompt → video_gen，两端句柄都是 `prompt`），并按资产依赖连好边。
   **一条边只连本镜真正出镜的资产**：单人镜头不要连另一个角色的资产；同框两人的镜头最多连两个角色。
   **若该集已有片段（同 episode_no + segment_no 的 storyboard/prompt/video_gen），原位更新其字段，不要新建平行节点**；系统会在收尾自动归并重复并标记下线。
6. **必须把每个分镜节点的输出接到对应 prompt 节点的上下文**：storyboard → prompt，
   `source_handle` 写 `shots`、`target_handle` 写 `ctx`（同一片段的两个节点，segment_no 要对上）。
   少了这条边，制作页读不到分镜表和台词。

不要调用任何媒体生成工具，也不要创建 timeline 节点。做完后用一句话说明：拆了几个镜头、提了哪些资产。"""


class ProduceRequest(BaseModel):
    canvas: str
    episode: int = 1
    instruction: str = ""


def _produce_session_id(canvas: str) -> str:
    """一个项目一个会话：Agent 能记住上一集怎么拆的，人也能在画布里看到历史。"""
    slug = re.sub(r"[^0-9a-zA-Z_\-]+", "-", canvas).strip("-")[:48] or "canvas"
    return f"studio-{slug}"


def _produce_prompt(graph: Canvas, episode: int, extra: str) -> tuple[str, str]:
    episodes = _episodes(graph)
    current = next((item for item in episodes if int(item.get("no") or 1) == int(episode)), None)
    if current is None:
        raise HTTPException(status_code=404, detail=f"没有第 {episode} 集")
    node_id = str(current.get("script_node_id") or "")
    if not node_id or node_id not in graph.nodes:
        raise HTTPException(status_code=400, detail="这一集还没有剧本正文，先在剧本页导入")
    prompt = _PRODUCE_INSTRUCTION.format(no=int(episode), node_id=node_id)
    if (extra or "").strip():
        prompt += f"\n\n补充要求（优先级最高）：{extra.strip()}"
    return prompt, node_id


async def _run_produce(job_id: str, canvas_name: str, prompt: str, session_id: str, episode: int = 1) -> None:
    job = _jobs[job_id]
    job.update(status="running", started_at=time.time())
    live = None
    try:
        before = _produce_counts(await _load(canvas_name))
        live = _registry.get_or_create(session_id)
        _registry.bind_canvas(live, canvas_name)
        live.agent.tools = live.tools_for(True, allow_media_generation=False)
        base = _prompt_builder(_registry.workspace, live.agent.tools, _registry.skills) if _prompt_builder else ""
        live.agent.session.system_prompt = (
            base
            + "\n<media-approval>\n用户选择了自动准备：信息足够时直接完成全部生产包和生成节点。"
            + "\n这次任务明确要求不生成任何媒体，也不要声称已经生成了媒体。\n</media-approval>"
        )
        start_event_id = await _registry.event_store.latest_id(live.id) or "0-0"
        run_id = await _registry.begin_persisted_run(live, "agent", start_event_id)
        live.runs += 1
        request = type("StudioProduceRequest", (), {"prompt": prompt, "use_sub_agent": True, "media_policy": "auto_review"})()
        # 不订阅 bus：事件本来就由 EventStore 持久化，前端通过 /api/events/stream 补看。
        await _agent_runner(live, request, run_id)

        graph = await _load(canvas_name)
        changed = _repair_links(graph)
        # 幂等收尾：同集重跑 produce 后按（集, 段号）归并重复片段，多出的标 orphaned
        reconcile = _reconcile_produce(graph, episode)
        if reconcile["orphaned"]:
            changed = True
        if changed:
            await _save(graph)
        after = _produce_counts(graph)
        job.update(
            status="succeeded",
            finished_at=time.time(),
            agent_status=getattr(live.agent, "last_status", ""),
            before=before,
            after=after,
            added={
                "segments": max(0, after["segments"] - before["segments"]),
                "assets": max(0, after["assets"] - before["assets"]),
            },
        )
    except asyncio.CancelledError:
        job.update(status="cancelled", finished_at=time.time())
        raise
    except Exception as exc:  # noqa: BLE001 - 任务失败要能被界面读到
        job.update(status="failed", finished_at=time.time(), error=f"{type(exc).__name__}: {exc}")


def _sync_rendered_prompts(graph: Canvas) -> int:
    """把每个片段的正文按字段重渲染并落进 data.prompt（人改过的不动）。"""
    written = 0
    for node in list(graph.nodes.values()):
        if node.type != "prompt" or str(node.data.get("rendered_by") or "") == "human":
            continue
        shots: list[dict[str, Any]] = []
        for item in graph.text_inputs(node.id):
            source = graph.nodes.get(item["node_id"])
            if source is not None and source.type == "storyboard" and isinstance(source.data.get("shots"), list):
                shots = list(source.data["shots"])
                break
        if not shots:
            continue
        refs = _reference_rows(graph, node)
        name_by_id = {ref["asset_id"]: ref["name"] for ref in refs}
        rendered = _render_segment_text(_first_shot(shots, _segment_no(node, 0)), name_by_id)
        if not rendered:
            continue
        if node.data.get("prompt") == rendered and node.data.get("rendered_by") == "server":
            continue
        node.data["prompt"] = rendered
        node.data["rendered_by"] = "server"
        written += 1
    return written


def _repair_links(graph: Canvas) -> int:
    """补齐 Agent 漏掉的「分镜 → 提示词」连线，并重渲染服务端正文，返回改动条数。

    这条边是制作页读分镜表和台词的唯一入口：漏了不会报错，只会让片段详情
    悄无声息地变空。图上的不变量就该由系统保证，不能指望模型每次都记得连。
    """
    storyboards: dict[tuple[int, int], Any] = {}
    order = {nid: index for index, nid in enumerate(graph.nodes)}
    for node in graph.nodes.values():
        if node.type != "storyboard":
            continue
        if node.data.get("orphaned"):
            continue
        key = (_episode_no(node), int(node.data.get("segment_no") or 0))
        prev = storyboards.get(key)
        # 同段号多条分镜时取建图最新的：setdefault 静默取第一个，二次 produce 会连到旧表
        if prev is None or order.get(node.id, 0) > order.get(prev.id, 0):
            storyboards[key] = node
    added = 0
    for node in list(graph.nodes.values()):
        if node.type != "prompt":
            continue
        source = storyboards.get((_episode_no(node), int(node.data.get("segment_no") or 0)))
        if source is None:
            continue
        if any(edge.source == source.id for edge in graph.incoming(node.id)):
            continue
        try:
            graph.connect(source.id, "shots", node.id, "ctx")
        except Exception:  # noqa: BLE001 - 端口/类型不合就是模型建错了，跳过即可
            continue
        added += 1
    # 重渲染也算改动：只补连线时 added 可能为 0，但正文已经写进画布，
    # 调用方靠返回值决定要不要落盘，漏算就会让 data.prompt 只活在内存里。
    return added + _sync_rendered_prompts(graph)


def _reconcile_produce(graph: Canvas, episode_no: int) -> dict[str, Any]:
    """produce 幂等收尾：同集重跑策划后按（集, 段号）归并重复节点。

    只标记、不删除——历史 take 是花过钱的决策记录。规则：
    - prompt 按段号分组：保留「生成槽 takes 多 > 建图新」的那套，其余 data.orphaned=true
    - storyboard 同段号多条：保留最新，其余 orphaned
    - 资产按 (kind, 清洁名) 分组：同组非首个 orphaned（资产无 take，零成本）
    """
    order = {nid: index for index, nid in enumerate(graph.nodes)}

    def _gen_takes(node_id: str) -> int:
        node = graph.nodes.get(node_id)
        if node is None:
            return 0
        for edge in graph.outgoing(node.id):
            target = graph.nodes.get(edge.target)
            if target is not None and target.type in ("video_gen", "image_gen"):
                return len(target.data.get("takes") or [])
        return 0

    orphaned: list[str] = []

    # 1) 片段（prompt）按段号归并
    kept = 0
    seg_groups: dict[int, list[Any]] = {}
    for node in graph.nodes.values():
        if node.type != "prompt" or node.data.get("orphaned") or _episode_no(node) != episode_no:
            continue
        seg_groups.setdefault(int(_segment_no(node, 0)), []).append(node)
    for nodes in seg_groups.values():
        ranked = sorted(nodes, key=lambda n: (_gen_takes(n.id), order.get(n.id, 0)), reverse=True)
        kept += 1
        for extra in ranked[1:]:
            extra.data["orphaned"] = True
            orphaned.append(extra.id)

    # 2) 分镜（storyboard）同段号保留最新
    sb_groups: dict[tuple[int, int], list[Any]] = {}
    for node in graph.nodes.values():
        if node.type != "storyboard" or node.data.get("orphaned") or _episode_no(node) != episode_no:
            continue
        sb_groups.setdefault((_episode_no(node), int(node.data.get("segment_no") or 0)), []).append(node)
    for nodes in sb_groups.values():
        if len(nodes) <= 1:
            continue
        ranked = sorted(nodes, key=lambda n: order.get(n.id, 0), reverse=True)
        for extra in ranked[1:]:
            extra.data["orphaned"] = True
            orphaned.append(extra.id)

    # 3) 资产按 (kind, 清洁名) 去重（资产是项目级，跨集去重）
    #    有形象图的优先保留——重画一张形象图是真金白银，空壳新节点不该顶掉它
    merged = 0
    asset_groups: dict[tuple[str, str], list[Any]] = {}
    for node in graph.nodes.values():
        if node.type != "asset" or node.data.get("orphaned"):
            continue
        name = _clean_asset_name(str(node.data.get("name") or node.label or ""))
        asset_groups.setdefault((str(node.data.get("kind") or ""), name), []).append(node)

    def _has_look_image(node: Any) -> int:
        return int(any(look.get("image_path") for look in (node.data.get("looks") or []) if isinstance(look, dict)))

    for nodes in asset_groups.values():
        if len(nodes) <= 1:
            continue
        ranked = sorted(nodes, key=lambda n: (_has_look_image(n), order.get(n.id, 0)), reverse=True)
        for extra in ranked[1:]:
            extra.data["orphaned"] = True
            orphaned.append(extra.id)
            merged += 1

    return {"kept": kept, "orphaned": orphaned, "merged_assets": merged}


def _produce_counts(graph: Canvas) -> dict[str, int]:
    return {
        "segments": sum(1 for node in graph.nodes.values() if node.type == "prompt"),
        "assets": sum(1 for node in graph.nodes.values() if node.type == "asset"),
        "storyboards": sum(1 for node in graph.nodes.values() if node.type == "storyboard"),
    }


@router.post("/produce")
async def produce(body: ProduceRequest) -> dict[str, Any]:
    """让 Agent 把某一集做成「可以进入制作」的状态（拆分镜 / 提资产 / 写提示词 / 建生成槽）。"""
    _require_configured()
    if _agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 运行时未装配")
    graph = await _load(body.canvas)
    prompt, _node_id = _produce_prompt(graph, int(body.episode or 1), body.instruction)

    # 同一项目同时只允许一个策划任务，避免两个 Agent 同时往一张画布里写节点。
    for job in _jobs.values():
        if job.get("canvas") == graph.name and job.get("kind") == "produce" and job.get("status") in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="这个项目已经在拆分镜了，等它跑完")

    job_id = f"produce_{uuid.uuid4().hex[:12]}"
    _jobs[job_id] = {
        "job_id": job_id,
        "kind": "produce",
        "canvas": graph.name,
        "episode": int(body.episode or 1),
        "status": "queued",
        "created_at": time.time(),
        "error": "",
    }
    _tasks[job_id] = asyncio.create_task(
        _run_produce(job_id, graph.name, prompt, _produce_session_id(graph.name), int(body.episode or 1))
    )
    return {"ok": True, "job": _jobs[job_id]}

