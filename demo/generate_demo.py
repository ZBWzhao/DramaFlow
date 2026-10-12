"""P2 验收：把画布上的提示词真的"跑"出产物（当前用 mock provider）。

跑法：
    python demo/generate_demo.py                # 默认 team-demo 画布
    python demo/generate_demo.py main           # 指定画布

它做三件事：
  1. 给每个 prompt 节点补一个 image_gen 节点并连线（提示词 + 参考图）
  2. 用 run_canvas 按**拓扑序**执行生成（上游没完成不会先跑下游）
  3. 检查产物文件、节点状态、体检结果（幂等：重复跑不会重复建节点）

注意：mock provider 产出的 PNG 是**可复现的占位图**（配色由提示词决定），
不是 AI 作图；接真实 provider 只需替换 ToolContext.media，工具与画布代码不用动。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from core.canvas import Canvas  # noqa: E402
from core.loader import create_agent_tools  # noqa: E402

WORKSPACE = ROOT / "workspace"


async def main() -> None:
    name = next((a for a in sys.argv[1:] if not a.startswith("--")), "team-demo")
    tools, _, ctx = await create_agent_tools(cwd=str(WORKSPACE))
    by_name = {t.name: t for t in tools}
    print(f"画布工具就绪：{[n for n in by_name if n in ('get_canvas', 'add_node', 'connect_nodes', 'run_canvas', 'generate_media')]}")
    print(f"能力位：{ctx.capabilities()}")

    canvas = Canvas.load(WORKSPACE / f"{name}.canvas.json", name)
    if not canvas.nodes:
        print(f"✗ 画布「{name}」是空的")
        return

    # ---------- 1. 补生成节点 ----------
    print("\n--- 1. 为每个片段建生成节点 ---")
    for prompt_node in [n for n in canvas.nodes.values() if n.type == "prompt"]:
        existing = [
            n
            for n in canvas.nodes.values()
            if n.type == "image_gen"
            and any(e.source == prompt_node.id and e.target == n.id for e in canvas.edges)
        ]
        if existing:
            print(f"  · {prompt_node.label} 已有生成节点 {existing[0].label}，跳过")
            continue
        gen = canvas.add_node(
            "image_gen",
            f"生成·{prompt_node.label}",
            data={"segment_no": prompt_node.data.get("segment_no"), "model": "mock-image-v1", "size": "1024x576"},
        )
        canvas.connect(prompt_node.id, "prompt", gen.id, "prompt")  # 提示词 → 生成
        # 资产参考图也接过去：同一资产 → 同一个生成节点，编号保持一致
        for edge in canvas.incoming(prompt_node.id):
            if canvas.edge_type(edge) == "IMAGE":
                canvas.connect(edge.source, edge.source_handle, gen.id, "ref")
        canvas.save()
        print(f"  · 建 {gen.label}（{gen.id}）并连线：提示词 + 参考图")

    # ---------- 2. 按拓扑序执行 ----------
    jobs = canvas.generation_jobs()
    print(f"\n--- 2. 拓扑序执行 {len(jobs)} 个生成节点 ---")
    for index, job in enumerate(jobs, 1):
        print(f"  {index}. {job['label']}（{job['kind']}｜提示词 {job['prompt_chars']} 字｜参考 {len(job['refs'])} 张｜状态 {job['status']}）")

    result = await by_name["run_canvas"].execute({"canvas": name}, ctx)
    print("\n" + result.content)

    # ---------- 3. 验收 ----------
    print(f"\n{'=' * 60}\nP2 门禁\n{'=' * 60}")
    canvas = Canvas.load(WORKSPACE / f"{name}.canvas.json", name)
    generated = [
        n for n in canvas.nodes.values() if n.type in ("image_gen", "video_gen") and n.outputs
    ]
    artifacts = []
    for node in generated:
        for port, output in node.outputs.items():
            path = (output.get("value") or {}).get("path")
            if path:
                file = WORKSPACE / path
                artifacts.append((node.label, path, file.exists(), file.stat().st_size if file.exists() else 0))

    checks = [
        (f"生成节点数 ≥ 1（实际 {len(generated)}）", len(generated) >= 1),
        (f"全部节点状态为 done（{sorted({n.status for n in generated})}）", all(n.status == "done" for n in generated)),
        (f"产物文件都存在（{len(artifacts)} 个）", all(exists for _, _, exists, _ in artifacts)),
        ("产物是有效 PNG（>1KB）", all(size > 1024 for *_, size in artifacts)),
    ]
    for label, ok in checks:
        print(f"  {'✓' if ok else '✗'} {label}")
    print("\n产物清单：")
    for label, path, exists, size in artifacts:
        print(f"  {'✓' if exists else '✗'} {label} → {path}（{size // 1024} KB）")

    problems = canvas.validate()
    print(f"\n体检：{'✓ 无问题' if not problems else f'{len(problems)} 个问题'}")
    for problem in problems:
        print(f"  - {problem}")
    print("\n前端查看：http://127.0.0.1:8000/#canvas → 选画布 → 节点上会显示缩略图")


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
