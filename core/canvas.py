"""画布：短剧生产图（节点 + 连线），存成工作区里的一个 JSON 文件。

对照 Toonflow：画布是工作区里的 `*.json`（带 toonflowCanvas 标记），**不是数据库表**。
  · 节点  = 一个产物单元（剧本/分镜/资产/提示词）
  · 连线  = 数据流（上游产出喂给下游），有类型校验
  · 正文不进 JSON：画布只存路径，正文落 assets/<node_id>/content.md

和 Toonflow 的一处**有意差异**：
  Toonflow 没有拓扑排序，执行顺序靠 Agent 读边现场推理；
  这里把顺序算出来（Kahn 算法 + 环检测），使"画面链接"有确定语义 —— 谁依赖谁、先做谁。

为什么这样设计（面试可讲）：
  1. 状态放在服务端文件 → 不需要前端↔服务端双向桥（Toonflow 用 canvasCall 事件 + 回传）
  2. 图是显式的 → 校验、排序、导出都能在同一份数据上做
  3. 原子写（tmp + replace）→ 生成中途崩溃不会留下半个画布
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

CANVAS_MARKER = "toonflow_canvas"  # 识别是不是画布文件（对应 Toonflow 的 toonflowCanvas 标记）

# ---------------------------------------------------------------------------
# 节点类型与端口声明
# ---------------------------------------------------------------------------
# 每个节点类型声明自己的输入/输出端口：连线必须"类型对得上"，和接插件一样。
# 类型：STRING（文本） / IMAGE / VIDEO / AUDIO；"*" 表示通配。
NODE_SPEC: dict[str, dict[str, dict[str, str]]] = {
    "image": {
        "label": "图片素材",
        "in": {},
        "out": {"image": "IMAGE"},
    },
    "video": {
        "label": "视频素材",
        "in": {},
        "out": {"video": "VIDEO"},
    },
    "audio": {
        "label": "音频素材",
        "in": {},
        "out": {"audio": "AUDIO"},
    },
    "text": {
        "label": "文本（剧本/大纲/备注）",
        "in": {"in": "STRING"},
        "out": {"text": "STRING"},
    },
    "storyboard": {
        "label": "分镜（片段 → 镜头）",
        "in": {"script": "STRING"},
        "out": {"shots": "STRING"},
    },
    "asset": {
        "label": "资产（角色/场景/道具/风格）",
        "in": {"desc": "STRING", "image": "IMAGE"},
        "out": {"ref": "IMAGE", "spec": "STRING"},
    },
    "prompt": {
        "label": "片段提示词（一个片段一份）",
        "in": {"ref": "IMAGE", "video_ref": "VIDEO", "ctx": "STRING"},
        "out": {"prompt": "STRING"},
    },
    "image_gen": {
        "label": "图片生成",
        "in": {"prompt": "STRING", "ref": "IMAGE"},
        "out": {"image": "IMAGE"},
    },
    "video_gen": {
        "label": "视频生成",
        "in": {"prompt": "STRING", "ref": "IMAGE", "video_ref": "VIDEO"},
        "out": {"video": "VIDEO"},
    },
    "tts_gen": {
        "label": "旁白生成",
        "in": {"text": "STRING"},
        "out": {"audio": "AUDIO"},
    },
    "timeline": {
        "label": "时间线 / 成片",
        "in": {"video": "VIDEO", "audio": "AUDIO", "subtitle": "STRING"},
        "out": {"video": "VIDEO"},
    },
}

NODE_STATUS = ("draft", "ready", "generating", "done", "error")

# 分镜字段（对齐「一镜一段」契约）：镜头结构存成数据，正文由服务端按字段渲染，
# 所以「时长 / 景别 / 机位 / 主体」都能被代码判定，而不是靠模型把长文写对。
SHOT_SIZES = ("极特写", "大特写", "特写", "近景", "中景", "全景", "大全景", "远景", "大远景", "空镜")
SHOT_MIN_S = 3.0
SHOT_MAX_S = 8.0
SHOT_CAMERA_FIELDS = (("height", "机位高度"), ("angle", "机位角度"), ("movement", "运镜"))
ASSET_KINDS = ("character", "scene", "prop", "style")
# 分镜 action 里的 `@名字`；`@图片1…` 这种已物化写法不算。
_SHOT_MENTION = re.compile(r"(?<!图片)(?<!\d)@([^\s@，,。；;：:、）)】\]\"'“”]{1,24})")

# 项目画幅 → 分辨率高度（与 server/studio.py 的历史口径保持一致）。
_QUALITY_HEIGHTS = {"480P": 854, "720P": 1280, "1080P": 1920}
_RESOLUTIONS = {"480p", "720p", "1080p"}


def normalize_resolution(value: Any) -> str | None:
    """把 ``720P`` / ``720p`` / ``quality=720P`` 归一成 provider 认识的 ``720p``。"""
    text = str(value or "").strip().lower()
    return text if text in _RESOLUTIONS else None


def media_size(aspect_ratio: Any, quality: Any) -> str | None:
    """按画幅与画质算一个偶数边长的 ``WxH``（供图片节点直接传给 provider）。

    与 ``server/studio.py`` 的 ``_default_size`` 同口径；算不出来时返回 None，
    交给调用方退回请求参数，而不是悄悄用一个默认值覆盖用户设置。
    """
    ratio = str(aspect_ratio or "").strip()
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[:xX×/]\s*(\d+(?:\.\d+)?)\s*", ratio)
    if not match:
        return None
    width_ratio, height_ratio = float(match.group(1)), float(match.group(2))
    if width_ratio <= 0 or height_ratio <= 0:
        return None
    height = _QUALITY_HEIGHTS.get(str(quality or "").strip().upper())
    if height is None:
        return None
    width = int(round(height * width_ratio / height_ratio))
    return f"{width - width % 2}x{height - height % 2}"


def build_media_request(
    job: dict[str, Any],
    *,
    provider: str = "",
    model: str = "",
    generate_audio: bool = False,
) -> dict[str, Any]:
    """把一条生成任务翻译成 Provider 请求（画布 Worker 与 Agent 工具共用）。

    为什么必须共用：Provider 只认 ``prompt/size/duration/resolution/ratio``，
    「横竖屏、几秒」这类业务口径已经在 ``Canvas.generation_params`` 里解析过。
    如果每个调用方自己拼请求，就会出现「Worker 用 9:16、Agent 用硬编码 16:9」
    这种用户看得见、代码里却很难发现的双轨。

    ``job`` 支持两种形状：画布 ``generation_jobs()`` 的 ``params`` 嵌套结构，
    以及审批后冻结的 flat 字段。缺失的 key 不猜默认值（除 size 的兼容值）。
    """
    kind = str(job.get("kind") or "image")
    params = dict(job.get("params") or {})
    for key in ("duration", "ratio", "resolution", "size"):
        if params.get(key) in (None, "") and job.get(key) not in (None, ""):
            params[key] = job.get(key)
    size = params.get("size") or ("1024x576" if kind == "image" else "1280x720")
    references = [
        {
            "path": str(ref["path"]),
            "type": str(ref.get("type") or ""),
            "mime_type": str(ref.get("mime_type") or ""),
        }
        for ref in (job.get("references") or job.get("refs") or [])
        if isinstance(ref, dict) and ref.get("path")
    ]
    request: dict[str, Any] = {
        "prompt": str(job.get("prompt") or ""),
        "model": str(model or ""),
        "size": size,
        "generate_audio": bool(generate_audio),
        "references": references,
    }
    for key in ("duration", "resolution", "ratio"):
        if params.get(key) not in (None, ""):
            request[key] = params[key]
    if provider:
        request["provider"] = provider
    return request


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


def _shot_label(item: Any, name_by_id: dict[str, str] | None = None) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("name") or (name_by_id or {}).get(str(item.get("id") or "")) or item.get("id") or "")


def shot_problems(shot: dict[str, Any], name_by_id: dict[str, str] | None = None) -> list[str]:
    """单个镜头的字段问题（纯函数，引擎层与业务校验共用一份口径）。

    这里的每一条都直接决定服务端能不能渲染出可用正文，所以「缺」就是真问题。
    """
    out: list[str] = []
    seconds = _shot_seconds(shot)
    if seconds <= 0:
        out.append("缺 duration_s（一镜一段，单镜 3–8 秒）")
    elif not (SHOT_MIN_S - 1e-6 <= seconds <= SHOT_MAX_S + 1e-6):
        out.append(f"时长 {seconds:g}s 不在 {SHOT_MIN_S:g}–{SHOT_MAX_S:g}s 区间（一镜一段，常规 4–6 秒）")

    size = str(shot.get("shot_size") or "").strip()
    if not size:
        out.append("缺 shot_size")
    elif size not in SHOT_SIZES:
        out.append(f"景别「{size}」不在枚举内：{'、'.join(SHOT_SIZES)}")

    camera = shot.get("camera") if isinstance(shot.get("camera"), dict) else {}
    missing_camera = [label for key, label in SHOT_CAMERA_FIELDS if not str(camera.get(key) or "").strip()]
    if missing_camera:
        out.append("机位缺 " + "、".join(missing_camera))

    subjects = shot.get("subjects") if isinstance(shot.get("subjects"), list) else []
    subjects = [item for item in subjects if isinstance(item, dict)]
    if not subjects:
        out.append("subjects 为空：每镜至少写一个主体（空镜也要写画面无人）")
    elif len(subjects) >= 2:
        for index, item in enumerate(subjects, start=1):
            missing = [field for field in ("gaze", "screen_pos") if not str(item.get(field) or "").strip()]
            if missing:
                out.append(f"第 {index} 个主体缺 {'、'.join(missing)}（同镜 ≥2 人时必须写清视线与画面位置）")

    declared = {_shot_label(item, name_by_id) for item in subjects}
    declared |= {
        _shot_label(item, name_by_id)
        for item in (shot.get("props") if isinstance(shot.get("props"), list) else [])
        if isinstance(item, dict)
    }
    declared.discard("")
    mentioned = {match.group(1).strip() for match in _SHOT_MENTION.finditer(str(shot.get("action") or ""))}
    if declared:
        dangling = sorted(mentioned - declared)
        if dangling:
            out.append(
                "action 引用了本镜没声明的资产："
                + "、".join("@" + name for name in dangling)
                + "（引用闭包：@ 到的名字必须在 subjects/props 里）"
            )
    return out


def asset_problems(data: dict[str, Any]) -> list[str]:
    """资产节点的字段问题：字段缺失会让「一致性」无从写起。"""
    out: list[str] = []
    kind = str(data.get("kind") or "").strip()
    if not kind:
        out.append("缺 kind（character/scene/prop/style）")
    elif kind not in ASSET_KINDS:
        out.append(f"kind「{kind}」不在枚举内：{'、'.join(ASSET_KINDS)}")
    for field, label in (("name", "资产名"), ("spec", "外观说明")):
        if not str(data.get(field) or "").strip():
            out.append(f"缺 {field}（{label}）")
    for field, label in (("anchors", "外观锚点"), ("consistency_tags", "一致性标签")):
        value = data.get(field)
        items = [item for item in value if str(item).strip()] if isinstance(value, list) else []
        if not items:
            out.append(f"缺 {field}（{label}）")
    spec = str(data.get("spec") or "")
    if "三视图" in spec or "三面图" in spec:
        out.append("spec 要求三视图：本项目一图一人，生成单角度参考图（不要三视图并排）")
    return out


def _compatible(source_type: str, target_type: str) -> bool:
    """端口类型兼容：相等，或任一侧为通配 *（对应 Toonflow 的 isTypeCompatible）。"""
    return source_type == target_type or "*" in (source_type, target_type)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:6]}"


def story_similarity(left: str, right: str) -> float:
    """中文友好的轻量故事相似度（字符二元组包含系数）。

    **不再用于路由。** 曾经用它把自然语言创意匹配到「最像」的一张画布（阈值 0.34），
    结果 40% 的相似度就足以让 Agent 把新故事写进别人的项目、并改掉人家的标题——
    模型没有能力判断两个项目是否同一故事，用户也无法预期它什么时候会认错。
    现在画布由用户在界面上选定，工具侧只认当前画布（见 core/context.py 的作用域仓库）。
    这个函数保留下来只作离线分析与回归对照。
    """
    stop = ("帮我", "做一段", "生成", "制作", "视频", "动画", "短剧", "一个", "一段", "请")

    def features(value: str) -> set[str]:
        value = value.lower()
        for word in stop:
            value = value.replace(word, "")
        value = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)
        if len(value) < 2:
            return {value} if value else set()
        return {value[index : index + 2] for index in range(len(value) - 1)}

    a, b = features(left), features(right)
    if not a or not b:
        return 0.0
    return round(len(a & b) / min(len(a), len(b)), 4)


@dataclass
class Node:
    id: str
    type: str
    label: str
    position: dict[str, float] = field(default_factory=lambda: {"x": 0.0, "y": 0.0})
    status: str = "draft"
    data: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def in_ports(self) -> dict[str, str]:
        return NODE_SPEC.get(self.type, {}).get("in", {})

    @property
    def out_ports(self) -> dict[str, str]:
        return NODE_SPEC.get(self.type, {}).get("out", {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "position": self.position,
            "status": self.status,
            "data": self.data,
            "outputs": self.outputs,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Node":
        return cls(
            id=raw["id"],
            type=raw.get("type", "text"),
            label=raw.get("label", ""),
            position=raw.get("position") or {"x": 0.0, "y": 0.0},
            status=raw.get("status", "draft"),
            data=raw.get("data") or {},
            outputs=raw.get("outputs") or {},
        )


@dataclass
class Edge:
    id: str
    source: str
    source_handle: str
    target: str
    target_handle: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "source_handle": self.source_handle,
            "target": self.target,
            "target_handle": self.target_handle,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Edge":
        return cls(
            id=raw.get("id") or _new_id("e"),
            source=raw["source"],
            source_handle=raw.get("source_handle", ""),
            target=raw["target"],
            target_handle=raw.get("target_handle", ""),
        )


class CanvasError(Exception):
    """画布操作被拒绝（类型不符 / 自环 / 找不到节点等）。"""


class Canvas:
    """一个画布文件的内存模型。所有改动都通过方法做，方便校验与落盘。"""

    def __init__(self, name: str, path: Path | None = None) -> None:
        self.name = name
        self.path = path
        # Repository optimistic-lock token. A newly created canvas starts at 0;
        # every successful persisted write increments it exactly once.
        self.revision = 0
        self.persisted = False
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        # 一张画布只承载一个独立故事。meta 不参与节点执行，但用于新请求路由、
        # 防止不同剧本误连，以及在前端显示人类可读的剧本名。
        self.meta: dict[str, Any] = {}
        self.viewport: dict[str, float] = {"x": 0.0, "y": 0.0, "zoom": 1.0}

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Path, name: str | None = None) -> "Canvas":
        if not path.exists():
            return cls(name or path.stem, path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        canvas = cls.from_dict(raw, name=name or path.stem, path=path)
        canvas.persisted = True
        return canvas

    @classmethod
    def from_dict(cls, raw: dict[str, Any], name: str | None = None, path: Path | None = None) -> "Canvas":
        canvas = cls(raw.get("name") or name or (path.stem if path else "main"), path)
        canvas.meta = raw.get("meta") or {}
        canvas.viewport = raw.get("viewport") or {"x": 0.0, "y": 0.0, "zoom": 1.0}
        canvas.nodes = {n["id"]: Node.from_dict(n) for n in raw.get("nodes", [])}
        canvas.edges = [Edge.from_dict(e) for e in raw.get("edges", [])]
        canvas.revision = int(raw.get("revision") or 0)
        return canvas

    def to_dict(self) -> dict[str, Any]:
        return {
            CANVAS_MARKER: True,
            "name": self.name,
            "revision": self.revision,
            "meta": self.meta,
            "viewport": self.viewport,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
        }

    def save(self) -> None:
        """原子写：先写临时文件再替换 —— 崩在中间也不会留下半个画布。"""
        if self.path is None:
            return
        self.revision += 1
        self.persisted = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        # Windows 下目标文件被别的句柄打开时 os.replace 会抛 WinError 5，退避重试。
        for attempt in range(12):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if attempt == 11:
                    raise
                time.sleep(0.05 * (attempt + 1))

    # ------------------------------------------------------------------
    # 节点 / 连线操作（都做校验）
    # ------------------------------------------------------------------
    def ensure_story(self, title: str, summary: str = "", story_id: str = "") -> str:
        """给画布建立稳定的故事身份，并把旧节点迁入该身份。"""
        title = title.strip() or self.name
        summary = summary.strip()
        if not story_id:
            seed = f"{title}\n{summary}".encode("utf-8")
            story_id = str(self.meta.get("story_id") or f"story_{hashlib.sha1(seed).hexdigest()[:10]}")
        self.meta.update({"story_id": story_id, "story_title": title, "story_summary": summary})
        for node in self.nodes.values():
            node.data.setdefault("story_id", story_id)
        return story_id

    def story_text(self) -> str:
        """用于识别是否为同一剧本的短文本指纹（不把完整正文塞进工具结果）。"""
        bits = [str(self.meta.get("story_title") or ""), str(self.meta.get("story_summary") or "")]
        for node in self.nodes.values():
            if node.type in ("text", "storyboard"):
                bits.append(node.label)
                bits.append(self.read_text(node.id)[:700])
        return "\n".join(bit for bit in bits if bit)

    def add_node(
        self,
        type: str,
        label: str,
        position: dict[str, float] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Node:
        if type not in NODE_SPEC:
            raise CanvasError(f"未知节点类型：{type}（可选：{', '.join(NODE_SPEC)}）")
        # 没给位置就按大网格铺开（间距要容得下带缩略图的生成节点，否则会叠在一起）
        if position is None:
            index = len(self.nodes)
            position = {"x": 80.0 + (index % 4) * 300.0, "y": 80.0 + (index // 4) * 300.0}
        node_data = dict(data or {})
        if self.meta.get("story_id"):
            node_data.setdefault("story_id", self.meta["story_id"])
        node = Node(id=_new_id("n"), type=type, label=label, position=position, data=node_data)
        self.nodes[node.id] = node
        return node

    def update_node(self, node_id: str, *, invalidate_generation: bool = True, **fields: Any) -> Node:
        node = self.require(node_id)
        if fields.get("status") is not None and fields["status"] not in NODE_STATUS:
            raise CanvasError(f"未知状态：{fields['status']}（可选：{', '.join(NODE_STATUS)}）")
        for key in ("label", "status", "position", "data", "outputs"):
            if key not in fields or fields[key] is None:
                continue
            if key in ("data", "outputs"):
                merged = dict(getattr(node, key))
                merged.update(fields[key])
                # A user/Agent edit made while a Worker is rendering must
                # invalidate that Worker's generation token.  The Worker only
                # writes ``active_generation_id`` itself, so that internal
                # update is exempt from invalidation.  Worker-internal writes
                # (进度/状态) pass invalidate_generation=False for the same
                # reason: a task must never invalidate its own token.
                if (
                    key == "data"
                    and invalidate_generation
                    and node.data.get("active_generation_id")
                    and any(name != "active_generation_id" for name in fields[key])
                ):
                    merged["active_generation_id"] = f"edit_{uuid.uuid4().hex}"
                setattr(node, key, merged)
            else:
                setattr(node, key, fields[key])
        return node

    def require(self, node_id: str) -> Node:
        node = self.nodes.get(node_id)
        if node is None:
            raise CanvasError(f"节点不存在：{node_id}（用 get_canvas 看现有节点 id）")
        return node

    def connect(self, source: str, source_handle: str, target: str, target_handle: str) -> Edge:
        """连线：校验节点存在、端口存在、类型兼容、不许自环、不许重复、**不许成环**。"""
        src, dst = self.require(source), self.require(target)
        if source == target:
            raise CanvasError("不能把节点连到自己")
        src_story = str(src.data.get("story_id") or "")
        dst_story = str(dst.data.get("story_id") or "")
        if src_story and dst_story and src_story != dst_story:
            raise CanvasError(
                f"不能跨剧本连线：「{src.label}」属于 {src_story}，"
                f"「{dst.label}」属于 {dst_story}。请先切换到对应剧本画布"
            )
        if source_handle not in src.out_ports:
            raise CanvasError(
                f"{src.label} 没有输出端口 {source_handle}（可用：{', '.join(src.out_ports) or '无'}）"
            )
        if target_handle not in dst.in_ports:
            raise CanvasError(
                f"{dst.label} 没有输入端口 {target_handle}（可用：{', '.join(dst.in_ports) or '无'}）"
            )
        src_type, dst_type = src.out_ports[source_handle], dst.in_ports[target_handle]
        if not _compatible(src_type, dst_type):
            raise CanvasError(f"类型不匹配：{source_handle}({src_type}) → {target_handle}({dst_type})")
        for edge in self.edges:
            if (edge.source, edge.source_handle, edge.target, edge.target_handle) == (
                source,
                source_handle,
                target,
                target_handle,
            ):
                return edge  # 幂等：重复连线直接返回已有边

        # 成环检查：画布是**有方向的流水线**（topo_order 依赖这一点）。
        # 加边前先试算：若 source 已经在 target 的下游，这条边就把流程变成"互相等待"。
        # 必须在写入前拦掉 —— 环一旦落盘，整个画布就排不了序，所有生成都做不了。
        chain = self.find_path(target, source)
        if chain is not None:
            # chain 是 target → … → source；接上这条边后回到 target，所以用 target 闭合
            route = " → ".join(self.nodes[i].label for i in [*chain, target])
            raise CanvasError(
                f"这条边会形成环：{route}，"
                "流程将无法排序（画布是流水线，不能互相依赖）"
            )

        edge = Edge(id=_new_id("e"), source=source, source_handle=source_handle, target=target, target_handle=target_handle)
        self.edges.append(edge)
        return edge

    def remove_nodes(self, node_ids: Iterable[str]) -> int:
        ids = list(node_ids)
        for node_id in ids:
            self.nodes.pop(node_id, None)
        before = len(self.edges)
        self.edges = [e for e in self.edges if e.source not in ids and e.target not in ids]
        return len(ids), before - len(self.edges)

    def remove_edges(self, edge_ids: Iterable[str]) -> int:
        ids = set(edge_ids)
        before = len(self.edges)
        self.edges = [e for e in self.edges if e.id not in ids]
        return before - len(self.edges)

    # ------------------------------------------------------------------
    # 图算法
    # ------------------------------------------------------------------
    def incoming(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.target == node_id]

    def outgoing(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.source == node_id]

    def edge_type(self, edge: Edge) -> str:
        """这条边上游输出的是什么类型。"""
        node = self.nodes.get(edge.source)
        return node.out_ports.get(edge.source_handle, "*") if node else "*"

    def refs_for(self, node_id: str, only_image: bool = True) -> list[dict[str, Any]]:
        """入边的**参考编号**：{{ref 1}}、{{ref 2}}…

        只有 IMAGE 类型的入边才发号牌（默认 only_image=True）——
        分镜文本（STRING）是"上下文"，不是"参考图"，给它编号只会让人看不懂。
        （Toonflow 的做法是文字与媒体共用编号；这里按用途分开发号，导出时更好读。）

        排序键 = **上游节点的创建顺序** + 端口名：
        · 稳定：建完图编号就不再变（Toonflow 用 referenceOrder 达到同样目的）
        · 可预测：先建的角色就是 {{ref 1}}，而不是按随机 id 排出一个看不懂的顺序
        同一个资产在每个片段里都拿到同一个编号 —— 这就是"角色一致性"的落点。
        """
        order_index = {nid: index for index, nid in enumerate(self.nodes)}
        edges = sorted(self.incoming(node_id), key=lambda e: (order_index.get(e.source, 10**9), e.source_handle))
        if only_image:
            edges = [e for e in edges if self.edge_type(e) == "IMAGE"]
        result = []
        for index, edge in enumerate(edges, 1):
            media = self.media_output(edge.source, self.edge_type(edge))
            result.append({
                "ref": index,
                "node_id": edge.source,
                "handle": edge.source_handle,
                "label": self.nodes[edge.source].label if edge.source in self.nodes else edge.source,
                "type": self.edge_type(edge),
                "path": media.get("path", ""),
                "mime_type": media.get("mime_type") or media.get("mime") or "",
            })
        return result

    def media_output(self, node_id: str, data_type: str, seen: set[str] | None = None) -> dict[str, Any]:
        """解析一个媒体输出；资产卡可把上游上传图片作为自己的真实参考图。"""
        seen = set(seen or ())
        if node_id in seen:
            return {}
        seen.add(node_id)
        node = self.nodes.get(node_id)
        if node is None:
            return {}
        for output in node.outputs.values():
            if str(output.get("data_type") or "").upper() != data_type.upper():
                continue
            value = output.get("value") or {}
            if isinstance(value, dict) and value.get("path"):
                return dict(value)
        for edge in self.incoming(node_id):
            if self.edge_type(edge) == data_type:
                value = self.media_output(edge.source, data_type, seen)
                if value:
                    return value
        return {}

    def generation_refs(self, node_id: str) -> list[dict[str, Any]]:
        """生成节点的真实媒体参考：直接连接 + 上游 prompt 已绑定的参考。"""
        edges = list(self.incoming(node_id))
        prompt_ids = [
            edge.source for edge in edges
            if self.nodes.get(edge.source) is not None and self.nodes[edge.source].type == "prompt"
        ]
        media_edges = [edge for edge in edges if self.edge_type(edge) in ("IMAGE", "VIDEO")]
        for prompt_id in prompt_ids:
            media_edges.extend(
                edge for edge in self.incoming(prompt_id) if self.edge_type(edge) in ("IMAGE", "VIDEO")
            )
        result: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for edge in media_edges:
            data_type = self.edge_type(edge)
            media = self.media_output(edge.source, data_type)
            path = str(media.get("path") or "")
            key = path or f"{edge.source}:{edge.source_handle}"
            if key in seen_paths:
                continue
            seen_paths.add(key)
            source = self.nodes.get(edge.source)
            result.append({
                "node_id": edge.source,
                "label": source.label if source else edge.source,
                "type": data_type,
                "path": path,
                "mime_type": media.get("mime_type") or media.get("mime") or "",
            })
        return result

    def text_inputs(self, node_id: str) -> list[dict[str, Any]]:
        """入边里的**文本上下文**（分镜表这类），不参与参考编号。"""
        order_index = {nid: index for index, nid in enumerate(self.nodes)}
        edges = sorted(self.incoming(node_id), key=lambda e: (order_index.get(e.source, 10**9), e.source_handle))
        out = []
        for edge in edges:
            if self.edge_type(edge) == "IMAGE":
                continue
            node = self.nodes.get(edge.source)
            out.append(
                {
                    "node_id": edge.source,
                    "handle": edge.source_handle,
                    "label": node.label if node else edge.source,
                    "type": node.type if node else "?",
                    "text": self.read_text(edge.source) if node else "",
                }
            )
        return out

    # ------------------------------------------------------------------
    # 正文读写与资产描述
    # ------------------------------------------------------------------
    def root(self) -> Path:
        return self.path.parent if self.path else Path.cwd()

    def read_text(self, node_id: str) -> str:
        """读节点正文。

        正文有多个存放位置（历史原因）：``assets/<id>/content.md`` 大文件、
        ``outputs.prompt/text`` 单行正文、``data.prompt`` 卡片内联提示词。
        S5 之后新写的正文多落在 ``data.prompt`` / ``outputs["prompt"]``，
        所以这里按「文件 → outputs.prompt → outputs.text → outputs.shots → data.prompt」
        依次回退；生成、体检、导出、制片台必须共用这一个入口，
        否则会出现「界面看得到、生成读不到」。
        """
        node = self.nodes.get(node_id)
        if node is None:
            return ""
        relative = node.data.get("content_path")
        if relative:
            path = self.root() / relative
            if path.exists():
                text = path.read_text(encoding="utf-8")
                if text.strip():
                    return text
        for key in ("prompt", "text", "shots"):
            value = (node.outputs.get(key) or {}).get("value")
            if isinstance(value, str) and value.strip():
                return value
        prompt = node.data.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            return prompt
        return ""

    def asset_desc(self, node_id: str) -> str:
        """资产的"一句话描述"：标签已经带了类型，后面接稳定特征。

        这段文字会被拼进提示词前言 —— 它是"角色长什么样"的唯一出处，
        所以一致性其实靠：**设定只存一份，每次展开时引用同一段文字**。
        """
        node = self.nodes.get(node_id)
        if node is None:
            return node_id
        data = node.data or {}
        bits = [str(data.get("name") or node.label)]
        tags = data.get("consistency_tags") or []
        if tags:
            bits.append("、".join(str(t) for t in tags))
        desc = data.get("description")
        if desc:
            bits.append(str(desc))
        return f"{'，'.join(bits)}"

    def project_meta(self) -> dict[str, Any]:
        """项目级默认参数（画幅 / 画质 / 视频模型），由前端项目设置写进 meta.project。

        画布层只读、不猜默认值：缺失就返回 None，让调用方退回请求参数，
        避免用一个隐藏默认值悄悄覆盖用户设置。
        """
        project = self.meta.get("project")
        return dict(project) if isinstance(project, dict) else {}

    def _segment_seconds(self, node: Node) -> float | None:
        """节点所属片段在分镜里的总时长（node.data.segment_no 对上 shots 的 segment_no）。"""
        if not node.data.get("segment_no"):
            return None
        try:
            segment_no = int(node.data.get("segment_no"))
        except (TypeError, ValueError):
            return None
        total = 0.0
        found = False
        for other in self.nodes.values():
            if other.type != "storyboard":
                continue
            for shot in other.data.get("shots") or []:
                try:
                    if int(shot.get("segment_no") or 1) != segment_no:
                        continue
                except (TypeError, ValueError):
                    continue
                total += _shot_seconds(shot)
                found = True
        return round(total, 2) if found and total > 0 else None

    def generation_params(self, node: Node) -> dict[str, Any]:
        """单次生成的媒体参数：节点自身 > 分镜镜头时长 > 项目 defaults > None（交给请求参数）。

        Provider 只拿 ``size/duration/resolution/ratio``，所以「横竖屏、几秒」这类
        业务口径必须在这一层决定，不能散落在前端常量或 Worker 里的硬编码。
        """
        data = node.data or {}
        project = self.project_meta()

        duration = data.get("duration", data.get("duration_s"))
        if duration in (None, ""):
            duration = self._segment_seconds(node)
        if duration in (None, ""):
            duration = project.get("duration")
        try:
            duration = min(30, max(1, int(round(float(duration))))) if duration not in (None, "") else None
        except (TypeError, ValueError):
            duration = None

        ratio = data.get("ratio") or data.get("aspect_ratio") or project.get("aspect_ratio")
        ratio = str(ratio).strip() if ratio not in (None, "") else None
        quality = data.get("quality") or data.get("resolution") or project.get("quality")
        resolution = normalize_resolution(quality)
        size = data.get("size") or (media_size(ratio, quality or "720P") if ratio else None)
        return {
            "duration": duration,
            "ratio": ratio,
            "resolution": resolution,
            "size": size,
        }

    def resolve_prompt(self, node_id: str) -> dict[str, Any]:
        """生成节点的提示词来源（显式 prompt 节点 > 其他文本输入 > 卡片自身提示词）。

        用户手动连了提示词节点就说明他想以连接为准；卡片里的文字此时作为
        「被覆盖」记录（``prompt_overridden``），而不是被悄悄丢掉。
        """
        node = self.require(node_id)
        own = str((node.data or {}).get("prompt") or "").strip()
        for edge in self.incoming(node_id):
            if edge.target_handle != "prompt" or edge.source not in self.nodes:
                continue
            text = self.read_text(edge.source).strip()
            if not text:
                continue
            source = "prompt_node" if self.nodes[edge.source].type == "prompt" else "text_input"
            return {
                "prompt": text,
                "prompt_source": source,
                "prompt_overridden": bool(own) and own != text,
            }
        for item in self.text_inputs(node_id):
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            return {
                "prompt": text,
                "prompt_source": "text_input",
                "prompt_overridden": bool(own) and own != text,
            }
        if own:
            return {"prompt": own, "prompt_source": "node", "prompt_overridden": False}
        return {"prompt": "", "prompt_source": None, "prompt_overridden": False}

    def record_generated_media(
        self,
        node_id: str,
        *,
        kind: str,
        path: str,
        mime: str,
        provider: str = "",
        mock: bool = False,
        model: str = "",
        duration_s: float | None = None,
        take_id: str | None = None,
        shot_no: int = 1,
        mode: str = "canvas",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """把一次生成结果写进节点：outputs + takes 历史 + current_take_id + status=done。

        画布 Worker 与 Agent 工具共用这一份写法。以前 Agent 只覆盖 outputs，
        「重新生成」等于把上一版抹掉，制作者没法对比、也没法回退；两边各写一份
        还会让 take 字段（duration_s/mode/rating）慢慢长歪。
        """
        node = self.require(node_id)
        port = "image" if kind == "image" else "video"
        take = {
            "take_id": take_id or _new_id("take"),
            "path": path,
            "mime": mime,
            "provider": provider,
            "mock": bool(mock),
            "model": model,
            "duration_s": float(duration_s) if duration_s not in (None, "") else None,
            "shot_no": int(shot_no),
            "mode": mode,
            "created_at": time.time(),
            "rating": "",
            "note": "",
        }
        # 调用方可附带额外凭据（如 stale 指纹）；None 值不落库，保持 take 形状干净。
        if extra:
            take.update({k: v for k, v in extra.items() if v is not None})
        takes = list(node.data.get("takes") or [])
        takes.append(take)
        # 只保留最近 30 版：历史是给"对比/回退"用的，不是无限增长的存储负担。
        node.data["takes"] = takes[-30:]
        # 采用语义：首次生成自动采用（此前什么都没有，无可保护）；重生成永不抢占——
        # 新版只进历史，等人在制片台/画布显式「采用」。历史是决策记录，不是最新值指针。
        # 指针丢失的旧画布（有历史但没 current_take_id）由新写入兜底接管。
        # outputs 必须跟随 current_take_id：采用哪版，下游（时间线/预览）就用哪版。
        if not node.data.get("current_take_id"):
            node.data["current_take_id"] = take["take_id"]
            node.outputs[port] = {
                "data_type": port.upper(),
                "value": {"path": path, "mime": mime, "mock": bool(mock), "provider": provider},
            }
        self.update_node(node_id, status="done")
        return take

    def find_path(self, start: str, goal: str) -> list[str] | None:
        """start 能否走到 goal；能就返回路径（给"为什么会成环"做解释用）。

        注意别叫 path —— `Canvas.path` 已经是画布文件路径了，同名会被实例属性遮住。
        """
        if start == goal:
            return [start]
        parent: dict[str, str] = {}
        queue = deque([start])
        seen = {start}
        while queue:
            node_id = queue.popleft()
            for edge in self.outgoing(node_id):
                nxt = edge.target
                if nxt in seen or nxt not in self.nodes:
                    continue
                seen.add(nxt)
                parent[nxt] = node_id
                if nxt == goal:
                    chain = [goal]
                    while chain[-1] != start:
                        chain.append(parent[chain[-1]])
                    return list(reversed(chain))
                queue.append(nxt)
        return None

    def cycles(self) -> list[list[str]]:
        """找出环（强连通分量，Tarjan）：每项是"互相依赖"的一组节点。

        为什么单独有这个：Kahn 排不出来时只能说"这些节点排不了序"，
        而其中大部分只是**依赖了环**——真正的环常常只有两三个节点，
        删边时要的是后者。所以把环精确算出来（体检消息与破环工具都用它）。
        """
        index_of: dict[str, int] = {}
        low: dict[str, int] = {}
        on_stack: set[str] = set()
        stack: list[str] = []
        found: list[list[str]] = []
        counter = 0

        adjacency: dict[str, list[str]] = {node_id: [] for node_id in self.nodes}
        for edge in self.edges:
            if edge.source in adjacency and edge.target in adjacency:
                adjacency[edge.source].append(edge.target)

        def visit(node_id: str) -> None:
            nonlocal counter
            index_of[node_id] = low[node_id] = counter
            counter += 1
            stack.append(node_id)
            on_stack.add(node_id)
            for nxt in adjacency[node_id]:
                if nxt not in index_of:
                    visit(nxt)
                    low[node_id] = min(low[node_id], low[nxt])
                elif nxt in on_stack:
                    low[node_id] = min(low[node_id], index_of[nxt])
            if low[node_id] == index_of[node_id]:
                group: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    group.append(member)
                    if member == node_id:
                        break
                if len(group) > 1:
                    found.append(group)

        for node_id in list(self.nodes):
            if node_id not in index_of:
                visit(node_id)
        return found

    def topo_order(self) -> list[str]:
        """拓扑排序（Kahn）：返回"先做谁、后做谁"。

        只在节点之间算依赖；有环就抛错（环意味着互相等待，永远做不完）。
        """
        indegree = {node_id: 0 for node_id in self.nodes}
        for edge in self.edges:
            if edge.source in indegree and edge.target in indegree:
                indegree[edge.target] += 1
        queue = deque(sorted(node_id for node_id, deg in indegree.items() if deg == 0))
        order: list[str] = []
        while queue:
            node_id = queue.popleft()
            order.append(node_id)
            for edge in sorted(self.outgoing(node_id), key=lambda e: e.target):
                indegree[edge.target] -= 1
                if indegree[edge.target] == 0:
                    queue.append(edge.target)
        if len(order) != len(self.nodes):
            # 只报**环内**节点：排不出来的那一堆里大部分只是依赖了环，
            # 把 8 个节点甩给用户、而环其实只有 3 个，等于没说清问题在哪。
            groups = self.cycles()
            if groups:
                detail = "；".join(
                    "、".join(self.nodes[i].label for i in group) for group in groups
                )
            else:
                detail = "、".join(self.nodes[i].label for i in sorted(set(self.nodes) - set(order)))
            blocked = len(self.nodes) - len(order) - sum(len(g) for g in groups)
            extra = f"（另有 {blocked} 个节点因依赖它而无法排序）" if blocked else ""
            raise CanvasError(f"存在环，流程无法排序：{detail}{extra}")
        return order

    def validate(self) -> list[str]:
        """体检：返回问题清单（空列表 = 一切正常）。"""
        problems: list[str] = []
        canvas_story = str(self.meta.get("story_id") or "")
        node_stories = {str(n.data.get("story_id")) for n in self.nodes.values() if n.data.get("story_id")}
        if len(node_stories) > 1:
            problems.append("当前画布混入了多个剧本，请使用剧本识别分流后再连线")
        if canvas_story:
            foreign = [n.label for n in self.nodes.values() if n.data.get("story_id") not in (None, "", canvas_story)]
            if foreign:
                problems.append(f"有 {len(foreign)} 个节点不属于当前剧本：{'、'.join(foreign[:3])}")
        try:
            self.topo_order()
        except CanvasError as exc:
            problems.append(str(exc))
        for edge in self.edges:
            if edge.source not in self.nodes or edge.target not in self.nodes:
                problems.append(f"边 {edge.id} 指向了不存在的节点")
        for node in self.nodes.values():
            if node.type == "prompt" and not self.incoming(node.id):
                problems.append(f"提示词节点「{node.label}」没有任何输入（至少连一个资产或分镜）")
            elif node.type == "prompt" and not self.refs_for(node.id):
                problems.append(f"提示词节点「{node.label}」没有连任何资产 → 展开后没有参考图，角色会不一致")
            # 正文可能落在 content.md / outputs.prompt / data.prompt —— 统一用 read_text 判断，别只看文件。
            if node.type == "prompt" and not self.read_text(node.id).strip():
                problems.append(f"提示词节点「{node.label}」还没有正文（卡片内填写，或用 set_text 写入），导出会是空片段")
            if node.type == "prompt" and not node.data.get("segment_no"):
                problems.append(f"提示词节点「{node.label}」缺少 segment_no，无法和分镜片段稳定对应")
            if node.type in ("image_gen", "video_gen"):
                resolved = self.resolve_prompt(node.id)
                if not resolved["prompt"]:
                    problems.append(f"生成节点「{node.label}」没有可用提示词（卡片内填写，或连接提示词/文本节点）")
            if node.type == "tts_gen":
                text_inputs = [edge for edge in self.incoming(node.id) if edge.target_handle == "text"]
                if not text_inputs and not str(node.data.get("text") or "").strip():
                    problems.append(f"旁白节点「{node.label}」没有文本输入")
            if node.type == "timeline":
                video_inputs = [edge for edge in self.incoming(node.id) if edge.target_handle == "video"]
                if not video_inputs:
                    problems.append(f"时间线「{node.label}」没有连接任何视频片段")
            if node.type == "asset":
                for problem in asset_problems(node.data):
                    problems.append(f"资产「{node.label}」{problem}")

        # 分镜：一镜一段的结构字段 + 时长预算 + 片段覆盖率（这三条都是"可判定"的）
        prompts = [n for n in self.nodes.values() if n.type == "prompt"]
        covered = {int(p.data.get("segment_no") or 0) for p in prompts}
        name_by_id = {nid: str(n.label or "") for nid, n in self.nodes.items()}
        for node in self.nodes.values():
            if node.type != "storyboard":
                continue
            shots = list(node.data.get("shots") or [])
            if not shots:
                problems.append(f"分镜「{node.label}」还没有结构化镜头数据（用 set_storyboard 写入）")
                continue
            for shot in shots:
                for problem in shot_problems(shot, name_by_id):
                    problems.append(f"分镜「{node.label}」镜{shot.get('no', '?')} {problem}")
            target = node.data.get("target_duration")
            total = self.total_duration(shots)
            if target and abs(total - float(target)) > 0.01:
                problems.append(f"分镜「{node.label}」时长合计 {total}s ≠ 目标 {target}s（差 {round(total - float(target), 2)}s）")
            missing_segments = sorted(set(self.segment_durations(shots)) - covered)
            if prompts and missing_segments:
                problems.append(f"分镜「{node.label}」的片段 {missing_segments} 还没有对应的提示词节点")
        return problems

    # ------------------------------------------------------------------
    # 分镜：结构化数据 → 文本，并派生片段时长
    # ------------------------------------------------------------------
    @staticmethod
    def total_duration(shots: list[dict[str, Any]]) -> float:
        return round(sum(_shot_seconds(s) for s in shots), 2)

    def segment_durations(self, shots: list[dict[str, Any]]) -> dict[int, float]:
        """每个片段占多少秒（片段的边界因此可计算，而不是靠模型写对时间范围）。"""
        out: dict[int, float] = {}
        for shot in shots:
            seg = int(shot.get("segment_no") or 1)
            out[seg] = round(out.get(seg, 0.0) + _shot_seconds(shot), 2)
        return dict(sorted(out.items()))

    @staticmethod
    def _shot_extra(shot: dict[str, Any], name_by_id: dict[str, str] | None = None) -> str:
        """新字段（camera/subjects/props/light/dialogue/focus/forbidden）的补行。"""
        names = name_by_id or {}

        def _label(item: dict[str, Any]) -> str:
            return str(item.get("name") or names.get(item.get("id") or "") or item.get("id") or "")

        bits: list[str] = []
        camera = shot.get("camera") if isinstance(shot.get("camera"), dict) else {}
        cam = "，".join(
            f"{label}{camera[key]}"
            for key, label in (("height", "机位高"), ("angle", "角度"), ("movement", "运镜"))
            if camera.get(key)
        )
        if cam:
            bits.append("机位：" + cam)
        subjects = shot.get("subjects")
        if isinstance(subjects, list) and subjects:
            rows = []
            for item in subjects:
                if not isinstance(item, dict):
                    continue
                attrs = [str(item[key]) for key in ("screen_pos", "start_state", "end_state") if item.get(key)]
                gaze = str(item.get("gaze") or "").strip()
                if gaze:
                    # 字段本身可能已经写成「看向对方喉咙」，不要渲染成「看向看向…」
                    prefix = "" if gaze.startswith(("看向", "望向", "盯着", "注视", "凝视")) else "看向"
                    attrs.insert(1 if item.get("screen_pos") else 0, prefix + gaze)
                label = _label(item)
                rows.append(f"{label}（{'；'.join(attrs)}）" if attrs else str(label))
            if rows:
                bits.append("主体：" + "；".join(rows))
        props = shot.get("props")
        if isinstance(props, list) and props:
            bits.append(
                "道具："
                + "；".join(
                    f"{_label(item)}（{item.get('state')}）"
                    for item in props
                    if isinstance(item, dict)
                )
            )
        light = shot.get("light") if isinstance(shot.get("light"), dict) else {}
        rules = "，".join(
            f"{label}{light[key]}"
            for key, label in (("direction", "方向"), ("color", "色温"), ("contrast", "对比"))
            if light.get(key)
        )
        if rules:
            bits.append("光线：" + rules)
        dialogue = shot.get("dialogue") if isinstance(shot.get("dialogue"), dict) else {}
        if str(dialogue.get("text") or "").strip():
            speaker = str(dialogue.get("speaker") or "").strip()
            who = name_by_id.get(speaker) or speaker
            # 说话人写进提示词，配音阶段才知道这句该用谁的嗓音
            bits.append(f"台词（{who}，同期声，原文一字不改）：{dialogue['text']}" if who else f"台词：{dialogue['text']}")
        if shot.get("focus"):
            bits.append("焦点：" + str(shot["focus"]))
        forbidden = shot.get("forbidden")
        if isinstance(forbidden, list) and forbidden:
            bits.append("禁止：" + "；".join(str(item) for item in forbidden))
        return "／".join(bits)

    def _render_shot_block(self, shot: dict[str, Any], name_by_id: dict[str, str] | None = None) -> str:
        """一镜一行；新字段挂在第二行。老字段照旧渲染，历史画布不会变样。"""
        parts: list[str] = []
        for key, label in (
            ("subject", "主体"),
            ("action", "动作"),
            ("shot_size", "景别"),
            ("camera_move", "运动"),
            ("sound", "声音"),
        ):
            value = shot.get(key)
            if value:
                parts.append(f"{label}：{value}")
        parts.append(f"时长：{_shot_seconds(shot):g}s")
        extra = self._shot_extra(shot, name_by_id)
        line = f"镜{shot.get('no', '?')}｜" + "｜".join(parts)
        return line + (f"\n  {extra}" if extra else "")

    def render_storyboard(self, node_id: str) -> str:
        """把结构化分镜渲染成可读文本（时间范围由时长累加算出来，不靠模型手写）。"""
        node = self.require(node_id)
        shots = list(node.data.get("shots") or [])
        if not shots:
            return ""
        name_by_id = {nid: str(n.label or "") for nid, n in self.nodes.items()}
        lines: list[str] = [f"# {node.label}"]
        if node.data.get("target_duration"):
            lines.append(f"目标总时长 {node.data['target_duration']}s｜实际 {self.total_duration(shots)}s")
        lines.append("")
        cursor = 0.0
        by_segment: dict[int, list[dict[str, Any]]] = {}
        for shot in shots:
            by_segment.setdefault(int(shot.get("segment_no") or 1), []).append(shot)
        for seg, seg_shots in sorted(by_segment.items()):
            seg_seconds = round(sum(_shot_seconds(s) for s in seg_shots), 2)
            lines.append(f"## 片段{seg}（{cursor:g}-{cursor + seg_seconds:g}s）")
            lines.append("")
            for shot in seg_shots:
                lines.append(self._render_shot_block(shot, name_by_id))
            lines.append("")
            cursor = round(cursor + seg_seconds, 2)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 展开提示词（P1 的核心：把 {{ref N}} 变成能直接投喂的文本）
    # ------------------------------------------------------------------
    def render_prompt(self, node_id: str) -> dict[str, Any]:
        """把一个片段提示词展开成最终文本。

        展开规则：
          · `{{ref N}}` → `【参考N】`，并在前言里写出 `【参考N】= 资产描述`
          · 资产描述取自**上游资产节点**（consistency_tags 是唯一出处）
          · 分镜文本（STRING 入边）作为"依据"附在后面，不占参考编号
          · 正文里出现了没有对应资产的编号 → 记进 leftovers，导出时明确告警
        """
        node = self.require(node_id)
        if node.type != "prompt":
            raise CanvasError(f"「{node.label}」不是提示词节点（type={node.type}）")

        refs = self.refs_for(node_id)
        body = self.read_text(node_id)
        header = [f"【参考{r['ref']}】= {self.asset_desc(r['node_id'])}" for r in refs]
        expanded = body
        for ref in refs:
            expanded = expanded.replace(f"{{{{ref {ref['ref']}}}}}", f"【参考{ref['ref']}】")
        leftovers = sorted(set(re.findall(r"\{\{ref \d+\}\}", expanded)))
        return {
            "node_id": node_id,
            "label": node.label,
            "segment_no": node.data.get("segment_no"),
            "duration": node.data.get("duration"),
            "refs": refs,
            "header": header,
            "body": expanded,
            "leftovers": leftovers,
            "contexts": self.text_inputs(node_id),
        }

    def render_all(self) -> str:
        """导出全片提示词（Markdown）：按片段号排，没有号就按拓扑序。"""
        prompts = [n for n in self.nodes.values() if n.type == "prompt"]
        if not prompts:
            raise CanvasError("画布里没有提示词节点（prompt），无法导出")
        order = {node_id: index for index, node_id in enumerate(self.topo_order())}
        prompts.sort(key=lambda n: (n.data.get("segment_no") if n.data.get("segment_no") is not None else 10**6,
                                   order.get(n.id, 10**6)))

        lines = [
            f"# 全片提示词 · 画布「{self.name}」",
            "",
            f"{len(prompts)} 个片段｜由 toonflow-py-lab 从画布导出（参考编号与资产节点一一对应）",
            "",
        ]
        for node in prompts:
            rendered = self.render_prompt(node.id)
            lines.append(f"## {rendered['label']}")
            meta = []
            if rendered["segment_no"] is not None:
                meta.append(f"片段号 {rendered['segment_no']}")
            if rendered["duration"]:
                meta.append(f"时长 {rendered['duration']}")
            if meta:
                lines += [f"*{'｜'.join(str(m) for m in meta)}*", ""]
            if rendered["header"]:
                lines += ["**参考（对应第 N 张参考图）**", *rendered["header"], ""]
            for ctx in rendered["contexts"]:
                lines += [f"**分镜依据**（来自「{ctx['label']}」）"]
                quoted = (ctx["text"] or "（空）").strip().splitlines()[:12]
                lines += [f"> {q}" for q in quoted] + [""]
            lines += ["**提示词**", "```text", rendered["body"].strip(), "```", ""]
            if rendered["leftovers"]:
                lines += [f"> ⚠️ 未展开的编号：{', '.join(rendered['leftovers'])}（画布里没有对应的资产连线）", ""]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 布局：按拓扑分层摆位（同一步在同一列，从左到右就是执行顺序）
    # ------------------------------------------------------------------
    # 间距要容得下**最高的节点**（生成节点带缩略图约 240px），否则会互相压住。
    ARRANGE_X_GAP = 380
    ARRANGE_Y_GAP = 360

    def arrange(self, x_gap: int | None = None, y_gap: int | None = None) -> int:
        """按拓扑序分层重排所有节点，返回被移动的节点数。"""
        x_gap = x_gap or self.ARRANGE_X_GAP
        y_gap = y_gap or self.ARRANGE_Y_GAP
        level: dict[str, int] = {node_id: 0 for node_id in self.nodes}
        for node_id in self.topo_order():
            for edge in self.outgoing(node_id):
                level[edge.target] = max(level.get(edge.target, 0), level[node_id] + 1)

        buckets: dict[int, list[str]] = {}
        for node_id, lv in level.items():
            buckets.setdefault(lv, []).append(node_id)

        type_order = {
            "text": 0, "storyboard": 1, "asset": 2, "image": 2, "video": 2, "audio": 2,
            "prompt": 3, "image_gen": 4, "video_gen": 5, "tts_gen": 5, "timeline": 6,
        }

        def visual_order(node_id: str) -> tuple[Any, ...]:
            node = self.nodes[node_id]
            segment = int(node.data.get("segment_no") or 10**6)
            return (type_order.get(node.type, 99), segment, node.label, node.id)

        moved = 0
        for lv, ids in sorted(buckets.items()):
            for index, node_id in enumerate(sorted(ids, key=visual_order)):
                node = self.nodes[node_id]
                new_position = {"x": 80.0 + lv * x_gap, "y": 80.0 + index * y_gap}
                if node.position != new_position:
                    node.position = new_position
                    moved += 1
        return moved

    # ------------------------------------------------------------------
    # 生成任务：按拓扑序把「提示词 → 生成节点」串起来
    # ------------------------------------------------------------------
    def generation_jobs(self) -> list[dict[str, Any]]:
        """按**拓扑序**列出生成节点，并解析每个节点的提示词与媒体参数。

        顺序很重要：上游没跑完就跑下游，产物会缺参考图 —— 这正是拓扑排序在业务上的意义。

        提示词遵循 ``resolve_prompt`` 的来源优先级（显式 prompt 节点 > 文本输入 > 卡片自身），
        参数遵循 ``generation_params`` 的解析链。两个字段都随 job 下发，前端与 Worker 不再各自猜。
        """
        jobs: list[dict[str, Any]] = []
        for node_id in self.topo_order():
            node = self.nodes[node_id]
            if node.type not in ("image_gen", "video_gen"):
                continue
            resolved = self.resolve_prompt(node_id)
            prompt_text = resolved["prompt"]
            jobs.append(
                {
                    "node_id": node.id,
                    "label": node.label,
                    "kind": "image" if node.type == "image_gen" else "video",
                    "prompt": prompt_text,
                    "prompt_chars": len(prompt_text),
                    "prompt_source": resolved["prompt_source"],
                    "prompt_overridden": resolved["prompt_overridden"],
                    "ready": bool(prompt_text.strip()),
                    "ready_reason": "" if prompt_text.strip() else "没有可用提示词（卡片内填写，或连接提示词/文本节点）",
                    "params": self.generation_params(node),
                    "refs": self.generation_refs(node_id),
                    "status": node.status,
                }
            )
        return jobs

    def timeline_jobs(self) -> list[dict[str, Any]]:
        """Resolve timeline dependencies without mutating the canvas.

        Edges express dependency.  ``clip_order`` is the authoritative playback
        order because edge insertion order is an implementation detail.
        """
        order_index = {node_id: index for index, node_id in enumerate(self.nodes)}
        jobs: list[dict[str, Any]] = []
        for node_id in self.topo_order():
            node = self.nodes[node_id]
            if node.type != "timeline":
                continue
            incoming = self.incoming(node_id)
            video_edges = [edge for edge in incoming if edge.target_handle == "video" and self.edge_type(edge) == "VIDEO"]
            requested = [str(item) for item in (node.data.get("clip_order") or [])]
            rank = {source_id: index for index, source_id in enumerate(requested)}
            video_edges.sort(key=lambda edge: (rank.get(edge.source, 10**9), order_index.get(edge.source, 10**9)))
            trims = node.data.get("trims") if isinstance(node.data.get("trims"), dict) else {}
            clips: list[dict[str, Any]] = []
            for edge in video_edges:
                source = self.nodes.get(edge.source)
                media = self.media_output(edge.source, "VIDEO")
                trim = trims.get(edge.source) if isinstance(trims.get(edge.source), dict) else {}
                clips.append(
                    {
                        "node_id": edge.source,
                        "label": source.label if source else edge.source,
                        "path": str(media.get("path") or ""),
                        "mime_type": media.get("mime_type") or media.get("mime") or "",
                        "trim_start": float(trim.get("start") or 0),
                        "trim_end": trim.get("end"),
                        "transition": "cut",
                    }
                )
            audio = None
            tts = None
            for edge in incoming:
                if edge.target_handle == "audio" and self.edge_type(edge) == "AUDIO":
                    media = self.media_output(edge.source, "AUDIO")
                    if media.get("path"):
                        audio = {"node_id": edge.source, "path": str(media["path"])}
                        break
                    source = self.nodes.get(edge.source)
                    if source and source.type == "tts_gen":
                        narration = str(source.data.get("text") or "").strip()
                        if not narration:
                            for text_input in self.text_inputs(source.id):
                                narration = str(text_input.get("text") or "").strip()
                                if narration:
                                    break
                        if narration:
                            tts = {
                                "node_id": source.id,
                                "text": narration,
                                "provider": str(source.data.get("provider") or "mock"),
                                "model": str(source.data.get("model") or "tts-1"),
                                "voice": str(source.data.get("voice") or "alloy"),
                                "speed": float(source.data.get("speed") or 1.0),
                            }
                            break
            subtitle_text = str(node.data.get("subtitle_text") or "").strip()
            if not subtitle_text:
                for edge in incoming:
                    if edge.target_handle == "subtitle" and self.edge_type(edge) == "STRING":
                        subtitle_text = self.read_text(edge.source).strip()
                        if subtitle_text:
                            break
            output = node.data.get("output") if isinstance(node.data.get("output"), dict) else {}
            jobs.append(
                {
                    "node_id": node.id,
                    "label": node.label,
                    "clips": clips,
                    "audio": audio,
                    "tts": tts,
                    # 逐句对白配音计划：合成时按镜头边界对齐成对白轨（见 CompositionService）
                    "dialogue": self._dialogue_plan(clips),
                    "subtitle_text": subtitle_text,
                    "output": {
                        "width": int(output.get("width") or 1280),
                        "height": int(output.get("height") or 720),
                        "fps": int(output.get("fps") or 24),
                    },
                    "ready": bool(clips) and all(clip["path"] for clip in clips),
                    "status": node.status,
                }
            )
        return jobs

    def _voice_for(self, speaker: str, shot: dict[str, Any] | None = None) -> tuple[str, bool]:
        """speaker（资产 id 或名字）→ (voice_id, 是否命中资产)。

        音色是「声音的参考图」：角色绑固定 voice_id，每句台词才是同一个嗓音。
        """
        if not speaker:
            return "default", False
        ids = [speaker]
        for subject in (shot or {}).get("subjects") or []:
            if isinstance(subject, dict) and speaker in (
                str(subject.get("id") or ""),
                str(subject.get("name") or ""),
            ):
                ids.insert(0, str(subject.get("id") or ""))
                break
        for cand in ids:
            node = self.nodes.get(cand)
            if node is not None and node.type == "asset":
                return str(node.data.get("voice_id") or "default"), True
        for node in self.nodes.values():
            if node.type != "asset":
                continue
            if speaker in (str(node.data.get("name") or ""), node.label):
                return str(node.data.get("voice_id") or "default"), True
        return "default", False

    def _dialogue_plan(self, clips: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """逐句对白配音计划：clip_index + 文本 + 音色。

        数据源 = 每段上游 prompt 节点经 ctx 边挂的分镜 shots[].dialogue。
        老画布没有 speaker 时回退「名字：台词」前缀（命中资产才采用并剥前缀，
        否则整句照读，避免把普通句子误当对白拆掉）。
        """
        out: list[dict[str, Any]] = []
        for index, clip in enumerate(clips):
            for edge in self.incoming(str(clip.get("node_id") or "")):
                if self.edge_type(edge) != "STRING" or edge.target_handle != "prompt":
                    continue
                prompt_node = self.nodes.get(edge.source)
                if prompt_node is None or prompt_node.type != "prompt":
                    continue
                for ctx_edge in self.incoming(prompt_node.id):
                    if ctx_edge.target_handle != "ctx":
                        continue
                    sb = self.nodes.get(ctx_edge.source)
                    if sb is None or sb.type != "storyboard":
                        continue
                    for shot in sb.data.get("shots") or []:
                        if not isinstance(shot, dict):
                            continue
                        entry = shot.get("dialogue")
                        entries = entry if isinstance(entry, list) else [entry]
                        for item in entries:
                            if not isinstance(item, dict):
                                continue
                            text = str(item.get("text") or "").strip()
                            if not text:
                                continue
                            speaker = str(item.get("speaker") or "").strip()
                            voice, found = self._voice_for(speaker, shot)
                            if not speaker:
                                match = re.match(r"^([^：:，,。]{1,12})\s*[：:]\s*(.+)$", text, re.DOTALL)
                                if match:
                                    cand = match.group(1).strip()
                                    cand_voice, cand_found = self._voice_for(cand, shot)
                                    if cand_found:
                                        speaker, voice, text = cand, cand_voice, match.group(2).strip()
                            out.append(
                                {
                                    "clip_index": index,
                                    "text": text,
                                    "speaker": speaker,
                                    "voice": voice if voice else "default",
                                    "speed": 1.0,
                                }
                            )
        return out

    # ------------------------------------------------------------------
    # 给模型看的紧凑视图 / 给前端看的完整视图
    # ------------------------------------------------------------------
    def summary(self) -> str:
        lines = [f"画布「{self.name}」：{len(self.nodes)} 个节点、{len(self.edges)} 条边"]
        lines.append("节点（id｜类型｜状态｜标签｜输出端口）：")
        for node in self.nodes.values():
            outs = ", ".join(f"{name}({kind})" for name, kind in node.out_ports.items()) or "无"
            line = f"- {node.id}｜{node.type}｜{node.status}｜{node.label}｜输出：{outs}"
            # 下游团队成员只拿紧凑画布摘要，不复制上游全文。正文路径必须出现在
            # 摘要里，否则分镜师只能看到“有一个剧本节点”，却无法用 read_file
            # 读取它，最终会退化成凭摘要写作或跳过 set_storyboard。
            content_path = str(node.data.get("content_path") or "").strip()
            if content_path:
                line += f"｜正文：{content_path}"
            if node.type == "storyboard" and isinstance(node.data.get("shots"), list):
                line += f"｜结构化镜头：{len(node.data['shots'])} 个"
            if node.type == "prompt":
                refs = self.refs_for(node.id)
                if refs:
                    pairs = ", ".join(f"{{{{ref {r['ref']}}}}}={r['label']}" for r in refs)
                    line += f"｜可用引用：{pairs}"
                else:
                    line += "｜⚠️ 还没有连任何资产（参考图）"
            elif node.type == "asset":
                # 把稳定特征也列出来：审校要核对"资产是否完整"，看不到内容就只能写"无法证实"
                tags = node.data.get("consistency_tags") or []
                kind = node.data.get("kind", "?")
                line += f"｜kind={kind}｜tags={'、'.join(str(t) for t in tags) if tags else '⚠️ 缺 consistency_tags'}"
            lines.append(line)
        if self.edges:
            lines.append("连线（上游端口 → 下游端口）：")
            for edge in self.edges:
                src = self.nodes.get(edge.source)
                dst = self.nodes.get(edge.target)
                src_label = f"{src.label}.{edge.source_handle}" if src else edge.source
                dst_label = f"{dst.label}.{edge.target_handle}" if dst else edge.target
                lines.append(f"- {src_label} → {dst_label}")
        try:
            order = self.topo_order()
            lines.append("可执行顺序（拓扑序）：" + " → ".join(self.nodes[n].label for n in order))
        except CanvasError as exc:
            lines.append(f"⚠️ {exc}")
        problems = self.validate()
        if problems:
            lines.append("体检问题：")
            lines.extend(f"- {p}" for p in problems)
        return "\n".join(lines)


def canvas_path(root: Path, name: str) -> Path:
    return root / f"{name}.canvas.json"
