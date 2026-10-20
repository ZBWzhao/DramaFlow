"""回归：produce 幂等化——二次策划按（集+段号）归并，takes 保留，孤儿只标记不删除。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.canvas import Canvas, Node
from server.studio import _reconcile_produce


def _segment_set(c: Canvas, tag: str, episode: int, seg_no: int, takes: int = 0) -> None:
    """建一套 storyboard + prompt + video_gen（video_gen 可带 takes）。"""
    sb = Node(
        id=f"sb_{tag}", type="storyboard", label=f"分镜{tag}",
        data={"episode_no": episode, "segment_no": seg_no,
              "shots": [{"no": 1, "duration_s": 4, "action": "动作"}]},
        status="ready",
    )
    pr = Node(
        id=f"pr_{tag}", type="prompt", label=f"片段{tag}",
        data={"episode_no": episode, "segment_no": seg_no, "duration_s": 4},
        status="ready",
    )
    gen = Node(id=f"vg_{tag}", type="video_gen", label=f"生成槽{tag}", status="done" if takes else "draft")
    if takes:
        gen.data["takes"] = [{"take_id": f"take_{tag}", "path": f"{tag}.mp4"}]
        gen.data["current_take_id"] = f"take_{tag}"
        gen.outputs["video"] = {"data_type": "VIDEO", "value": {"path": f"{tag}.mp4", "mime": "video/mp4"}}
    for node in (sb, pr, gen):
        c.nodes[node.id] = node
    c.connect(sb.id, "shots", pr.id, "ctx")
    c.connect(pr.id, "prompt", gen.id, "prompt")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="dramaflow-upsert-") as d:
        c = Canvas("t", Path(d) / "t.canvas.json")

        # 第一次 produce：第 1 集段 1/2（旧套，段 1 有 take）
        _segment_set(c, "old1", 1, 1, takes=1)
        _segment_set(c, "old2", 1, 2)
        # 第二次 produce：段 1/2/3（新套）
        _segment_set(c, "new1", 1, 1)
        _segment_set(c, "new2", 1, 2)
        _segment_set(c, "new3", 1, 3)
        # 重复资产：同名角色建了两次
        a1 = Node(id="as_1", type="asset", label="角色·林越", data={"kind": "character", "name": "林越"}, status="ready")
        a2 = Node(id="as_2", type="asset", label="角色·林越", data={"kind": "character", "name": "林越"}, status="ready")
        c.nodes[a1.id] = a1
        c.nodes[a2.id] = a2

        summary = _reconcile_produce(c, 1)
        prompts = [n for n in c.nodes.values() if n.type == "prompt" and not n.data.get("orphaned")]
        assert len(prompts) == 3, f"应保留 3 套片段，实际 {len(prompts)}"
        seg1 = next(n for n in prompts if n.data.get("segment_no") == 1)
        assert seg1.id == "pr_old1", "段 1 应保留有 take 的旧套"
        orphaned = set(summary["orphaned"])
        assert "pr_new1" in orphaned and "pr_old2" in orphaned, summary
        assert "pr_new2" not in orphaned, "段 2 两套都无 take，保留建图最新的新套"
        assert "pr_new3" not in orphaned, "新段 3 应保留"
        # 保留套的生成槽与 take 原样
        kept_gen = c.nodes["vg_old1"]
        assert kept_gen.data.get("current_take_id") == "take_old1", "保留套的 take 不能丢"
        # 分镜去重：段 1 保留最新的 new1 分镜，old1 分镜标 orphaned
        assert "sb_old1" in orphaned and "sb_new1" not in orphaned, summary
        # 资产去重：两套都无形象图，保留建图最新的 as_2，as_1 被标记
        assert "as_1" in orphaned and "as_2" not in orphaned, summary
        assert summary["merged_assets"] == 1, summary
        # 只标记不删除
        assert all(nid in c.nodes for nid in orphaned), "孤儿节点必须保留在画布上"
        # 第 2 集不受影响
        _segment_set(c, "ep2", 2, 1)
        _reconcile_produce(c, 2)
        assert not c.nodes["pr_ep2"].data.get("orphaned"), "其他集不应被波及"

    print("produce_upsert_invariants PASS")


if __name__ == "__main__":
    main()
