"""剧本识别、隔离与布局回归。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.canvas import Canvas, CanvasError, story_similarity


def main() -> None:
    milk = Canvas.load(ROOT / "workspace" / "main.canvas.json")
    rain = Canvas.load(ROOT / "workspace" / "rain-oath.canvas.json")
    query = "帮我做一段奶龙破土而出升上天堂的动画"
    same = story_similarity(query, milk.story_text())
    other = story_similarity(query, rain.story_text())
    assert same >= 0.34 and same > other, (same, other)

    test = Canvas("guard")
    test.ensure_story("剧本甲")
    source = test.add_node("text", "剧本甲")
    target = test.add_node("storyboard", "剧本乙")
    target.data["story_id"] = "story_b"
    try:
        test.connect(source.id, "text", target.id, "script")
    except CanvasError as exc:
        assert "跨剧本" in str(exc)
    else:
        raise AssertionError("跨剧本连线未被阻止")

    assert all(node.data.get("story_id") == milk.meta.get("story_id") for node in milk.nodes.values())
    assert all(node.data.get("story_id") == rain.meta.get("story_id") for node in rain.nodes.values())
    print(f"PASS story routing: same={same:.2f}, unrelated={other:.2f}, cross-story guard=ok")


if __name__ == "__main__":
    main()
