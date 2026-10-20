"""回归：take 采用规则（首次自动采用，重生成不抢占）+ 30 版上限 + stale 指纹。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.canvas import Canvas


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="dramaflow-takes-") as d:
        c = Canvas("t", Path(d) / "t.canvas.json")
        g = c.add_node("video_gen", "生成槽")

        # 1) 首次生成：自动采用
        t1 = c.record_generated_media(
            g.id, kind="video", path="a.mp4", mime="video/mp4", provider="mock",
            shot_no=1, mode="first_frame",
        )
        assert c.require(g.id).data["current_take_id"] == t1["take_id"], "首次生成必须自动采用"

        # 2) 重生成：不抢占已采用的版本，但必须进历史
        t2 = c.record_generated_media(
            g.id, kind="video", path="b.mp4", mime="video/mp4", provider="mock",
            shot_no=1, mode="first_frame",
        )
        node = c.require(g.id)
        assert node.data["current_take_id"] == t1["take_id"], "重生成不得抢占用户已采用的版本"
        assert [t["take_id"] for t in node.data["takes"]] == [t1["take_id"], t2["take_id"]], "新 take 必须进历史"
        assert node.outputs["video"]["value"]["path"] == "a.mp4", "outputs 保持已采用版本"

        # 3) 显式采用 t2（走画布选版端点同款语义：把选中版拷回 outputs + current_take_id）
        take_row = next(t for t in node.data["takes"] if t["take_id"] == t2["take_id"])
        node.data["current_take_id"] = t2["take_id"]
        node.outputs["video"]["value"]["path"] = take_row["path"]
        t3 = c.record_generated_media(g.id, kind="video", path="c.mp4", mime="video/mp4", provider="mock")
        assert c.require(g.id).data["current_take_id"] == t2["take_id"], "已有选择后第三次生成同样不抢占"

        # 4) 历史上限 30 版
        for i in range(40):
            c.record_generated_media(g.id, kind="video", path=f"v{i}.mp4", mime="video/mp4", provider="mock")
        assert len(c.require(g.id).data["takes"]) <= 30, "历史上限 30 版"

        # 5) 指针丢失的旧画布（有历史但无 current_take_id）：新写入要兜底接管
        c.require(g.id).data.pop("current_take_id")
        t5 = c.record_generated_media(g.id, kind="video", path="fix.mp4", mime="video/mp4", provider="mock")
        assert c.require(g.id).data["current_take_id"] == t5["take_id"], "指针丢失时新 take 应兜底接管"

    # 6) stale 指纹：纯函数行为
    from core.studio_rules import refs_hash, shots_hash, stable_hash

    shots_a = [{"no": 1, "duration_s": 4, "action": "A 抬手"}]
    shots_b = [{"no": 1, "duration_s": 5, "action": "A 抬手"}]
    assert shots_hash(shots_a) != shots_hash(shots_b), "字段变化必须换指纹"
    assert shots_hash(shots_a) == shots_hash(list(reversed(shots_a))), "同内容指纹稳定"
    r1 = refs_hash([{"asset_id": "a1", "active_look_id": "L1", "image_path": "p.png"}])
    r2 = refs_hash([{"asset_id": "a1", "active_look_id": "L1", "image_path": "q.png"}])
    assert r1 != r2, "参考图变化必须换指纹"
    assert stable_hash({"b": 1, "a": 2}) == stable_hash({"a": 2, "b": 1}), "键序无关"
    assert refs_hash([]) == "" and shots_hash([]) == "", "空输入返回空串（无指纹语义）"

    print("takes_invariants PASS")


if __name__ == "__main__":
    main()
