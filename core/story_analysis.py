"""剧本理解：确定性 LLM 链（四卡），非 agent loop。

对照设计 docs/superpowers/specs/2026-10-21-script-asset-pipeline-design.md §3。
为什么不用 agent：产物全部可 schema 校验、无需画布工具、成本可预算、
打断点天然落在卡间。llm 参数 duck-typed（只要 async simple）——测试注入 fake。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

StoryAnalysisCard = Literal["worldview", "synopsis", "episode_outline", "audiovisual_style"]
CARDS: list[StoryAnalysisCard] = ["worldview", "synopsis", "episode_outline", "audiovisual_style"]

_CARD_INSTRUCTION: dict[str, str] = {
    # 世界观 = 设定层（世界如何运作），不是事件层（发生了什么）。
    # 若只写「只写剧本里成立的事实」，模型最安全的策略就是复述剧情——必须显式禁止叙事。
    # 注意：skills/script-analysis/SKILL.md 的「产物口径」与本表须同步修改。
    "worldview": (
        '输出世界观 JSON：{"worldview": "..."}。世界观是**设定层**——这个世界如何运作，'
        '不是剧情——发生了什么。按小节组织：'
        '【时代与地点】【社会与规则】【设定与法则】【势力与组织】【调性关键词】。'
        '每节只写静态事实（是什么/怎么运作/边界在哪），不写任何事件的经过；'
        '禁止出现「故事开始/随后/最终/这时」等叙事词，禁止复述任何一场戏的情节。'
        '正反例（示例仅示意写法）：'
        '✗「教授在阶梯教室授课时，学生发现符号画反了」（这是剧情）；'
        '✓「此世界的雨天会吞没声音，居民以手语交谈」（这是设定——写世界如何运作，不写谁遇到了什么）。'
        '≤800字，只写剧本里成立的设定，不脑补前史。'
    ),
    "synopsis": '基于世界观输出故事梗概 JSON：{"synopsis": "≤600字，主线因果链：欲望→阻碍→代价→结局"}',
    "episode_outline": (
        '输出分集大纲 JSON：{"episode_outline": [{"no": 1, "title": "…", "hook": "本集开头3秒的钩子事件", '
        '"beats": ["按因果顺序，每条一句"]}]}，覆盖剧本全部集数'
    ),
    "audiovisual_style": (
        '基于前三项输出视听风格 JSON：{"audiovisual_style": {"visual": "画面基调", '
        '"camera": "镜头语言倾向", "pacing": "节奏（单镜时长偏好/情绪峰值间隔）"}}'
    ),
}


class StoryAnalysis(BaseModel):
    """meta.story_analysis 的 schema；聊天工具与 pipeline 写入前都过这里。"""

    skill: dict = Field(default_factory=dict)
    updated_at: int = 0
    worldview: str = ""
    synopsis: str = ""
    episode_outline: list[dict] = Field(default_factory=list)
    audiovisual_style: dict = Field(default_factory=dict)


def extract_json(text: str) -> dict:
    """剥掉 ```json 围栏 / 前后缀废话，取第一个平衡的 JSON 对象。"""
    cleaned = re.sub(r"```(?:json)?", "", text or "").strip()
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("回复里没有 JSON 对象")
    depth = 0
    for index in range(start, len(cleaned)):
        if cleaned[index] == "{":
            depth += 1
        elif cleaned[index] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(cleaned[start : index + 1])
    raise ValueError("JSON 未闭合")


def _card_messages(card: StoryAnalysisCard, script_text: str, prior: dict, instruction: str = "") -> list[dict]:
    done = {k: prior[k] for k in CARDS if prior.get(k)}
    prior_text = json.dumps(done, ensure_ascii=False) if done else "（无）"
    system = (
        "你是短剧剧本理解助手。只输出一个 JSON 对象，不要输出解释文字或 markdown 围栏。"
        "不擅自增加剧本没有的剧情事件、人物、前史；台词引用必须是原文。"
    )
    user = f"【剧本全文】\n{script_text}\n\n【已完成产物】\n{prior_text}\n\n【本次任务】\n{instruction or _CARD_INSTRUCTION[card]}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def run_story_analysis(
    script_text: str,
    llm: Any,
    *,
    cards: list[StoryAnalysisCard] | None = None,
    prior: dict | None = None,
    skill_tag: dict | None = None,
    instructions: dict[str, str] | None = None,
    on_event: Callable[[dict], None] | None = None,
    cancel: asyncio.Event | None = None,
) -> dict:
    """逐卡执行；单卡 JSON 解析失败回喂错误重试 1 次，再失败记 card_failed 但继续。

    每卡开始前检查 cancel——已取消则跳过剩余卡，保留 prior 与已完成卡。
    instructions：按卡覆盖默认指令（用户在 UI 里改过的提示词）；缺卡的卡用默认。
    """
    emit = on_event or (lambda event: None)
    todo = list(cards or CARDS)
    overrides = {k: str(v) for k, v in (instructions or {}).items() if str(v or "").strip()}
    analysis = StoryAnalysis(**(prior or {}))
    for card in todo:
        if cancel is not None and cancel.is_set():
            break
        emit({"type": "card_start", "key": card})
        messages = _card_messages(card, script_text, analysis.model_dump(), overrides.get(card, ""))
        data = None
        error = ""
        for attempt in range(2):  # 首次 + 回喂重试 1 次
            call = messages if attempt == 0 else messages + [
                {"role": "user", "content": f"上次回复无法解析：{error}。请只输出一个合法 JSON 对象。"}
            ]
            text = await llm.simple(call)
            # 这里不检查 cancel：回复已经到手，算已完成的工作；
            # 取消的落点在「下一卡开始前」，已完成卡要保留。
            try:
                parsed = extract_json(text)
                if card not in parsed:
                    raise ValueError(f"JSON 里没有「{card}」字段")
                data = parsed[card]
                break
            except (ValueError, json.JSONDecodeError) as exc:
                error = str(exc)
                data = None
        if data is None:
            emit({"type": "card_failed", "key": card, "error": error})
            continue
        setattr(analysis, card, data)
        emit({"type": "card_done", "key": card, "data": data})
    analysis.skill = skill_tag or analysis.skill
    analysis.updated_at = int(time.time())
    result = analysis.model_dump()
    emit({"type": "analysis_done", "analysis": result, "cancelled": bool(cancel and cancel.is_set())})
    return result
