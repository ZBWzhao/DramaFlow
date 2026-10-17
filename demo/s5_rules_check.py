"""S5 验收：校验器按字段校验（check_shots / check_asset / check_references 闭包）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import studio_rules
from core.canvas import Canvas

GOOD = {
    "no": 1,
    "segment_no": 1,
    "duration_s": 5,
    "shot_size": "近景",
    "camera": {"height": "与胸口齐平", "angle": "左前侧 30°", "movement": "固定"},
    "subjects": [
        {"id": "n_a", "screen_pos": "画面左三分之一", "gaze": "看向对方喉咙", "start_state": "抵住", "end_state": "定格"},
        {"id": "n_b", "screen_pos": "画面右三分之一", "gaze": "垂眼看向地面", "start_state": "僵住", "end_state": "后仰"},
    ],
    "props": [{"id": "n_p", "state": "螺纹段贴住皮肤"}],
    "action": "反手把 @钢筋 横着抵住 @深棕士兵 的喉咙",
    "focus": "手与喉咙的接触点",
    "light": {"direction": "右后方", "color": "暖橙 2700K", "contrast": "高"},
    "sound": "仅环境音：压抑的粗重呼吸",
    "dialogue": {"text": "别动。", "duration_s": 2},
    "forbidden": ["不出现第三个人"],
}
NAMES = {"n_a": "灰蓝士兵", "n_b": "深棕士兵", "n_p": "钢筋"}
REFS = [
    {"asset_id": "n_a", "ordinal": 1},
    {"asset_id": "n_b", "ordinal": 2},
    {"asset_id": "n_p", "ordinal": 3},
]

failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def rules_of(findings, severity=None):
    return [f["rule"] for f in findings if severity is None or f["severity"] == severity]


def clone(**patch):
    shot = {**GOOD}
    shot.update(patch)
    return shot


# 1) 合法镜头
findings = studio_rules.check_shots([GOOD], name_by_id=NAMES)
check("合法镜头无 error", not rules_of(findings, "error"), str(findings))

# 2) 故意少填 gaze（同镜 2 人）
broken = clone(subjects=[dict(GOOD["subjects"][0]), {k: v for k, v in GOOD["subjects"][1].items() if k != "gaze"}])
findings = studio_rules.check_shots([broken], name_by_id=NAMES)
check("少填 gaze 报错", any("gaze" in f["message"] for f in findings if f["severity"] == "error"), str(findings))

# 3) 其他字段缺失/越界
cases = {
    "时长 10s 报错": (clone(duration_s=10), "3–8s"),
    "时长 2s 报错": (clone(duration_s=2), "3–8s"),
    "景别不在枚举": (clone(shot_size="超近景"), "不在枚举"),
    "机位缺运镜": (clone(camera={"height": "齐胸", "angle": "45°"}), "运镜"),
    "subjects 为空": (clone(subjects=[]), "subjects 为空"),
    "action 引用未声明资产": (clone(action="把 @钢筋 递给 @路人甲"), "@路人甲"),
}
for label, (shot, needle) in cases.items():
    findings = studio_rules.check_shots([shot], name_by_id=NAMES)
    check(label, any(needle in f["message"] and f["severity"] == "error" for f in findings), str(findings))

findings = studio_rules.check_shots([clone(forbidden=[])], name_by_id=NAMES)
check("forbidden 为空只警告", [f["severity"] for f in findings] == ["warn"], str(findings))

# 4) 资产字段
asset = {
    "kind": "character",
    "name": "灰蓝士兵",
    "anchors": ["左虎口旧疤"],
    "consistency_tags": ["深灰蓝土布作训服"],
    "forbidden": ["不出现现代军装"],
    "spec": "单角度正面全身，平视，中性表情",
}
check("合法资产无 error", not studio_rules.check_asset(asset), str(studio_rules.check_asset(asset)))
bad_asset = {**asset, "kind": "", "anchors": [], "spec": "生成三视图并排"}
messages = [f["message"] for f in studio_rules.check_asset(bad_asset)]
check("资产缺 kind/anchors 报错", any("kind" in m for m in messages) and any("anchors" in m for m in messages), str(messages))
check("spec 要三视图报错", any("三视图" in m for m in messages), str(messages))

# 5) 引用闭包（对照入边集合，不再到正文猜纯文本）
prompt = "反手把 @灰蓝士兵 的 @钢筋 横着抵住 @深棕士兵 的喉咙"
findings = studio_rules.check_references(prompt, REFS, name_by_id=NAMES)
check("入边齐全时无 dangling", not rules_of(findings, "error"), str(findings))
findings = studio_rules.check_references(prompt + "，画面外站着 @路人甲", REFS, name_by_id=NAMES)
check("@入边外的名字报错", "refs.dangling_mention" in rules_of(findings, "error"), str(findings))
findings = studio_rules.check_references("只写 @灰蓝士兵", REFS, name_by_id=NAMES)
check("未 @ 的入边只提示", all(f["severity"] == "info" for f in findings), str(findings))
findings = studio_rules.check_references("灰蓝士兵 站在左侧", REFS, name_by_id=NAMES)
check("纯文本同名不再算引用", len(rules_of(findings, "info")) == 3, str(findings))

# 6) 旧文本时间轴校验已移除
check("check_prompt_timeline 已移除", not hasattr(studio_rules, "check_prompt_timeline"))
check("parse_timeline 已移除", not hasattr(studio_rules, "parse_timeline"))

# 7) 台词与时长
lines = studio_rules.dialogue_from_shots([GOOD])
check("dialogue 字段可解析", lines == [{"speaker": "", "line": "别动。", "kind": "dialogue"}], str(lines))
findings = studio_rules.check_segment_duration(4, [{"speaker": "林越", "line": "一二三四五六七八九十一二三四五六七八九十", "kind": "dialogue"}])
check("台词装不下报错", "segment.duration.speech_overflow" in rules_of(findings, "error"), str(findings))
findings = studio_rules.check_segment_duration(10, [])
check("片段 >8s 报错", "segment.duration.too_long" in rules_of(findings, "error"), str(findings))
check("旧 sound 台词仍可解析", studio_rules.parse_dialogue("同期对白 林越「303 的，夜宵到了。」")[0]["line"] == "303 的，夜宵到了。")

# 8) lint_segment 汇总结构
result = studio_rules.lint_segment(
    duration_s=5,
    shots=[GOOD],
    prompt=prompt,
    references=REFS,
    name_by_id=NAMES,
)
check("lint_segment 不阻断", result["blocking"] is False, str(result["findings"]))
check("lint_segment 关键字段齐全", {"severity", "blocking", "findings", "dialogue", "shots_sum", "shot_count", "mentions", "reference_count"} <= set(result))
check("shot_count/shots_sum 正确", result["shot_count"] == 1 and result["shots_sum"] == 5.0, str(result))
check("mentions 抽取", set(result["mentions"]) == {"灰蓝士兵", "钢筋", "深棕士兵"}, str(result["mentions"]))

# 9) 引擎体检与业务校验同一口径
canvas = Canvas(name="s5-check")
asset_node = canvas.add_node("asset", "灰蓝士兵")
asset_node.data.update(asset)
other = canvas.add_node("asset", "深棕士兵")
other.data.update({**asset, "name": "深棕士兵", "anchors": ["右眉断口"]})
prop = canvas.add_node("asset", "钢筋")
prop.data.update({"kind": "prop", "name": "钢筋", "anchors": ["螺纹钢断口"], "consistency_tags": ["锈红螺纹"], "spec": "单件特写"})
board = canvas.add_node("storyboard", "第1场分镜")
prompt_node = canvas.add_node("prompt", "片段1")
canvas.connect(board.id, "shots", prompt_node.id, "ctx")
for node in (asset_node, other, prop):
    canvas.connect(node.id, "ref", prompt_node.id, "ref")
board.data["shots"] = [
    {
        **GOOD,
        "subjects": [dict(GOOD["subjects"][0], id=asset_node.id), dict(GOOD["subjects"][1], id=other.id)],
        "props": [{"id": prop.id, "state": "螺纹段贴住皮肤"}],
    }
]
board.data["segment_no"] = 1
prompt_node.data["segment_no"] = 1
problems = canvas.validate()
shot_problems = [p for p in problems if "分镜「第1场分镜」" in p or p.startswith("资产「")]
check("Canvas.validate 用新口径且不误报", not shot_problems, str(shot_problems))
board.data["shots"] = [clone(duration_s=12, subjects=board.data["shots"][0]["subjects"], props=board.data["shots"][0]["props"])]
check(
    "Canvas.validate 能报出越界时长",
    any("3–8s" in p for p in canvas.validate() if "分镜" in p),
    str(canvas.validate()),
)

print()
if failures:
    print("FAILED:", failures)
    raise SystemExit(1)
print("S5 ALL PASS")
