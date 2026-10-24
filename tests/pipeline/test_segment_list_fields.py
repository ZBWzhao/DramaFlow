"""片段列表视图的「扫一眼就能选」字段：dialogue_hint（首条台词原文）。

制作页队列原来只有 段号/状态点/时长，制作者不点进去就不知道这一段讲什么。
缩略图（thumbnail/keyframe_path）和版本数（take_count）后端早就返回了，
缺的只有台词摘要——在服务端补一个 dialogue_hint，前端只渲染，不自己拼。
"""
import sys
from pathlib import Path

from core.canvas import Canvas

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.studio import _segment_view  # noqa: E402


def _canvas_with_shots(dialogues: list[str]) -> tuple[Canvas, object]:
    canvas = Canvas("t")
    shots = []
    for no, text in enumerate(dialogues, 1):
        shot = {
            "no": no,
            "duration_s": 5.0,
            "subject": f"镜{no}",
            "shot_size": "中景",
            "camera": {"height": "齐腰", "angle": "正面", "movement": "固定"},
        }
        if text:
            shot["dialogue"] = {"text": text}
        shots.append(shot)
    sb = canvas.add_node("storyboard", "分镜01", data={"episode_no": 1, "segment_no": 1, "shots": shots})
    prompt = canvas.add_node("prompt", "分镜01·开场", data={"episode_no": 1, "segment_no": 1})
    canvas.connect(sb.id, "shots", prompt.id, "ctx")
    return canvas, prompt


def test_list_view_exposes_first_dialogue_as_hint():
    canvas, prompt = _canvas_with_shots(["你终于回来了。", "我不该走的。"])
    view = _segment_view(canvas, prompt)
    assert view["dialogue_hint"] == "你终于回来了。", "台词提示取第一条，逐句台词是详情页的事"


def test_dialogue_hint_skips_empty_and_defaults_blank():
    canvas, prompt = _canvas_with_shots(["", "压低声音。"])
    assert _segment_view(canvas, prompt)["dialogue_hint"] == "压低声音。"
    canvas2, prompt2 = _canvas_with_shots(["", ""])
    assert _segment_view(canvas2, prompt2)["dialogue_hint"] == "", "无台词片段给空串，前端显示占位"
