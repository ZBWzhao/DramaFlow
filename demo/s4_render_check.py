"""S4 验收：字段 → 服务端渲染 → data.prompt / rendered_by=server / 首帧 8 段式。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.canvas import Canvas
from server import studio

SHOT = {
    "no": 1,
    "segment_no": 1,
    "duration_s": 5,
    "shot_size": "近景",
    "camera": {"height": "与胸口齐平", "angle": "左前侧 30°", "movement": "固定"},
    "subjects": [
        {
            "id": "",
            "screen_pos": "画面左三分之一",
            "gaze": "看向对方喉咙",
            "start_state": "反手抵住",
            "end_state": "两人定格",
        }
    ],
    "props": [],
    "action": "反手把 @断裂钢筋 横着抵住 @深棕士兵 的喉咙",
    "focus": "手与喉咙的接触点",
    "light": {"direction": "右后方", "color": "暖橙 2700K", "contrast": "高"},
    "sound": "仅环境音：压抑的粗重呼吸",
    "dialogue": {"text": "别动。", "duration_s": 2},
    "forbidden": ["不出现第三个人"],
}

tmp = Path(tempfile.mkdtemp())
canvas = Canvas(name="s4-check", path=tmp / "s4.json")

asset = canvas.add_node("asset", "灰蓝士兵")
asset.data.update(
    {
        "kind": "character",
        "name": "灰蓝士兵",
        "anchors": ["左虎口旧疤"],
        "consistency_tags": ["深灰蓝土布作训服"],
        "forbidden": ["不出现现代军装"],
        "spec": "单角度正面全身，平视，中性表情",
    }
)
board = canvas.add_node("storyboard", "第1场分镜")
prompt = canvas.add_node("prompt", "片段1")
canvas.connect(board.id, "shots", prompt.id, "ctx")
canvas.connect(asset.id, "ref", prompt.id, "ref")

SHOT["subjects"][0]["id"] = asset.id
SHOT["props"] = [{"id": asset.id, "state": "螺纹段贴住皮肤"}]
board.data["shots"] = [SHOT]
board.data["segment_no"] = 1
prompt.data["segment_no"] = 1

written = studio._sync_rendered_prompts(canvas)
rendered = str(prompt.data.get("prompt") or "")
print("同步片段数:", written)
print("rendered_by:", prompt.data.get("rendered_by"))
checks = [
    ("正文含 @资产名", "@灰蓝士兵" in rendered),
    ("正文含站位", "画面左三分之一" in rendered),
    ("正文含视线", "看向对方喉咙" in rendered),
    ("正文含机位三字段", all(k in rendered for k in ("机位高度", "角度", "运镜"))),
    ("正文含光线", "暖橙 2700K" in rendered),
    ("正文含台词", "别动。" in rendered),
    ("正文含禁止项", "不出现第三个人" in rendered),
    ("正文不含三视图", "三视图" not in rendered),
]
for name, ok in checks:
    print(("PASS " if ok else "FAIL ") + name)

view = studio._segment_view(canvas, prompt, with_detail=True)
print("view.prompt 与 data.prompt 一致:", view["prompt"] == rendered)
print("view.duration_s:", view["duration_s"], view["duration_source"])
board_text = canvas.render_storyboard(board.id)
print("--- 分镜渲染 ---")
print(board_text)
checks += [
    ("分镜补行用资产名而非 node id", "灰蓝士兵（画面左三分之一" in board_text),
    ("gaze 不重复前缀", "看向看向" not in board_text and "看向对方喉咙" in board_text),
]

# 人改过的正文不能被覆盖
prompt.data["rendered_by"] = "human"
prompt.data["prompt"] = "人工改过的正文"
studio._sync_rendered_prompts(canvas)
print("人改正文未被覆盖:", prompt.data["prompt"] == "人工改过的正文")

raise SystemExit(0 if all(ok for _, ok in checks) else 1)
