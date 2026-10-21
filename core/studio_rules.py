"""制片台硬校验：把「分镜/提示词能不能直接送去生成」变成可执行的算术。

设计原则
--------
1. **纯函数、只读**：不碰画布文件，不抛异常，统一返回 `list[Finding]`，由调用方决定展示还是拦截。
2. **字段口径只有一份**：镜头/资产的字段形状判定在 `core/canvas.py`（`shot_problems` /
   `asset_problems`），这里只把「问题」翻译成带严重度的结论，避免引擎层和业务层两套阈值漂移。
3. **所有阈值都带来源**：数字来自真实产线（见 `THRESHOLDS` 的注释），不是拍脑袋。

三道校验对应制作者真实的三个痛点
--------------------------------
- **镜头结构不成形** → 服务端渲染出来是空描述（缺机位、缺主体、时长超出窗口），模型自由发挥
- **时长装不下台词** → 生成出来口型对不上，白抽一次卡（一次视频调用 = 一次钱）
- **正文 @ 了不在入边里的资产** → 参考图没进去，角色外貌漂移，整段作废

这里**不再校验「提示词正文里的时间轴」**：一镜一段之后，时间由分镜字段累加算出，
正文里的多段时钟是旧范式（一段 = 多镜）的遗留，继续按文本解析只会把正常提示词判成错。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from core.canvas import asset_problems, shot_problems

# ---------------------------------------------------------------------------
# 阈值
# ---------------------------------------------------------------------------

THRESHOLDS: dict[str, float] = {
    # 中文台词朗读速度。影视配音常规 4-5 字/秒，取保守下限 4.5，
    # 这样算出的是「最短可行时长」，宁可多留余量也不要装不下。
    "speech_chars_per_second": 4.5,
    # 表演余量：入画、停顿、反应镜头。2 秒是行业里"不显得赶"的下限。
    "performance_margin_s": 2.0,
    # 单镜时长窗口。一镜一段 = 一次生成，主流视频模型在 3-8 秒最稳，常规 4-6 秒。
    # 低于 3s 信息量不足（模型会补无意义运动），高于 8s 一个镜头会空、容易崩脸。
    "shot_min_s": 3.0,
    "shot_max_s": 8.0,
    # 片段 = 一镜，所以片段窗口与单镜窗口一致；老画布的多镜片段由 check_shots 逐镜报。
    "segment_min_s": 3.0,
    "segment_max_s": 8.0,
    # 单次生成可携带的参考图上限。多数视频模型在 12 张之后开始忽略后面的。
    "reference_limit": 12,
    # 容差（秒）。四舍五入产生的亚秒级偏差不算错。
    "timeline_tolerance_s": 0.51,
}

# 分层的严重度：error 拦住生成，warn 只提示。
SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"
SEVERITY_INFO = "info"


class Finding(dict):
    """一条校验结论。用 dict 子类是为了直接 JSON 化给前端，同时保留属性访问。"""

    def __init__(self, rule: str, severity: str, message: str, **extra: Any) -> None:
        super().__init__(rule=rule, severity=severity, message=message, **extra)

    @property
    def rule(self) -> str:
        return self["rule"]

    @property
    def severity(self) -> str:
        return self["severity"]

    @property
    def message(self) -> str:
        return self["message"]


def _error(rule: str, message: str, **extra: Any) -> Finding:
    return Finding(rule, SEVERITY_ERROR, message, **extra)


def _warn(rule: str, message: str, **extra: Any) -> Finding:
    return Finding(rule, SEVERITY_WARN, message, **extra)


def _info(rule: str, message: str, **extra: Any) -> Finding:
    return Finding(rule, SEVERITY_INFO, message, **extra)


def worst_severity(findings: Iterable[Finding]) -> str:
    """汇总一组结论里最严重的级别，供前端一键上色。"""
    rank = {SEVERITY_INFO: 0, SEVERITY_WARN: 1, SEVERITY_ERROR: 2}
    best = SEVERITY_INFO
    for item in findings:
        if rank[item.severity] > rank[best]:
            best = item.severity
    return best


# ---------------------------------------------------------------------------
# 台词解析：旧画布把台词塞在 sound 里，新画布有 dialogue 字段
# ---------------------------------------------------------------------------

# 形如 `陈小满：一共两块五。` / `阿岸: 姐，扫码。` / `旁白：三年后。`
_SPEAKER_LINE = re.compile(r"^\s*(?P<speaker>[^：:\n]{1,12})\s*[：:]\s*(?P<line>.+?)\s*$")

# 形如 `同期对白 林越「303 的，夜宵到了。」` / `林越（沙哑）「你这手环……哪儿来的。」`
_QUOTED_LINE = re.compile(
    r"(?P<speaker>[^\s｜|，,。；;：:「」“”（）()]{1,12})"
    r"\s*(?:[（(][^）)]{0,24}[)）])?"
    r"\s*(?:[：:]|说|道)?\s*"
    r"[「“](?P<line>[^」”]{1,120})[」”]"
)

# 不该被当成说话人的前缀，避免把 "台词：" "字幕：" 解析成人名。
_NOT_A_SPEAKER = {"台词", "字幕", "旁白", "音效", "音乐", "画外音", "OS", "VO", "备注", "注"}
# 「仅环境音」「无对白」「全是底噪」这类描述性前缀不是人名。
_AMBIENT_PREFIX = ("仅", "无", "只", "全部", "均为", "本段")
# 旁白类：说话人留空，kind 记为 voiceover。
_VOICEOVER = {"旁白", "画外音", "OS", "VO", "内心"}
# 台词以外的环境音/音效：出现这些词就认为是噪音描述而非台词。
_AMBIENT_HINT = re.compile(r"(声|响|乐|音效|BGM|bgm)$")


def parse_dialogue(sound: str | None) -> list[dict[str, str]]:
    """从旧的 `sound` 字段尽力还原台词（新画布请直接用 `dialogue.text`）。"""
    text = (sound or "").strip()
    if not text:
        return []

    found: list[dict[str, str]] = []

    # 写法一：`名字「台词」`。在整段文本上找，避免被台词内的句号切断。
    for match in _QUOTED_LINE.finditer(text):
        speaker = match.group("speaker").strip(" 　、，,；;：:")
        line = match.group("line").strip()
        if not line:
            continue
        if speaker in _NOT_A_SPEAKER or speaker in _AMBIENT_PREFIX:
            if speaker in _VOICEOVER:
                found.append({"speaker": "", "line": line, "kind": "voiceover"})
            continue
        if speaker.startswith(_AMBIENT_PREFIX) or _AMBIENT_HINT.search(speaker):
            continue
        found.append({"speaker": speaker, "line": line, "kind": "dialogue"})
    if found:
        return found

    # 写法二：`名字：台词`。按句末标点切开逐块匹配。
    chunks = [chunk for chunk in re.findall(r"[^。！？!?]+[。！？!?]?", text) if chunk.strip()]
    for chunk in chunks:
        match = _SPEAKER_LINE.match(chunk)
        if not match:
            continue
        speaker = match.group("speaker").strip()
        line = match.group("line").strip()
        if not line:
            continue
        if speaker in _NOT_A_SPEAKER:
            if speaker in _VOICEOVER:
                found.append({"speaker": "", "line": line, "kind": "voiceover"})
            continue
        if speaker.startswith(_AMBIENT_PREFIX) or _AMBIENT_HINT.search(speaker):
            continue
        found.append({"speaker": speaker, "line": line, "kind": "dialogue"})
    return found


def dialogue_from_shots(shots: Sequence[Mapping[str, Any]] | None) -> list[dict[str, str]]:
    """汇总一段分镜里所有镜头的台词（`dialogue` 字段优先，旧画布退回 `sound`）。"""
    out: list[dict[str, str]] = []
    for shot in shots or []:
        entry = shot.get("dialogue")
        if isinstance(entry, Mapping):
            line = str(entry.get("text") or "").strip()
            if line:
                out.append({"speaker": str(entry.get("speaker") or "").strip(), "line": line, "kind": "dialogue"})
            continue
        if isinstance(entry, list) and entry:
            for item in entry:
                if not isinstance(item, Mapping):
                    continue
                line = str(item.get("line") or "").strip()
                if not line:
                    continue
                out.append(
                    {
                        "speaker": str(item.get("speaker") or "").strip(),
                        "line": line,
                        "kind": str(item.get("kind") or "dialogue"),
                    }
                )
            continue
        out.extend(parse_dialogue(str(shot.get("sound") or "")))
    return out


def spoken_char_count(lines: Sequence[Mapping[str, Any]]) -> int:
    """台词总字数（只算中文与字母数字，不数字标点）。"""
    total = 0
    for item in lines:
        for char in str(item.get("line") or ""):
            if char.isalnum():
                total += 1
    return total


# ---------------------------------------------------------------------------
# 规则 1：镜头结构（字段口径见 core/canvas.shot_problems）
# ---------------------------------------------------------------------------


def check_shots(
    shots: Sequence[Mapping[str, Any]] | None,
    *,
    name_by_id: Mapping[str, str] | None = None,
) -> list[Finding]:
    """一段分镜的逐镜体检：结构字段、引用闭包、禁止项。

    `name_by_id` 把资产 node id 映射到资产名，用来把 `@名字` 和 subjects/props 对齐。
    """
    items = list(shots or [])
    if not items:
        return [_error("shots.empty", "分镜没有任何镜头：一镜一段，至少写一个镜头")]

    names = dict(name_by_id or {})
    findings: list[Finding] = []
    for index, shot in enumerate(items, start=1):
        shot_no = shot.get("no") or index
        for problem in shot_problems(dict(shot), names):
            findings.append(_error("shot.field", f"镜{shot_no}：{problem}", shot_no=shot_no))
        forbidden = shot.get("forbidden")
        has_forbidden = isinstance(forbidden, list) and any(str(item).strip() for item in forbidden)
        if not has_forbidden:
            findings.append(
                _warn(
                    "shot.forbidden",
                    f"镜{shot_no} 没有写 forbidden：模型会自己补人、补景、补文字",
                    shot_no=shot_no,
                )
            )
    return findings


def check_asset(asset: Mapping[str, Any] | None, *, label: str = "") -> list[Finding]:
    """资产节点的字段体检（kind/name/anchors/consistency_tags/spec，且 spec 不得要三视图）。"""
    prefix = f"{label}：" if label else ""
    return [
        _error("asset.field", prefix + problem)
        for problem in asset_problems(dict(asset or {}))
    ]


# ---------------------------------------------------------------------------
# 规则 2：片段时长 vs 台词量
# ---------------------------------------------------------------------------


def check_segment_duration(
    duration_s: float | None,
    dialogue: Sequence[Mapping[str, Any]] | None = None,
    *,
    thresholds: Mapping[str, float] | None = None,
) -> list[Finding]:
    """片段时长是否装在 3–8s 窗口内（一镜一段），且装得下台词。"""
    limits = {**THRESHOLDS, **(thresholds or {})}
    findings: list[Finding] = []

    if duration_s is None or duration_s <= 0:
        return [_error("segment.duration", "片段没有时长，无法排产")]

    duration = float(duration_s)
    low = limits["segment_min_s"]
    high = limits["segment_max_s"]

    if duration < low:
        findings.append(
            _error(
                "segment.duration.too_short",
                f"片段 {duration:g}s 低于 {low:g}s 下限：镜头信息量不足，模型容易输出无意义的运动",
                duration=duration,
                limit=low,
            )
        )
    elif duration > high:
        findings.append(
            _error(
                "segment.duration.too_long",
                f"片段 {duration:g}s 超过 {high:g}s 上限：一镜一段，超过就拆成两个片段，"
                "否则模型会在一个镜头里自己填时间",
                duration=duration,
                limit=high,
            )
        )

    lines = list(dialogue or [])
    if lines:
        chars = spoken_char_count(lines)
        cps = limits["speech_chars_per_second"]
        margin = limits["performance_margin_s"]
        needed = chars / cps + margin
        if needed > duration + 1e-6:
            findings.append(
                _error(
                    "segment.duration.speech_overflow",
                    (
                        f"台词 {chars} 字需要约 {needed:.1f}s（{cps:g} 字/秒 + {margin:g}s 表演余量），"
                        f"但片段只有 {duration:g}s：装不下，必须把台词拆到下一个片段"
                    ),
                    chars=chars,
                    required_s=round(needed, 2),
                    duration=duration,
                )
            )
        elif needed > duration * 0.9:
            findings.append(
                _info(
                    "segment.duration.speech_tight",
                    f"台词 {chars} 字占掉约 {needed:.1f}s / {duration:g}s，几乎没有表演余量",
                    chars=chars,
                    required_s=round(needed, 2),
                    duration=duration,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# 规则 3：引用闭包（正文 @名字 vs 入边资产）
# ---------------------------------------------------------------------------

# 提示词里的 `@名字`；不匹配 `@图片1名字` 这种已物化的形式。
# 名字的边界必须包含全角/半角左括号：服务端渲染的写法是 `@林直人（画面左三分之一；…）`，
# 不在这里截断的话括号内容会被吞进资产名，造成「引用不在入边里」的误报。
_MENTION = re.compile(r"(?<!图片)(?<!\d)@([^\s@，,。；;：:、（）()【】\[\]\"'“”]{1,64})")
# 序号式引用：`{{ref 1}}`。历史画布用过这种写法，只读兼容，不再产生。
_REF_TOKEN = re.compile(r"\{\{\s*ref\s*(?P<ordinal>\d+)\s*\}\}", re.IGNORECASE)


def extract_ref_ordinals(prompt: str) -> list[int]:
    """抽出 `{{ref N}}` 里的序号，按首次出现顺序去重。"""
    seen: list[int] = []
    for match in _REF_TOKEN.finditer(prompt or ""):
        ordinal = int(match.group("ordinal"))
        if ordinal not in seen:
            seen.append(ordinal)
    return seen


def extract_mentions(prompt: str) -> list[str]:
    """抽出提示词里所有 `@名字`，按首次出现顺序去重。"""
    seen: list[str] = []
    for match in _MENTION.finditer(prompt or ""):
        name = match.group(1).strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def check_references(
    prompt: str,
    references: Sequence[Mapping[str, Any]] | None,
    *,
    name_by_id: Mapping[str, str] | None = None,
    thresholds: Mapping[str, float] | None = None,
) -> list[Finding]:
    """正文里的 `@名字` 必须落在本片段的入边资产集合里，引用数不超限。

    `references` 每项形如 `{"asset_id": "n_x", "look_id": "look_1", "ordinal": 1}`；
    `name_by_id` 把 asset_id 映射到资产名。**只对照入边集合，不再到正文里猜纯文本**：
    名字没 @ 上就是没 @ 上，正文里出现几个字不算引用。
    """
    limits = {**THRESHOLDS, **(thresholds or {})}
    limit = int(limits["reference_limit"])
    refs = list(references or [])
    names_by_id = dict(name_by_id or {})
    findings: list[Finding] = []

    if len(refs) > limit:
        findings.append(
            _error(
                "refs.over_limit",
                f"引用了 {len(refs)} 个素材，超过模型上限 {limit} 个：多出来的会被模型忽略",
                count=len(refs),
                limit=limit,
            )
        )

    ordinals = [int(item.get("ordinal") or 0) for item in refs]
    if refs and sorted(ordinals) != list(range(1, len(refs) + 1)):
        findings.append(
            _warn(
                "refs.ordinal",
                "引用序号不是从 1 开始的连续整数，物化参考图时会错位",
                ordinals=ordinals,
            )
        )

    def _name_of(item: Mapping[str, Any]) -> str:
        return str(names_by_id.get(str(item.get("asset_id")), "") or item.get("name") or "").strip()

    inbound = {name for name in (_name_of(item) for item in refs) if name}
    mentions = extract_mentions(prompt)
    ref_ordinals = set(extract_ref_ordinals(prompt))

    for name in mentions:
        if name not in inbound:
            findings.append(
                _error(
                    "refs.dangling_mention",
                    f"正文引用了 @{name}，但它不在本片段的入边资产里：生成时这张图不会被送进模型",
                    mention=name,
                )
            )
    for item in refs:
        name = _name_of(item)
        if not name:
            continue
        if name in mentions:
            continue
        if int(item.get("ordinal") or 0) in ref_ordinals:
            continue
        findings.append(
            _info(
                "refs.unused",
                f"入边资产「{name}」已连上，但正文没有 @ 它：生成时可能用不上",
                mention=name,
            )
        )
    return findings


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def lint_segment(
    *,
    duration_s: float | None,
    shots: Sequence[Mapping[str, Any]] | None = None,
    prompt: str = "",
    references: Sequence[Mapping[str, Any]] | None = None,
    name_by_id: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """一个片段的完整体检。返回可直接给前端的结构。"""
    items = list(shots or [])
    dialogue = dialogue_from_shots(items)
    computed = round(sum(float(shot.get("duration_s") or shot.get("duration") or 0) for shot in items), 2)

    findings: list[Finding] = []
    findings.extend(check_shots(items, name_by_id=name_by_id))
    # speaker 必须能落到具体资产：写了个查无此人的名字，配音阶段就不知道用谁的嗓音
    _known_ids = {str(k) for k in (name_by_id or {})}
    _known_names = {str(v) for v in (name_by_id or {}).values()}
    for shot in items:
        d = shot.get("dialogue") if isinstance(shot.get("dialogue"), Mapping) else {}
        spk = str(d.get("speaker") or "").strip()
        if spk and spk not in _known_ids and spk not in _known_names:
            findings.append(
                _error(
                    "segment.dialogue.speaker_unknown",
                    f"台词说话人「{spk}」不是本片段引用的资产；改成 @资产名 或对应资产 id，配音才能绑定音色",
                    speaker=spk,
                )
            )
            break  # 一条足够，别刷屏
    findings.extend(check_segment_duration(duration_s, dialogue))
    findings.extend(check_references(prompt, references, name_by_id=name_by_id))

    # 分镜时长之和 vs 片段时长：一镜一段时两者应当相等。
    if duration_s and computed and abs(computed - float(duration_s)) > THRESHOLDS["timeline_tolerance_s"]:
        findings.append(
            _warn(
                "segment.shots_sum",
                f"分镜时长合计 {computed:g}s，片段时长 {float(duration_s):g}s，两者不一致",
                shots_sum=computed,
                duration=float(duration_s),
            )
        )

    return {
        "severity": worst_severity(findings),
        "blocking": any(item.severity == SEVERITY_ERROR for item in findings),
        "findings": [dict(item) for item in findings],
        "dialogue": dialogue,
        "dialogue_chars": spoken_char_count(dialogue),
        "dialogue_seconds": round(
            spoken_char_count(dialogue) / THRESHOLDS["speech_chars_per_second"]
            + THRESHOLDS["performance_margin_s"],
            2,
        )
        if dialogue
        else 0.0,
        "shots_sum": computed,
        "shot_count": len(items),
        "mentions": extract_mentions(prompt),
        "reference_count": len(list(references or [])),
        "reference_limit": int(THRESHOLDS["reference_limit"]),
    }


# ---------------------------------------------------------------------------
# stale 指纹：生成物记录「生成时看到了什么」，读取时现算比对，落后即过期
# ---------------------------------------------------------------------------


def stable_hash(obj: Any) -> str:
    """结构化对象的稳定指纹：字段变化必变，键序无关。16 位十六进制足够防撞。"""
    import hashlib
    import json

    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def shots_hash(shots: Sequence[Mapping[str, Any]] | None) -> str:
    """分镜结构化字段指纹：首帧/视频据此判定「是否已落后于当前分镜」。"""
    if not shots:
        return ""
    return stable_hash(list(shots))


def refs_hash(refs: Iterable[Mapping[str, Any]] | None) -> str:
    """片段引用指纹：资产 id / 当前形象 / 图路径 任一变化即变（换形象=过期）。"""
    rows = [
        {
            "asset_id": str(r.get("asset_id") or ""),
            "active_look_id": str(r.get("active_look_id") or ""),
            "image_path": str(r.get("image_path") or ""),
        }
        for r in (refs or [])
    ]
    if not rows:
        return ""
    return stable_hash(rows)
