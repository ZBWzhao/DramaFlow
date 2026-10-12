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
}

NODE_STATUS = ("draft", "ready", "generating", "done", "error")

# 分镜的 6 个字段（对齐 Toonflow storyboard 技能：主体/动作/景别/运动/声音/时长）
# 存成结构化数据（不是只有文本），"时长总和"才可能被代码判定。
SHOT_FIELDS = ("subject", "action", "shot_size", "camera_move", "sound", "duration")
SHOT_LABELS = {
    "subject": "主体",
    "action": "动作",
    "shot_size": "景别",
    "camera_move": "运动",
    "sound": "声音",
    "duration": "时长",
}


def _compatible(source_type: str, target_type: str) -> bool:
    """端口类型兼容：相等，或任一侧为通配 *（对应 Toonflow 的 isTypeCompatible）。"""
    return source_type == target_type or "*" in (source_type, target_type)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:6]}"


def story_similarity(left: str, right: str) -> float:
    """中文友好的轻量故事相似度；用于路由，不让模型凭画布名猜。

    采用字符二元组的包含系数，短标题（如“奶龙破土升天”）也能和较长的
    自然语言请求匹配；同时过滤常见创作套话，避免“帮我做一段视频”造成误判。
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
        os.replace(tmp, self.path)

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

    def update_node(self, node_id: str, **fields: Any) -> Node:
        node = self.require(node_id)
        if "status" in fields and fields["status"] not in NODE_STATUS:
            raise CanvasError(f"未知状态：{fields['status']}（可选：{', '.join(NODE_STATUS)}）")
        for key in ("label", "status", "position", "data", "outputs"):
            if key not in fields or fields[key] is None:
                continue
            if key in ("data", "outputs"):
                merged = dict(getattr(node, key))
                merged.update(fields[key])
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
        """读节点正文（存在 assets/<node_id>/content.md，画布 JSON 里只有路径）。"""
        node = self.nodes.get(node_id)
        if node is None:
            return ""
        relative = node.data.get("content_path")
        if not relative:
            value = (node.outputs.get("text") or {}).get("value")
            return str(value or "")
        path = self.root() / relative
        return path.read_text(encoding="utf-8") if path.exists() else ""

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
            # 有节点、连了线，但正文还没写 —— 导出时这一段会是空的（之前体检漏了这条）
            if node.type == "prompt" and not node.data.get("content_path"):
                problems.append(f"提示词节点「{node.label}」还没有正文（用 set_text 写入），导出会是空片段")
            if node.type == "prompt" and not node.data.get("segment_no"):
                problems.append(f"提示词节点「{node.label}」缺少 segment_no，无法和分镜片段稳定对应")
            if node.type == "prompt" and node.data.get("content_path"):
                prompt_chars = len(self.read_text(node.id).strip())
                if prompt_chars < 500:
                    problems.append(
                        f"提示词节点「{node.label}」正文仅 {prompt_chars} 字，可能只是摘要；"
                        "正式视频提示词应包含时间轴、站位、动作、运镜、灯光、声音、物理和约束"
                    )
            if node.type in ("image_gen", "video_gen"):
                prompt_inputs = [edge for edge in self.incoming(node.id) if edge.target_handle == "prompt"]
                if not prompt_inputs:
                    problems.append(f"生成节点「{node.label}」没有连接 prompt 输入，无法执行")
            if node.type == "asset" and node.data.get("kind") in ("character", "scene") and not node.data.get("consistency_tags"):
                problems.append(f"资产「{node.label}」缺少 consistency_tags（拼进提示词的稳定特征）")

        # 分镜：6 字段齐全 + 时长预算 + 片段覆盖率（这三条都是"可判定"的）
        prompts = [n for n in self.nodes.values() if n.type == "prompt"]
        covered = {int(p.data.get("segment_no") or 0) for p in prompts}
        for node in self.nodes.values():
            if node.type != "storyboard":
                continue
            shots = list(node.data.get("shots") or [])
            if not shots:
                problems.append(f"分镜「{node.label}」还没有结构化镜头数据（用 set_storyboard 写入）")
                continue
            for shot in shots:
                missing = [SHOT_LABELS[f] for f in SHOT_FIELDS if shot.get(f) in (None, "")]
                if missing:
                    problems.append(f"分镜「{node.label}」镜{shot.get('no', '?')} 缺字段：{'、'.join(missing)}")
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
        return round(sum(float(s.get("duration") or 0) for s in shots), 2)

    def segment_durations(self, shots: list[dict[str, Any]]) -> dict[int, float]:
        """每个片段占多少秒（片段的边界因此可计算，而不是靠模型写对时间范围）。"""
        out: dict[int, float] = {}
        for shot in shots:
            seg = int(shot.get("segment_no") or 1)
            out[seg] = round(out.get(seg, 0.0) + float(shot.get("duration") or 0), 2)
        return dict(sorted(out.items()))

    def render_storyboard(self, node_id: str) -> str:
        """把结构化分镜渲染成可读文本（时间范围由时长累加算出来，不靠模型手写）。"""
        node = self.require(node_id)
        shots = list(node.data.get("shots") or [])
        if not shots:
            return ""
        lines: list[str] = [f"# {node.label}"]
        if node.data.get("target_duration"):
            lines.append(f"目标总时长 {node.data['target_duration']}s｜实际 {self.total_duration(shots)}s")
        lines.append("")
        cursor = 0.0
        by_segment: dict[int, list[dict[str, Any]]] = {}
        for shot in shots:
            by_segment.setdefault(int(shot.get("segment_no") or 1), []).append(shot)
        for seg, seg_shots in sorted(by_segment.items()):
            seg_seconds = round(sum(float(s.get("duration") or 0) for s in seg_shots), 2)
            lines.append(f"## 片段{seg}（{cursor:g}-{cursor + seg_seconds:g}s）")
            lines.append("")
            for shot in seg_shots:
                parts = "｜".join(
                    f"{SHOT_LABELS[f]}：{shot.get(f, '')}" if f != "duration" else f"时长：{shot.get('duration')}s"
                    for f in SHOT_FIELDS
                )
                lines.append(f"镜{shot.get('no', '?')}｜{parts}")
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

        type_order = {"text": 0, "storyboard": 1, "asset": 2, "image": 2, "video": 2, "prompt": 3, "image_gen": 4, "video_gen": 5}

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
        """按**拓扑序**列出生成节点，并解析每个节点的输入提示词。

        顺序很重要：上游没跑完就跑下游，产物会缺参考图 —— 这正是拓扑排序在业务上的意义。
        """
        jobs: list[dict[str, Any]] = []
        for node_id in self.topo_order():
            node = self.nodes[node_id]
            if node.type not in ("image_gen", "video_gen"):
                continue
            prompt_text = ""
            for ctx in self.text_inputs(node_id):
                if ctx["text"]:
                    prompt_text = ctx["text"]  # 上游 prompt 节点的正文
                    break
            jobs.append(
                {
                    "node_id": node.id,
                    "label": node.label,
                    "kind": "image" if node.type == "image_gen" else "video",
                    "prompt": prompt_text,
                    "prompt_chars": len(prompt_text),
                    "refs": self.generation_refs(node_id),
                    "status": node.status,
                    "ready": bool(prompt_text.strip()),
                }
            )
        return jobs

    # ------------------------------------------------------------------
    # 给模型看的紧凑视图 / 给前端看的完整视图
    # ------------------------------------------------------------------
    def summary(self) -> str:
        lines = [f"画布「{self.name}」：{len(self.nodes)} 个节点、{len(self.edges)} 条边"]
        lines.append("节点（id｜类型｜状态｜标签｜输出端口）：")
        for node in self.nodes.values():
            outs = ", ".join(f"{name}({kind})" for name, kind in node.out_ports.items()) or "无"
            line = f"- {node.id}｜{node.type}｜{node.status}｜{node.label}｜输出：{outs}"
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
