"""一次性迁移：把历史 main 里混放的独立故事拆成单故事画布。

原文件会备份到 workspace/backups；重复执行是幂等的。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.canvas import Canvas, Edge, Node


WORKSPACE = ROOT / "workspace"
MAIN = WORKSPACE / "main.canvas.json"


def components(canvas: Canvas) -> list[set[str]]:
    adjacency = {node_id: set() for node_id in canvas.nodes}
    for edge in canvas.edges:
        if edge.source in adjacency and edge.target in adjacency:
            adjacency[edge.source].add(edge.target)
            adjacency[edge.target].add(edge.source)
    result: list[set[str]] = []
    pending = set(adjacency)
    while pending:
        start = next(iter(pending))
        group, stack = set(), [start]
        while stack:
            node_id = stack.pop()
            if node_id in group:
                continue
            group.add(node_id)
            stack.extend(adjacency[node_id] - group)
        pending -= group
        result.append(group)
    return sorted(result, key=len, reverse=True)


def subset(source: Canvas, node_ids: set[str], name: str, path: Path, title: str, summary: str) -> Canvas:
    target = Canvas(name, path)
    target.nodes = {node_id: Node.from_dict(source.nodes[node_id].to_dict()) for node_id in node_ids}
    target.edges = [
        Edge.from_dict(edge.to_dict())
        for edge in source.edges
        if edge.source in node_ids and edge.target in node_ids
    ]
    target.ensure_story(title, summary)
    target.arrange()
    target.save()
    return target


def main() -> None:
    source = Canvas.load(MAIN, "main")
    if source.meta.get("story_id"):
        print("main 已经是单故事画布，无需迁移")
        return
    groups = components(source)
    if len(groups) < 2:
        print("没有发现多个互不相连的故事，无需迁移")
        return

    backup_dir = WORKSPACE / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / "main-mixed-before-story-split.canvas.json"
    if not backup.exists():
        shutil.copy2(MAIN, backup)

    milk_group = next(
        (group for group in groups if any("奶龙" in source.nodes[node_id].label for node_id in group)),
        groups[-1],
    )
    other_groups = [group for group in groups if group is not milk_group]
    rain_group = set().union(*other_groups)

    subset(
        source,
        rain_group,
        "rain-oath",
        WORKSPACE / "rain-oath.canvas.json",
        "雨夜小巷·黑衣男人立誓",
        "黑衣男人在雨夜小巷下跪、砸地怒吼并仰天立誓。",
    )
    subset(
        source,
        milk_group,
        "main",
        MAIN,
        "奶龙破土升天",
        "奶龙从泥土中破土而出并升上天堂云海。",
    )
    print(f"已拆分：main={len(milk_group)} 节点，rain-oath={len(rain_group)} 节点；备份={backup}")


if __name__ == "__main__":
    main()
