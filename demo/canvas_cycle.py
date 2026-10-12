"""连线成环的防护与定位（纯本地，不调模型）。

由来：体检曾报"存在环"并列出 **8 个节点**，但真正的环只有 **3 个** ——
其余 5 个只是**依赖了环**。照着那串 id 去删边等于猜，所以这一版做了三件事：

  1. `Canvas.connect()` **在写入前**拒绝会成环的边，并给出环的路径
  2. 体检/拓扑排序的消息只报**环内节点**，并说明有多少节点是"被拖累"的
  3. `Canvas.cycles()` 把环精确算出来（破环工具 `demo/canvas_doctor.py` 用它定位到具体哪条边）

跑法：python demo/canvas_cycle.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.canvas import Canvas, CanvasError, Edge  # noqa: E402

WORKSPACE = ROOT / "workspace"


def main() -> None:
    path = WORKSPACE / "cycle-demo.canvas.json"
    canvas = Canvas(name="cycle-demo", path=path)

    # 一条正常的流水线：剧本正文 → 分镜 → 片段提示词
    script = canvas.add_node("text", "剧本正文")
    board = canvas.add_node("storyboard", "第1场分镜")
    prompt = canvas.add_node("prompt", "片段1·推门进店")
    canvas.connect(script.id, "text", board.id, "script")
    canvas.connect(board.id, "shots", prompt.id, "ctx")
    canvas.save()
    print("建好流水线：剧本正文 → 第1场分镜 → 片段1·推门进店")
    print("拓扑序：", [canvas.nodes[i].label for i in canvas.topo_order()])

    # ① 写入前拦截：把下游的输出接回上游 —— 这正是曾经真的发生过的误操作
    print("\n[1] 尝试把「片段1·推门进店」的输出接回「剧本正文」（会成环）")
    try:
        canvas.connect(prompt.id, "prompt", script.id, "in")
        print("    ✗ 竟然通过了（防护失效）")
    except CanvasError as exc:
        print(f"    ✓ 被拒绝：{exc}")

    # ② 手改 JSON 造出的环，体检也要能精确定位
    print("\n[2] 绕过防护直接塞一条边（模拟手改 JSON），再看体检怎么说")
    canvas.edges.append(
        Edge(id="e_manual", source=prompt.id, source_handle="prompt", target=script.id, target_handle="in")
    )
    for problem in canvas.validate():
        print(f"    · {problem}")
    groups = canvas.cycles()
    for group in groups:
        print(f"    cycles() 精确给出环：{[canvas.nodes[i].label for i in group]}")

    # ③ 删掉那条边就恢复可排序
    print("\n[3] 删掉那条边")
    removed = canvas.remove_edges(["e_manual"])
    print(f"    删除 {removed} 条｜体检：{canvas.validate() or '通过'}")
    print(f"    拓扑序：{[canvas.nodes[i].label for i in canvas.topo_order()]}")

    path.unlink(missing_ok=True)  # 演示用的临时画布，用完删掉
    print(f"\n（临时画布已清理：{path.name}）")


if __name__ == "__main__":
    main()
