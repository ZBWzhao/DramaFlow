"""画布医生：体检 + 把"环"精确定位到具体是哪几条边。

为什么需要它：`topo_order()` 报环时只给一串节点 id，
但真正要动手的是**删哪条边** —— 这个脚本就是回答这个问题的。

用法：
  python demo/canvas_doctor.py                      # 体检 + 列出构成环的边
  python demo/canvas_doctor.py --break-cycles       # 删掉"逆向边"打破环（会写盘）
  python demo/canvas_doctor.py --drop e_abc,e_def   # 精确删指定边
  python demo/canvas_doctor.py --name other         # 指定画布名

"逆向边"的判定：按**节点创建顺序**（正常流程是先建上游、后建下游），
若边的 source 比 target 晚创建，它就是可疑的那条。
这只是启发式 —— 所以脚本会把要删的边连标签一起打出来给你过目。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.canvas import Canvas  # noqa: E402

WORKSPACE = ROOT / "workspace"


def strongly_connected(canvas: Canvas) -> list[list[str]]:
    """环 = 强连通分量。算法在 core/canvas.py 里（Canvas.cycles），这里只是取名。"""
    return canvas.cycles()


def main() -> None:
    parser = argparse.ArgumentParser(description="画布体检与破环")
    parser.add_argument("--name", default="main", help="画布名（默认 main）")
    parser.add_argument("--drop", default="", help="要删除的边 id，逗号分隔")
    parser.add_argument("--break-cycles", action="store_true", help="自动删掉逆向边来打破环")
    args = parser.parse_args()

    canvas = Canvas.load(WORKSPACE / f"{args.name}.canvas.json", args.name)
    order = {node_id: index for index, node_id in enumerate(canvas.nodes)}
    print(f"画布 {canvas.name}：{len(canvas.nodes)} 节点 / {len(canvas.edges)} 边")

    problems = canvas.validate()
    print(f"体检：{'通过' if not problems else f'{len(problems)} 个问题'}")
    for item in problems:
        print(f"  · {item}")

    components = strongly_connected(canvas)
    if not components and not (args.drop or args.break_cycles):
        return

    suspect: list[str] = []
    for component in components:
        members = set(component)
        print(f"\n环（{len(component)} 个节点）：")
        for node_id in component:
            print(f"  · {node_id}  {canvas.nodes[node_id].label}")
        print("  环内的边：")
        for edge in canvas.edges:
            if edge.source in members and edge.target in members:
                src, dst = canvas.nodes[edge.source], canvas.nodes[edge.target]
                backwards = order.get(edge.source, 0) > order.get(edge.target, 0)
                mark = "  ← 逆向边（可疑）" if backwards else ""
                print(f"    {edge.id}  {src.label} ({edge.source_handle}) → {dst.label} ({edge.target_handle}){mark}")
                if backwards:
                    suspect.append(edge.id)
        if not suspect:
            # 全是顺向边：说明是"多条合法边凑成的环"，只能人工挑一条
            print("    （环内全是顺向边，无法自动判定，请用 --drop 指定要删的边）")

    targets = [e for e in args.drop.split(",") if e] or (suspect if args.break_cycles else [])
    if not targets:
        if suspect:
            print(f"\n建议删除：{', '.join(suspect)}（确认后加 --break-cycles 或 --drop）")
        return

    print("\n将删除：")
    for edge in canvas.edges:
        if edge.id in targets:
            src = canvas.nodes.get(edge.source)
            dst = canvas.nodes.get(edge.target)
            print(f"  {edge.id}  {src.label if src else '?'} → {dst.label if dst else '?'}")
    removed = canvas.remove_edges(targets)
    canvas.save()
    print(f"已删除 {removed} 条边，保存 {canvas.path}")

    problems = canvas.validate()
    print(f"复检：{'通过' if not problems else f'仍有 {len(problems)} 个问题'}")
    for item in problems:
        print(f"  · {item}")


if __name__ == "__main__":
    main()
