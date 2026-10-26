"""门哨兵（<<GATE-RULES>>）：六门 validate 输出末尾的机器可读规则行。

质量门分级 Task 1：门脚本只在输出层 additive 一行哨兵（人读输出逐字不变、
规则判定逻辑零改动），执行器据此按 rule id 分级 blocking / advisory。
本文件纯 subprocess 跑真 node 门——不碰数据库、不碰 LLM。

给 Task 2 的接口：CHARACTERS_ADVISORY_IDS——characters 门两条 advisory 规则
（引文逐字、voice.prompt 长度）的 id，直接 import 进 StageSpec["advisory_rules"]。
"""

import json
import subprocess
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILLS = _REPO_ROOT / "pipeline_skills"
_SENTINEL = "<<GATE-RULES>>"

# characters 门 advisory 白名单（spec §4：引文逐字 + voice.prompt 长度）。
# id 由本任务在 characters 门输出层盘出并行为锁定（见
# test_characters_advisory_ids_map_to_real_rules）；Task 2 消费。
CHARACTERS_ADVISORY_IDS = frozenset({"quote-verbatim", "voice-prompt-length"})

# 六门全量 rule id 清单（从各门 gateReport 实跑盘点，非 grep 抄写；outline / script /
# characters 三门由 lock 测试断言集合相等防 .mjs 演化漂移，art / staging / storyboard
# 只锁哨兵形状）：
# outline:     lead-cap, support-cap, functional-cap, scene-cap, prop-cap, once-scene,
#              beat-gap, ep1-hook, major-early, ep-fields, crowd-plan, risk-flag,
#              no-dialogue, refs
# script:      duration, line-length, speaker, hook-cliff, hook-open, has-action,
#              action-prose, beats-claimed, refs-characters, refs-scenes
# characters:  quote-verbatim + voice-prompt-length（advisory，即上面常量）；
#              cast-structure（blocking 汇总项——卡结构 / summary 缺失 / 提示词雷同 /
#              音色提示词含引号台词等，spec §4「其余（summary 缺失等结构类）」全归它）
# art:         anchors, lighting, no-people, english, no-names, variants, prop-states,
#              prop-scale, prop-hands, prop-white
# staging:     items-read, item-shape, location-ref, cast-complete, present-set,
#              place-valid, no-overlap, extras
# storyboard:  coverage, segment-cap, cut-length, dialogue-fit, ep-duration, crowd,
#              segment-id, size-phrase, camera-phrase, h3-structure, h3-dialogue,
#              h3-lang, frame-prompt, prompt-no-names, composition, seedance-shot,
#              refs, shot-recipe

OUTLINE_IDS = frozenset({
    "lead-cap", "support-cap", "functional-cap", "scene-cap", "prop-cap",
    "once-scene", "beat-gap", "ep1-hook", "major-early", "ep-fields",
    "crowd-plan", "risk-flag", "no-dialogue", "refs",
})
SCRIPT_IDS = frozenset({
    "duration", "line-length", "speaker", "hook-cliff", "hook-open",
    "has-action", "action-prose", "beats-claimed", "refs-characters", "refs-scenes",
})
CHARACTERS_IDS = CHARACTERS_ADVISORY_IDS | {"cast-structure"}


def _run_gate(skill: str, script: str, args: list[str], files: dict[str, str]) -> subprocess.CompletedProcess:
    """在临时工作目录落 fixture 后跑门（与执行器 _run_gate 同一条 node 子进程方式）。"""
    with tempfile.TemporaryDirectory(prefix="gate-sentinel-") as td:
        work = Path(td)
        for name, content in files.items():
            (work / name).write_text(content, encoding="utf-8")
        return subprocess.run(
            ["node", str(_SKILLS / skill / "scripts" / script), *args],
            cwd=work, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120,
        )


