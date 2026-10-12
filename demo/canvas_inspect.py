"""画布体检报告：一眼看清一张画布的质量（不调模型、不花钱）。

跑法：
    python demo/canvas_inspect.py                  # 默认 main 画布
    python demo/canvas_inspect.py team-demo        # 指定画布
    python demo/canvas_inspect.py --all            # 列出所有画布并逐个体检

看点：拓扑序、体检问题、资产 consistency_tags、每个片段的参考编号与未展开编号。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.canvas import Canvas, CanvasError  # noqa: E402

WORKSPACE = ROOT / "workspace"


def inspect(name: str) -> bool:
    """返回这张画布是否达标。"""
    path = WORKSPACE / f"{name}.canvas.json"
    if not path.exists():
        print(f"✗ 找不到画布：{path}")
        return False
    canvas = Canvas.load(path, name)
    print(f"\n{'=' * 64}\n画布「{name}」｜{len(canvas.nodes)} 节点 · {len(canvas.edges)} 连线\n{'=' * 64}")

    kinds: dict[str, int] = {}
    for node in canvas.nodes.values():
        kinds[node.type] = kinds.get(node.type, 0) + 1
    print(f"类型分布：{kinds}")

    try:
        order = canvas.topo_order()
        print("可执行顺序：" + " → ".join(canvas.nodes[i].label for i in order))
    except CanvasError as exc:
        print(f"✗ {exc}")
        return False

    for node in canvas.nodes.values():
        if node.type == "storyboard" and node.data.get("shots"):
            shots = node.data["shots"]
            print(
                f"\n分镜「{node.label}」：{len(shots)} 镜｜合计 {canvas.total_duration(shots)}s"
                f"｜目标 {node.data.get('target_duration', '—')}s"
                f"｜片段 {canvas.segment_durations(shots)}"
            )
        if node.type == "asset":
            print(f"  资产 {node.label}｜kind={node.data.get('kind', '?')}｜tags={node.data.get('consistency_tags')}")

    prompts = [n for n in canvas.nodes.values() if n.type == "prompt"]
    if prompts:
        print("\n片段提示词：")
        for node in prompts:
            rendered = canvas.render_prompt(node.id)
            refs = ", ".join(f"{{{{ref {r['ref']}}}}}={r['label']}" for r in rendered["refs"]) or "（无参考图）"
            flag = f"  ⚠️ 未展开 {rendered['leftovers']}" if rendered["leftovers"] else ""
            print(f"  {node.label}｜片段号 {rendered['segment_no']}｜{refs}{flag}")

    problems = canvas.validate()
    print(f"\n体检：{'✓ 无问题' if not problems else f'✗ {len(problems)} 个问题'}")
    for problem in problems:
        print(f"  - {problem}")
    return not problems


def main() -> None:
    args = sys.argv[1:]
    if "--all" in args:
        names = sorted(p.name[: -len(".canvas.json")] for p in WORKSPACE.glob("*.canvas.json"))
    else:
        names = [a for a in args if not a.startswith("--")] or ["main"]
    results = {name: inspect(name) for name in names}
    if len(results) > 1:
        print(f"\n{'=' * 64}\n汇总")
        for name, ok in results.items():
            print(f"  {'✓' if ok else '✗'} {name}")


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
