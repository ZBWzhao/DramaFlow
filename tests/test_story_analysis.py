"""剧本理解确定性链：逐卡事件、JSON 解析重试一次、卡间可打断、prior 保留。"""
import asyncio
import json

from core.story_analysis import CARDS, run_story_analysis


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def simple(self, messages):
        self.calls.append(messages)
        return self.replies.pop(0)


def _ok(card):
    if card == "episode_outline":
        return json.dumps(
            {"episode_outline": [{"no": 1, "title": "第1集", "hook": "开门撞尸", "beats": ["a"]}]},
            ensure_ascii=False,
        )
    if card == "audiovisual_style":
        return json.dumps({"audiovisual_style": {"visual": "写实", "camera": "手持", "pacing": "快"}}, ensure_ascii=False)
    return json.dumps({card: f"{card}内容"}, ensure_ascii=False)


def test_runs_all_cards_in_order_and_emits_events():
    llm = FakeLLM([_ok(c) for c in CARDS])
    events = []
    result = asyncio.run(run_story_analysis("剧本全文", llm, on_event=events.append))
    starts = [e["key"] for e in events if e["type"] == "card_start"]
    dones = [e["key"] for e in events if e["type"] == "card_done"]
    assert starts == list(CARDS) and dones == list(CARDS)
    assert events[-1]["type"] == "analysis_done"
    assert result["worldview"] == "worldview内容"
    assert result["episode_outline"][0]["hook"] == "开门撞尸"
    assert result["skill"] == {} and result["updated_at"] > 0


def test_json_fences_are_stripped():
    llm = FakeLLM(["```json\n" + _ok("worldview") + "\n```", *[_ok(c) for c in CARDS[1:]]])
    result = asyncio.run(run_story_analysis("剧", llm))
    assert result["worldview"] == "worldview内容"


def test_retries_once_on_bad_json_then_succeeds():
    replies = ["这不是JSON", _ok("worldview"), _ok("synopsis"), _ok("episode_outline"), _ok("audiovisual_style")]
    llm = FakeLLM(replies)
    events = []
    result = asyncio.run(run_story_analysis("剧", llm, on_event=events.append))
    assert not any(e["type"] == "card_failed" for e in events)
    assert result["worldview"] == "worldview内容"  # 第二次成功
    assert len(llm.calls[0]) == 2  # 首次调用只有 system+user
    assert len(llm.calls[1]) == 3  # 重试回喂了解析错误


def test_failed_card_does_not_block_rest():
    replies = ["坏", "还是坏", _ok("synopsis"), _ok("episode_outline"), _ok("audiovisual_style")]
    events = []
    result = asyncio.run(run_story_analysis("剧", FakeLLM(replies), on_event=events.append))
    failed = [e for e in events if e["type"] == "card_failed"]
    assert [e["key"] for e in failed] == ["worldview"]
    assert result["worldview"] == "" and result["synopsis"] == "synopsis内容"


def test_cancel_between_cards_keeps_finished_cards_and_prior():
    llm = FakeLLM([_ok(c) for c in CARDS])
    cancel = asyncio.Event()
    cancel.set()  # 入口即取消：一张卡都不该跑
    result = asyncio.run(
        run_story_analysis("剧", llm, prior={"worldview": "已有世界观"}, on_event=lambda e: None, cancel=cancel)
    )
    assert llm.calls == []  # 没有任何调用
    assert result["worldview"] == "已有世界观"  # prior 保留
    assert result["synopsis"] == ""


def test_partial_cards_only_runs_requested():
    llm = FakeLLM([_ok("worldview")])
    result = asyncio.run(run_story_analysis("剧", llm, cards=["worldview"]))
    assert result["worldview"] == "worldview内容"
    assert result["synopsis"] == ""
    assert len(llm.calls) == 1
