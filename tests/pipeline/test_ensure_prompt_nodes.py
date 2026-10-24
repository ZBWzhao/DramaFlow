"""produce 收尾兜底：分镜已落盘时，片段/生成槽由服务端确定性补齐，不指望模型。

背景（2026-10-24 复盘）：demo-media 画布上 Agent 连续多轮在「原位更新已有分镜」
上耗尽轮次（40 轮只够 set_storyboard 几十次），指令第 4/5 步（建 prompt /
video_gen）从未执行，制作页片段队列永远是 1（种子）。正文本来就由服务端按
shots 渲染（_sync_rendered_prompts），建空片段节点是机械活，不该花模型轮次。
"""
import sys
from pathlib import Path

from core.canvas import Canvas

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.studio import _ensure_prompt_nodes, _produce_progress  # noqa: E402


def _canvas_with_storyboard() -> tuple[Canvas, any]:
    canvas = Canvas("t")
    sb = canvas.add_node(
        "storyboard",
        "分镜01·开场",
        data={
            "episode_no": 1,
            "segment_no": 1,
            "shots": [{
                "no": 1, "duration_s": 4.0, "subject": "林生推门", "shot_size": "中景",
                "camera": {"height": "齐腰", "angle": "正面", "movement": "固定"},
            }],
        },
    )
    return canvas, sb


def test_ensure_creates_prompt_and_videogen_with_edges():
    canvas, sb = _canvas_with_storyboard()
    created = _ensure_prompt_nodes(canvas, 1)
    assert created == 2, "改动计数 = 1 个片段 + 1 个生成槽（与 _repair_links 的口径一致）"
    prompt = next(n for n in canvas.nodes.values() if n.type == "prompt")
    assert prompt.data["segment_no"] == 1 and prompt.data["episode_no"] == 1
    assert prompt.data["duration_s"] == 4.0
    assert prompt.data["rendered_by"] == "server"
    gen = next(n for n in canvas.nodes.values() if n.type == "video_gen")
    edges = {(e.source, e.target) for e in canvas.edges}
    assert (sb.id, prompt.id) in edges, "分镜→片段的 shots/ctx 边必须由系统补齐"
    assert (prompt.id, gen.id) in edges, "片段→视频生成槽必须由系统补齐"


def test_ensure_is_idempotent():
    canvas, _ = _canvas_with_storyboard()
    _ensure_prompt_nodes(canvas, 1)
    before = len(canvas.nodes)
    assert _ensure_prompt_nodes(canvas, 1) == 0
    assert len(canvas.nodes) == before


def test_ensure_backfills_missing_videogen_for_existing_prompt():
    canvas, sb = _canvas_with_storyboard()
    prompt = canvas.add_node("prompt", "分镜01·已有片段", data={"episode_no": 1, "segment_no": 1})
    canvas.connect(sb.id, "shots", prompt.id, "ctx")
    assert _ensure_prompt_nodes(canvas, 1) == 1, "已有片段缺生成槽时应只补 video_gen"
    gens = [n for n in canvas.nodes.values() if n.type == "video_gen"]
    assert len(gens) == 1
    assert any(e.source == prompt.id and e.target == gens[0].id for e in canvas.edges)
    assert sum(1 for n in canvas.nodes.values() if n.type == "prompt") == 1, "不重复建片段"


def test_ensure_skips_orphaned_and_other_episodes():
    canvas, sb = _canvas_with_storyboard()
    sb.data["orphaned"] = True
    assert _ensure_prompt_nodes(canvas, 1) == 0
    assert not [n for n in canvas.nodes.values() if n.type in ("prompt", "video_gen")]
    sb.data["orphaned"] = False
    sb.data["episode_no"] = 2
    assert _ensure_prompt_nodes(canvas, 1) == 0


def test_progress_counts_prompts_against_storyboards():
    canvas, _ = _canvas_with_storyboard()
    progress = _produce_progress(canvas, 1)
    assert progress["storyboards"] == 1 and progress["prompts"] == 0
    assert progress["percent"] == 0
    _ensure_prompt_nodes(canvas, 1)
    progress = _produce_progress(canvas, 1)
    assert progress["prompts"] == 1 and progress["total"] == 1 and progress["percent"] == 100