def _sentinel_rules(proc: subprocess.CompletedProcess) -> list[dict]:
    """断言 stdout 恰有一行哨兵且可解析为 [{id, ok, detail}]，返回解析结果。"""
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(_SENTINEL)]
    assert len(lines) == 1, (
        f"哨兵行应恰好一行，实际 {len(lines)} 行\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    rules = json.loads(lines[0][len(_SENTINEL):])
    assert isinstance(rules, list) and rules, "哨兵应是非空 JSON 数组"
    for r in rules:
        assert isinstance(r, dict) and set(r) == {"id", "ok", "detail"}, f"每条规则字段恰为 id/ok/detail：{r}"
        assert isinstance(r["id"], str) and r["id"], f"id 必须是非空字符串：{r}"
        assert isinstance(r["ok"], bool), f"ok 必须是布尔：{r}"
        assert isinstance(r["detail"], str), f"detail 必须是字符串：{r}"
    return rules


def test_outline_validate_emits_sentinel_and_locks_ids():
    # {} 大纲 = 门失败路径（缺 params / 空数组）——exit 1 前也要有哨兵
    proc = _run_gate(
        "novel-outline", "novel-outline.mjs",
        ["validate", "outline.json", "--stage", "full"],
        {"outline.json": "{}"},
    )
    assert proc.returncode == 1, f"空大纲应门失败：\n{proc.stdout}\n{proc.stderr}"
    rules = _sentinel_rules(proc)
    assert {r["id"] for r in rules} == OUTLINE_IDS
    risk = next(r for r in rules if r["id"] == "risk-flag")
    assert risk["ok"] is False, "{} 大纲没有预警清单，risk-flag 应记失败"


def test_script_validate_emits_sentinel_and_locks_ids():
    # {} 剧本 = 门失败路径；不带 --outline / --art，跳过型规则应 ok=True
    proc = _run_gate(
        "novel-script", "novel-script.mjs",
        ["validate", "script.json"],
        {"script.json": "{}"},
    )
    assert proc.returncode == 1, f"空剧本应门失败：\n{proc.stdout}\n{proc.stderr}"
    rules = _sentinel_rules(proc)
    assert {r["id"] for r in rules} == SCRIPT_IDS
    by_id = {r["id"]: r for r in rules}
    for skip_id in ("beats-claimed", "refs-characters", "refs-scenes"):
        assert by_id[skip_id]["ok"] is True, f"未给上游时 {skip_id} 应按跳过记通过"
        assert "跳过" in by_id[skip_id]["detail"], f"{skip_id} 的跳过语义应留在 detail"
    assert by_id["duration"]["ok"] is False, "{} 剧本没有集，duration 应记失败"


def test_characters_validate_emits_sentinel_and_locks_ids():
    # {"characters": []} = 门失败路径（cast 为空 + 缺 summary 都是结构违规）
    proc = _run_gate(
        "novel-characters", "novel-characters.mjs",
        ["validate", "cast.json", "book.txt"],
        {"cast.json": '{"characters": []}', "book.txt": ""},
    )
    assert proc.returncode == 1, f"空角色表应门失败：\n{proc.stdout}\n{proc.stderr}"
    rules = _sentinel_rules(proc)
    assert {r["id"] for r in rules} == CHARACTERS_IDS
    by_id = {r["id"]: r for r in rules}
    assert by_id["cast-structure"]["ok"] is False, "空表 / 缺 summary 属结构违规，应记在 cast-structure"
    assert all(r["ok"] is True for r in rules if r["id"] in CHARACTERS_ADVISORY_IDS), (
        "fixture 没踩 advisory 两条，它们应记通过"
    )


def test_characters_advisory_ids_map_to_real_rules():
    """CHARACTERS_ADVISORY_IDS 与真实规则的行为映射：fixture 同时踩中
    引文不逐字 + voice.prompt 超 400，两条 advisory 应各自 ok=False 并携带原文细节。"""
    cast = json.dumps(
        {"characters": [{
            "name": "张三",
            "persona": {"evidence": ["原文里没有这句话"]},
            "voice": {"prompt": "x" * 401},
        }]},
        ensure_ascii=False,
    )
    proc = _run_gate(
        "novel-characters", "novel-characters.mjs",
        ["validate", "cast.json", "book.txt"],
        {"cast.json": cast, "book.txt": "原文只有这一句。"},
    )
    assert proc.returncode == 1
    by_id = {r["id"]: r for r in _sentinel_rules(proc)}
    assert by_id["quote-verbatim"]["ok"] is False
    assert "引文不是原文逐字片段" in by_id["quote-verbatim"]["detail"]
    assert by_id["voice-prompt-length"]["ok"] is False
    assert "401" in by_id["voice-prompt-length"]["detail"]
    assert by_id["cast-structure"]["ok"] is False, "其余结构违规照旧记 blocking 汇总项"


def test_art_validate_emits_sentinel():
    # 只锁哨兵存在 + JSON 形状（id 集合不 lock，见文件头清单注释）
    proc = _run_gate(
        "novel-art", "novel-art.mjs",
        ["validate", "art.json"],
        {"art.json": "{}"},
    )
    assert proc.returncode == 1, f"空美术设定应门失败：\n{proc.stdout}\n{proc.stderr}"
    _sentinel_rules(proc)


def test_staging_validate_emits_sentinel_on_success():
    # schema.md 骨架（{} 会在 staging 崩）＝全绿成功路径——通过分支同样要有哨兵
    skeleton = json.dumps({"source": "x", "lang": "zh", "locations": [], "scenes": []}, ensure_ascii=False)
    proc = _run_gate(
        "novel-staging", "novel-staging.mjs",
        ["validate", "staging.json"],
        {"staging.json": skeleton},
    )
    assert proc.returncode == 0, f"骨架空站位应通过校验：\n{proc.stdout}\n{proc.stderr}"
    rules = _sentinel_rules(proc)
    assert all(r["ok"] is True for r in rules), "全绿门的哨兵应全部 ok=True"


def test_storyboard_validate_emits_sentinel():
    # 分镜 validate 必须给 --script；{} 剧本足够触发门失败路径
    proc = _run_gate(
        "novel-storyboard", "novel-storyboard.mjs",
        ["validate", "storyboard.json", "--script", "script.json"],
        {"storyboard.json": "{}", "script.json": "{}"},
    )
    assert proc.returncode == 1, f"空分镜应门失败：\n{proc.stdout}\n{proc.stderr}"
    _sentinel_rules(proc)
