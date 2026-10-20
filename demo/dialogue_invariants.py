"""回归：dialogue.speaker 字段——渲染带说话人、lint 拦截查无此人、汇总保留 speaker。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.canvas import Canvas, Node
from core.studio_rules import dialogue_from_shots, lint_segment


def main() -> None:
    shots = [
        {
            "no": 1,
            "segment_no": 1,
            "duration_s": 5,
            "shot_size": "近景",
            "camera": {"height": "与胸口齐平", "angle": "左前侧 30°", "movement": "固定"},
            "subjects": [{"id": "a_lin", "name": "林越", "screen_pos": "画面左三分之一",
                          "gaze": "看向对方", "start_state": "站着", "end_state": "坐下了"}],
            "action": "@林越 反手把门关上",
            "dialogue": {"text": "你到底是谁。", "speaker": "a_lin", "duration_s": 2},
            "forbidden": ["不出现第三个人"],
        },
        {
            "no": 2,
            "segment_no": 1,
            "duration_s": 4,
            "shot_size": "特写",
            "camera": {"height": "与胸口齐平", "angle": "正面", "movement": "固定"},
            "subjects": [{"id": "a_lin", "name": "林越", "screen_pos": "画面中央",
                          "gaze": "看向画外", "start_state": "坐着", "end_state": "站起来"}],
            "action": "@林越 抬头",
            "dialogue": {"text": "我等了你十年。", "speaker": "查无此人", "duration_s": 2},
            "forbidden": ["不出现文字"],
        },
    ]

    # 1) 汇总保留 speaker
    lines = dialogue_from_shots(shots)
    assert [l["speaker"] for l in lines] == ["a_lin", "查无此人"], lines

    # 2) lint：说话人不在引用资产里 → error；speaker 命中 name_by_id → 无该 finding
    name_by_id = {"a_lin": "林越"}
    bad = lint_segment(duration_s=9, shots=shots, prompt="台词见分镜",
                       references=[{"asset_id": "a_lin", "ordinal": 1}], name_by_id=name_by_id)
    assert any(f["rule"] == "segment.dialogue.speaker_unknown" for f in bad["findings"]), bad["findings"]

    good_shots = [dict(shots[0])]
    good = lint_segment(duration_s=5, shots=good_shots, prompt="台词见分镜",
                        references=[{"asset_id": "a_lin", "ordinal": 1}], name_by_id=name_by_id)
    assert not any(f["rule"] == "segment.dialogue.speaker_unknown" for f in good["findings"])

    # speaker 也可以直接写资产名（与 name_by_id 的值匹配）
    named = [dict(shots[0], dialogue={"text": "你到底是谁。", "speaker": "林越", "duration_s": 2})]
    ok2 = lint_segment(duration_s=5, shots=named, prompt="台词见分镜",
                       references=[{"asset_id": "a_lin", "ordinal": 1}], name_by_id=name_by_id)
    assert not any(f["rule"] == "segment.dialogue.speaker_unknown" for f in ok2["findings"])

    # 3) 渲染：台词行带说话人
    with tempfile.TemporaryDirectory(prefix="dramaflow-dialogue-") as d:
        c = Canvas("t", Path(d) / "t.canvas.json")
        asset = Node(
            id="a_lin", type="asset", label="林越",
            data={"kind": "character", "name": "林越"}, status="ready",
        )
        c.nodes[asset.id] = asset
        sb = c.add_node("storyboard", "分镜", data={"shots": [dict(shots[0])], "segment_no": 1})
        text = c.render_storyboard(sb.id)
        assert "台词（林越，同期声，原文一字不改）：你到底是谁。" in text, text

    print("dialogue_invariants PASS")


if __name__ == "__main__":
    main()
