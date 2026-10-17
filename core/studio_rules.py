"""制片台硬校验：把「分镜/提示词能不能直接送去生成」变成可执行的算术。

设计原则
--------
1. **纯函数、只读**：不碰画布文件，不抛异常，统一返回 `list[Finding]`，由调用方决定展示还是拦截。
2. **不修改 `Canvas.validate()`**：那是拓扑与端口契约的校验，属于引擎层；这里是业务层规则，
   挂在适配层外面，`server/studio.py` 与前端都能调。
3. **所有阈值都带来源**：数字来自真实产线（见 `THRESHOLDS` 的注释），不是拍脑袋。

三条规则对应制作者真实的三个痛点
--------------------------------
- **时长装不下台词** → 生成出来口型对不上，白抽一次卡（一次视频调用 = 一次钱）
- **提示词时间轴断档/重叠** → 模型对时间轴的理解失配，回来的是"糊"的片子
- **提示词里 @ 了不存在的参考图** → 参考图没进去，角色外貌漂移，整段作废
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from core.canvas import SHOT_FIELDS

# ---------------------------------------------------------------------------
# 阈值
# ---------------------------------------------------------------------------

THRESHOLDS: dict[str, float] = {
    # 中文台词朗读速度。影视配音常规 4-5 字/秒，取保守下限 4.5，
    # 这样算出的是「最短可行时长」，宁可多留余量也不要装不下。
    "speech_chars_per_second": 4.5,
    # 表演余量：入画、停顿、反应镜头。2 秒是行业里"不显得赶"的下限。
    "performance_margin_s": 2.0,
    # 单个片段的时长窗口。真实产线有两种粒度：
    #   ①「一段 = 一镜」= 一次生成 —— 本系统默认，单镜 3-5 秒是主流视频模型最稳的区间；
    #   ②「一段 = 多镜」= 人工剪辑粒度 —— 旧画布/火宝式产物，8-15 秒。
    # 下限不能按多镜的 8 秒卡单镜：8 秒的硬下限会把正常的三秒镜头全判成错误。
    # 真正约束「一镜不要太碎」的是 shot_min_s，所以这里只保留一个很宽的下限兜底。
    "segment_min_s": 3.0,
    "segment_max_s": 15.0,
    # 视频提示词的时间分段粒度：每 3 秒一段，是"画面描述密度"的经验平衡点。
    "prompt_step_s": 3.0,
    # 子镜头时长的可接受区间。低于 1.5s 观众看不清，高于 7s 一个镜头会空。
    "shot_min_s": 1.5,
    "shot_max_s": 7.0,
    # 单次生成可携带的参考图上限。多数视频模型在 12 张之后开始忽略后面的。
    "reference_limit": 12,
    # 允许的时间轴误差（秒）。四舍五入产生的亚秒级偏差不算错。
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
# 台词解析：把「被塞进 sound 字段的台词」还原成可机读结构
# ---------------------------------------------------------------------------

# 形如 `陈小满：一共两块五。` / `阿岸: 姐，扫码。` / `旁白：三年后。`
_SPEAKER_LINE = re.compile(r"^\s*(?P<speaker>[^：:\n]{1,12})\s*[：:]\s*(?P<line>.+?)\s*$")

# 形如 `同期对白 林越「303 的，夜宵到了。」` / `林越（沙哑）「你这手环……哪儿来的。」`
# 真实数据里分镜的 sound 字段用的是这种「名字 + 引号」写法，台词内容自带句末标点，
# 所以只能在整段文本上匹配，不能先按句号切块（会把引号切成两半）。
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
    """从 `sound` 字段解析台词。

    真实数据里台词被塞在 storyboard.shots[i].sound 里，两种写法都有：

    - `"脚步、喘息声；同期对白 林越「303 的，夜宵到了。」"`（主流）
    - `"陈小满：一共两块五。"`

    这里做尽力而为的还原：能拆出「说话人 + 台词」的收进结果，拆不出的当环境音。
    返回 `[{"speaker": "林越", "line": "303 的，夜宵到了。", "kind": "dialogue"}]`，
    `kind` 为 `dialogue` 或 `voiceover`（旁白，speaker 置空）。
    """
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
        # 「音效：雨声」「仅环境音：…」这类是提示而非台词，跳过。
        if speaker in _NOT_A_SPEAKER:
            if speaker in _VOICEOVER:
                found.append({"speaker": "", "line": line, "kind": "voiceover"})
            continue
        if speaker.startswith(_AMBIENT_PREFIX):
            continue
        # 说话人本身以「声/响/乐」结尾的多半是音效描述。
        if _AMBIENT_HINT.search(speaker):
            continue
        found.append({"speaker": speaker, "line": line, "kind": "dialogue"})
    return found


def dialogue_from_shots(shots: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """汇总一段分镜里所有镜头的台词（含从 sound 兜底解析的部分）。"""
    out: list[dict[str, str]] = []
    for shot in shots or []:
        explicit = shot.get("dialogue")
        if isinstance(explicit, list) and explicit:
            for item in explicit:
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
# 规则 1：片段时长 vs 台词量
# ---------------------------------------------------------------------------


def check_segment_duration(
    duration_s: float | None,
    dialogue: Sequence[Mapping[str, Any]] | None = None,
    *,
    thresholds: Mapping[str, float] | None = None,
) -> list[Finding]:
    """片段时长是否装在 8–15s 窗口内，且装得下台词。"""
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
                f"片段 {duration:g}s 超过 {high:g}s 上限：需要拆成两个片段，否则长镜头容易崩脸",
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
# 规则 2：提示词时间轴
# ---------------------------------------------------------------------------

# 项目自己的提示词格式：`- 镜头 1-2｜00:02.5–00:06.5｜实际出镜（半身）｜说话`
_SHOT_RANGE = re.compile(
    r"镜头\s*(?P<shot>\d+\s*-\s*\d+)\s*[｜|]\s*"
    r"(?P<start>\d{1,2}:\d{2}(?:\.\d+)?)\s*[-–—~至]\s*(?P<end>\d{1,2}:\d{2}(?:\.\d+)?)"
)
# 时钟区间：`00:00–00:15`。不带冒号的 `1-1` / `0–00` 一律不认，避免把「镜头 1-1」当时间轴。
_CLOCK_RANGE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?:\.\d+)?)\s*[-–—~至]\s*(?P<end>\d{1,2}:\d{2}(?:\.\d+)?)"
)
# 秒单位区间：`0-3秒：…`（火宝视频提示词的分段写法）
#
# 真实写法只结尾带一次单位（`0-3秒：` / `3-7秒：` / `7-11秒：`），
# 所以不能要求起止两侧都带「秒」。为了不把散文里的「8-15 个」误判成时间轴，
# 这里三条约束同时成立才算分段：①行首锚定 ②结尾必须带单位 ③后面紧跟分隔符。
#
# 分隔符要放宽到「冒号或竖线」：模型写表格体时会用全角竖线（`0-3秒｜特写｜固定机位：…`），
# 校验器认不出来就会把正常提示词全判成「没有时间分段」。这是排版差异，不是内容错误。
_SECOND_RANGE = re.compile(
    r"^\s*[-*·]?\s*(?P<start>\d+(?:\.\d+)?)\s*(?:s|秒)?\s*[-–—~至]\s*"
    r"(?P<end>\d+(?:\.\d+)?)\s*(?:s|秒)\s*[：:|｜]"
)


def _clock_to_seconds(text: str) -> float:
    """`00:02.5` → 2.5；`01:00` → 60。"""
    minutes, _, seconds = text.partition(":")
    return round(float(minutes) * 60 + float(seconds), 3)


def parse_timeline(prompt: str) -> list[dict[str, Any]]:
    """把提示词里的时间分段抽出来，按出现顺序返回。

    支持两种真实存在的写法，优先级从高到低：

    1. 子镜头区间：`镜头 1-2｜00:02.5–00:06.5｜…`。同一镜头会在每个角色小节里
       重复出现，按镜头号去重。
    2. 时钟区间：`00:00–00:15`（出现在提示词抬头）。
    3. 秒区间：`0-3秒：…`（火宝式视频提示词）。

    返回 `[{"start": 2.5, "end": 6.5, "shot": "1-2", "kind": "shot", "text": "…"}]`。
    """
    text = prompt or ""
    if not text.strip():
        return []

    # 1) 子镜头区间（按镜头号去重，保留首次出现的顺序）
    shots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _SHOT_RANGE.finditer(text):
        label = re.sub(r"\s+", "", match.group("shot"))
        if label in seen:
            continue
        seen.add(label)
        tail = text[match.end() : match.end() + 120].splitlines()[0]
        shots.append(
            {
                "shot": label,
                "start": _clock_to_seconds(match.group("start")),
                "end": _clock_to_seconds(match.group("end")),
                "kind": "shot",
                "text": tail.lstrip("｜| ：: 　").strip(),
            }
        )
    if shots:
        return shots

    # 2) 秒单位区间
    seconds: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        match = _SECOND_RANGE.search(line)
        if match:
            seconds.append(
                {
                    "start": float(match.group("start")),
                    "end": float(match.group("end")),
                    "kind": "second",
                    "text": line[match.end() :].lstrip("：:|｜ 　").strip(),
                }
            )
    if seconds:
        return seconds

    # 3) 时钟区间（取前两个，通常就是抬头里的全片时间段）
    clocks: list[dict[str, Any]] = []
    for match in _CLOCK_RANGE.finditer(text):
        clocks.append(
            {
                "start": _clock_to_seconds(match.group("start")),
                "end": _clock_to_seconds(match.group("end")),
                "kind": "clock",
                "text": "",
            }
        )
        if len(clocks) >= 2:
            break
    return clocks


def check_prompt_timeline(
    prompt: str,
    duration_s: float | None,
    *,
    thresholds: Mapping[str, float] | None = None,
    single_shot: bool = False,
) -> list[Finding]:
    """时间轴是否连续、覆盖整段时长、单个镜头不过长不过短。

    `single_shot=True` 表示这一段等于一个镜头、一次生成：此时提示词正文就是这一镜，
    不需要（也不应该）再写多段时间轴，否则又会退回「一次生成里塞多个镜头」的老问题。
    """
    limits = {**THRESHOLDS, **(thresholds or {})}
    step = limits["prompt_step_s"]
    tolerance = limits["timeline_tolerance_s"]
    shot_min = limits["shot_min_s"]
    shot_max = limits["shot_max_s"]

    if not (prompt or "").strip():
        return [_error("prompt.empty", "提示词为空")]

    segments = parse_timeline(prompt)
    if not segments:
        if single_shot:
            return []
        return [
            _error(
                "prompt.timeline.missing",
                "提示词里没有可识别的时间分段：子镜头要写成「镜头 1-2｜00:02.5–00:06.5」，"
                "或按 3 秒分段写成「0-3秒：…」",
            )
        ]

    findings: list[Finding] = []
    shot_mode = segments[0]["kind"] == "shot"

    # 每段长度
    for index, seg in enumerate(segments, start=1):
        label = f"镜头 {seg['shot']}" if seg.get("shot") else f"第 {index} 段"
        span = round(seg["end"] - seg["start"], 3)
        if span <= 0:
            findings.append(
                _error(
                    "prompt.timeline.inverted",
                    f"{label} 的时间区间倒置（{seg['start']:g}→{seg['end']:g}s）",
                    index=index,
                )
            )
        elif shot_mode:
            if span > shot_max + 1e-6:
                findings.append(
                    _warn(
                        "prompt.timeline.shot_long",
                        f"{label} 长 {span:g}s，超过 {shot_max:g}s 的单镜上限：观众会觉得镜头黏住不动",
                        index=index,
                        span=span,
                        limit=shot_max,
                    )
                )
            elif span < shot_min - 1e-6:
                findings.append(
                    _warn(
                        "prompt.timeline.shot_short",
                        f"{label} 只有 {span:g}s，短于 {shot_min:g}s：一闪而过，观众看不清动作",
                        index=index,
                        span=span,
                        limit=shot_min,
                    )
                )
        elif not shot_mode and (span > shot_max + 1e-6 or span < shot_min - 1e-6):
            # 「按 3 秒分段」是起步模板，不是死规矩：真实模型吃 2-6 秒的段都很稳。
            # 只有明显跨出单镜合理区间（会把多个动作糊在一起 / 一闪而过）才提示。
            findings.append(
                _warn(
                    "prompt.timeline.span",
                    f"{label} 长 {span:g}s，超出 {shot_min:g}-{shot_max:g}s 的合理区间："
                    "太长模型会把多个动作糊在一起，太短观众看不清",
                    index=index,
                    span=span,
                    limit=step,
                )
            )
        if not shot_mode and not seg["text"]:
            findings.append(
                _warn("prompt.timeline.blank", f"{label} 只有时间没有画面描述", index=index)
            )

    # 连续性：前一段的 end 必须接上后一段的 start
    for index in range(1, len(segments)):
        prev, cur = segments[index - 1], segments[index]
        gap = round(cur["start"] - prev["end"], 3)
        left = f"镜头 {prev['shot']}" if prev.get("shot") else f"第 {index} 段"
        right = f"镜头 {cur['shot']}" if cur.get("shot") else f"第 {index + 1} 段"
        if gap > tolerance:
            findings.append(
                _error(
                    "prompt.timeline.gap",
                    f"{left} 与 {right} 之间有 {gap:g}s 空缺：模型会在这段里自由发挥",
                    index=index,
                    gap=gap,
                )
            )
        elif gap < -tolerance:
            findings.append(
                _error(
                    "prompt.timeline.overlap",
                    f"{left} 与 {right} 重叠 {-gap:g}s：同一时间被两段描述争抢",
                    index=index,
                    gap=gap,
                )
            )

    # 起点归零
    if abs(segments[0]["start"]) > tolerance:
        findings.append(
            _warn(
                "prompt.timeline.starts_late",
                f"时间轴从 {segments[0]['start']:g}s 开始，应该从 0s 开始",
            )
        )

    # 总长对齐
    if duration_s and duration_s > 0:
        coverage = round(segments[-1]["end"] - segments[0]["start"], 3)
        if abs(coverage - float(duration_s)) > tolerance:
            findings.append(
                _error(
                    "prompt.timeline.coverage",
                    f"时间轴覆盖 {coverage:g}s，但片段时长是 {float(duration_s):g}s：两者必须相等",
                    coverage=coverage,
                    duration=float(duration_s),
                )
            )
        if not shot_mode:
            expected = int(-(-float(duration_s) // step))  # ceil
            # 允许 ±1 段：11s÷3s=3.67，写成 3 段各 3-4-4s 也是合法的覆盖方式。
            if abs(len(segments) - expected) > 1:
                findings.append(
                    _warn(
                        "prompt.timeline.count",
                        f"共 {len(segments)} 段，{float(duration_s):g}s ÷ {step:g}s 约 {expected} 段，段数偏差较大",
                        count=len(segments),
                        expected=expected,
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# 规则 3：引用完整性
# ---------------------------------------------------------------------------

# 提示词里的 `@名字`；不匹配 `@图片1名字` 这种已物化的形式。
_MENTION = re.compile(r"(?<!图片)(?<!\d)@([^\s@，,。；;：:、）)】\]\"'“”]{1,24})")
# 序号式引用：`{{ref 1}}`。画布的资产规范里用的是这种写法，而外部模型吃的是 `@名字`，
# 两种都是合法输入，校验器必须都认得，否则同一条提示词换个写法就判「没引用素材」。
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
    """提示词里的 `@名字` 与引用清单是否对得上，引用数是否超限。

    `references` 每项形如 `{"asset_id": "n_x", "look_id": "look_1", "ordinal": 1}`；
    `name_by_id` 把 asset_id 映射到资产名，用来核对 `@名字`。
    """
    limits = {**THRESHOLDS, **(thresholds or {})}
    limit = int(limits["reference_limit"])
    refs = list(references or [])
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

    names = {name_by_id.get(str(item.get("asset_id")), "") for item in refs} if name_by_id else set()
    names.discard("")
    mentions = extract_mentions(prompt)
    ref_ordinals = set(extract_ref_ordinals(prompt))
    ordinal_by_id = {str(item.get("asset_id")): int(item.get("ordinal") or 0) for item in refs}

    def _used(name: str) -> bool:
        """参考素材是否真的被提示词提到。

        真实提示词里资产名是直接写进正文的（`林越（外卖员）｜出镜状态总览`），
        不一定带 `@`。所以 `@名字`、`{{ref N}}` 和纯文本出现都算用到，只要判空不要误报。
        """
        if name in mentions:
            return True
        if name and name in prompt:
            return True
        base = name.split("（")[0].split("(")[0].strip()
        return bool(base) and base in prompt

    def _used_by_id(asset_id: str) -> bool:
        return ordinal_by_id.get(asset_id, 0) in ref_ordinals

    for name in mentions:
        if names and name not in names:
            findings.append(
                _error(
                    "refs.dangling_mention",
                    f"提示词引用了 @{name}，但它不在本片段的参考素材里：生成时这张图不会被送进模型",
                    mention=name,
                )
            )
    for item in refs:
        name = name_by_id.get(str(item.get("asset_id")), "") if name_by_id else ""
        if name and not _used(name) and not _used_by_id(str(item.get("asset_id"))):
            findings.append(
                _info(
                    "refs.unused",
                    f"参考素材「{name}」已绑定但提示词正文没有提到它，生成时可能用不上",
                    mention=name,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def lint_shot(shot: Mapping[str, Any]) -> list[Finding]:
    """单条分镜的结构体检（不涉及提示词）。"""
    findings: list[Finding] = []
    for field in SHOT_FIELDS:
        if field == "duration":
            continue
        if not str(shot.get(field) or "").strip():
            findings.append(
                _warn(
                    "shot.field_missing",
                    f"分镜缺 `{field}`：写到提示词里会变成空描述",
                    field=field,
                    shot_no=shot.get("no"),
                )
            )
    duration = shot.get("duration")
    try:
        value = float(duration) if duration is not None else 0.0
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        findings.append(
            _error("shot.duration", "分镜没有有效时长", shot_no=shot.get("no"))
        )
    return findings


def lint_segment(
    *,
    duration_s: float | None,
    shots: Sequence[Mapping[str, Any]] | None = None,
    prompt: str = "",
    references: Sequence[Mapping[str, Any]] | None = None,
    name_by_id: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """一个片段的完整体检。返回可直接给前端的结构。"""
    dialogue = dialogue_from_shots(shots or [])
    computed = round(sum(float(s.get("duration") or 0) for s in (shots or [])), 2)

    findings: list[Finding] = []
    for shot in shots or []:
        findings.extend(lint_shot(shot))
    findings.extend(check_segment_duration(duration_s, dialogue))
    findings.extend(check_prompt_timeline(prompt, duration_s, single_shot=len(shots or []) <= 1))
    findings.extend(check_references(prompt, references, name_by_id=name_by_id))

    # 分镜时长之和 vs 片段时长：这是最容易出的偏差。
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
        "timeline": parse_timeline(prompt),
        "mentions": extract_mentions(prompt),
        "reference_count": len(list(references or [])),
        "reference_limit": int(THRESHOLDS["reference_limit"]),
    }
