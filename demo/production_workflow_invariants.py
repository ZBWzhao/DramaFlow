"""Deterministic checks for production stages, persistence and bounded repair."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.canvas import Canvas
from core.canvas_repository import FileCanvasRepository
from core.context import ToolContext
from core.events import EventBus
from core.production import ProductionRun, ProductionRunner, ProductionStage, validate_production_stage
from core.production_repository import FileProductionRunRepository, ProductionRunConflict
from core.team import StepResult


def write_node_text(root: Path, canvas: Canvas, node_id: str, text: str) -> None:
    target = root / "assets" / node_id / "content.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    node = canvas.require(node_id)
    node.data["content_path"] = str(target.relative_to(root)).replace("\\", "/")


def make_valid_canvas(root: Path) -> Canvas:
    canvas = Canvas("production-test", root / "production-test.canvas.json")
    canvas.ensure_story("雨夜重逢", "外卖员在便利店遇见失散姐姐")
    script = canvas.add_node("text", "剧本正文", data={"kind": "script"})
    write_node_text(
        root,
        canvas,
        script.id,
        "场1｜深夜便利店｜陈默、陈雨\n"
        + "人物：陈默推门进入，雨水顺着外套滴落。动作：他把外卖放在柜台，看到银手链后停住。"
        + "陈默：这条手链，你从哪里来的？陈雨抬头，手停在半空。" * 8,
    )
    character = canvas.add_node(
        "asset", "角色·陈默",
        data={
            "kind": "character",
            "name": "陈默",
            "anchors": ["左眉旧疤", "黑色短发"],
            "consistency_tags": ["黄色外卖服", "黑色短发", "左眉旧疤"],
            "forbidden": ["不出现现代军装"],
            "spec": "单角度正面全身，平视，中性表情",
        },
    )
    scene = canvas.add_node(
        "asset", "场景·便利店",
        data={
            "kind": "scene",
            "name": "便利店",
            "anchors": ["银色收银台", "玻璃门雨痕"],
            "consistency_tags": ["冷白灯", "玻璃门雨痕", "银色收银台"],
            "forbidden": ["不出现招牌文字"],
            "spec": "单角度总览，平视，冷白顶光",
        },
    )
    board = canvas.add_node(
        "storyboard", "全片分镜",
        data={
            "target_duration": 10,
            "shots": [
                {
                    "no": 1,
                    "segment_no": 1,
                    "duration_s": 5,
                    "shot_size": "中景",
                    "camera": {"height": "与胸口齐平", "angle": "正面 0°", "movement": "跟拍后固定"},
                    "subjects": [
                        {
                            "name": "陈默",
                            "screen_pos": "画面中偏右",
                            "gaze": "看向店内柜台",
                            "start_state": "推门进来",
                            "end_state": "停在门内",
                        }
                    ],
                    "props": [{"name": "外卖袋", "state": "提在右手"}],
                    "action": "陈默推门进入，雨水顺着外套滴落",
                    "focus": "门框与人的交界",
                    "light": {"direction": "店内顶光", "color": "冷白 5600K", "contrast": "中"},
                    "sound": "雨声、门铃",
                    "dialogue": {"text": "", "duration_s": 0},
                    "forbidden": ["不出现第三个人"],
                },
                {
                    "no": 2,
                    "segment_no": 1,
                    "duration_s": 5,
                    "shot_size": "近景",
                    "camera": {"height": "与眼齐平", "angle": "右前侧 45°", "movement": "缓慢推近"},
                    "subjects": [
                        {
                            "name": "陈雨",
                            "screen_pos": "画面中偏左",
                            "gaze": "看向陈默手里的外卖",
                            "start_state": "低头整理货架",
                            "end_state": "抬头停住",
                        }
                    ],
                    "props": [],
                    "action": "陈雨抬头，手停在半空",
                    "focus": "抬眼的瞬间",
                    "light": {"direction": "顶光偏右", "color": "冷白 5600K", "contrast": "中"},
                    "sound": "雨声渐弱",
                    "dialogue": {"text": "这条手链，你从哪里来的？", "duration_s": 3},
                    "forbidden": ["不出现第二个柜台"],
                },
            ],
        },
    )
    prompt = canvas.add_node("prompt", "片段1·雨夜重逢", data={"segment_no": 1})
    write_node_text(
        root,
        canvas,
        prompt.id,
        "镜1｜0-5s｜中景｜{{ref 1}} 推门进入 {{ref 2}}，雨水落地，跟拍后固定；雨声。\n"
        + "镜2｜5-10s｜近景｜{{ref 1}} 在柜台前停住，缓慢推近；环境音渐弱。\n"
        + "风格：写实短剧，冷白荧光，人物位置连续，动作遵循真实物理。"
        + "不要文字水印，不要 logo，不要多余人物，不要改变角色服装与面部特征。" * 12,
    )
    canvas.connect(script.id, "text", board.id, "script")
    canvas.connect(board.id, "shots", prompt.id, "ctx")
    canvas.connect(character.id, "ref", prompt.id, "ref")
    canvas.connect(scene.id, "ref", prompt.id, "ref")
    canvas.save()
    return canvas


async def check_repository(root: Path) -> None:
    repository = FileProductionRunRepository(root / "runs")
    await repository.start()
    run = ProductionRun(
        id=uuid.uuid4().hex,
        session_id="session-test",
        canvas_name="production-test",
        team="dramaTeam",
        user_request="制作一段雨夜重逢短剧",
    )
    created = await repository.create(run.to_dict())
    assert created["revision"] == 1
    created["status"] = "limited"
    saved = await repository.save(created, 1)
    assert saved["revision"] == 2 and saved["status"] == "limited"
    latest = await repository.latest_for_session("session-test")
    assert latest and latest["id"] == run.id
    try:
        await repository.save(created, 1)
    except ProductionRunConflict:
        pass
    else:
        raise AssertionError("stale production revision was accepted")
    await repository.close()


class FakeTeam:
    """No-LLM team that intentionally fails the first script attempt."""

    def __init__(self, root: Path, canvases: FileCanvasRepository) -> None:
        self.root = root
        self.bus = EventBus()
        # 画布仓库是**按会话绑定收窄**的（Agent 只能动自己当前那张画布），
        # 所以测试上下文也要显式声明它工作在哪张画布上。
        context = ToolContext(
            str(root), config={"default_canvas": "runner-test"}, canvas_repository=canvases
        )
        self.manager = SimpleNamespace(parent=SimpleNamespace(context=context))
        self.manifest = SimpleNamespace(
            name="dramaTeam",
            entry="director",
            members={
                "director": SimpleNamespace(
                    delegates=["screenwriter", "asset_designer", "storyboarder", "prompt_writer", "reviewer"]
                )
            },
        )
        self.calls: dict[str, int] = {}

    async def _run_member(self, name: str, task: str, role: str) -> StepResult:
        self.calls[name] = self.calls.get(name, 0) + 1
        canvas = await self.manager.parent.context.canvas_repository.load("runner-test")
        if name == "screenwriter" and self.calls[name] > 1:
            node = canvas.add_node("text", "剧本正文", data={"kind": "script"})
            write_node_text(self.root, canvas, node.id, "场1｜房间｜甲\n人物甲起身。甲：开始制作。" * 20)
            await self.manager.parent.context.canvas_repository.save(canvas)
        elif name == "asset_designer":
            canvas.add_node(
                "asset", "角色·甲",
                data={
                    "kind": "character",
                    "name": "甲",
                    "anchors": ["黑色短发", "眉骨高"],
                    "consistency_tags": ["黑发", "蓝衣", "青年"],
                    "forbidden": ["不出现现代军装"],
                    "spec": "单角度正面全身，平视，中性表情",
                },
            )
            canvas.add_node(
                "asset", "场景·房间",
                data={
                    "kind": "scene",
                    "name": "房间",
                    "anchors": ["木桌", "白墙"],
                    "consistency_tags": ["木桌", "暖光", "白墙"],
                    "forbidden": ["不出现窗外景物"],
                    "spec": "单角度总览，平视，暖光",
                },
            )
            await self.manager.parent.context.canvas_repository.save(canvas)
        elif name == "storyboarder":
            script = next(node for node in canvas.nodes.values() if node.type == "text")
            board = canvas.add_node(
                "storyboard", "全片分镜",
                data={
                    "target_duration": 5,
                    "shots": [
                        {
                            "no": 1,
                            "segment_no": 1,
                            "duration_s": 5,
                            "shot_size": "中景",
                            "camera": {"height": "与胸口齐平", "angle": "正面 0°", "movement": "固定"},
                            "subjects": [
                                {
                                    "name": "甲",
                                    "screen_pos": "画面中央",
                                    "gaze": "看向木桌",
                                    "start_state": "坐在桌边",
                                    "end_state": "站起",
                                }
                            ],
                            "props": [{"name": "木桌", "state": "在画面中景"}],
                            "action": "甲从桌边起身",
                            "focus": "起身的腰线",
                            "light": {"direction": "右侧窗光", "color": "暖黄 3200K", "contrast": "中"},
                            "sound": "环境安静",
                            "dialogue": {"text": "开始制作。", "duration_s": 2},
                            "forbidden": ["不出现第二个人"],
                        }
                    ],
                },
            )
            canvas.connect(script.id, "text", board.id, "script")
            await self.manager.parent.context.canvas_repository.save(canvas)
        elif name == "prompt_writer":
            board = next(node for node in canvas.nodes.values() if node.type == "storyboard")
            assets = [node for node in canvas.nodes.values() if node.type == "asset"]
            prompt = canvas.add_node("prompt", "片段1", data={"segment_no": 1})
            write_node_text(
                self.root, canvas, prompt.id,
                ("镜1｜0-5s｜中景｜{{ref 1}} 在 {{ref 2}} 起身；固定镜头；自然台词。"
                 "写实风格，暖光，动作连续，符合真实物理，不要水印、字幕、logo 或多余人物。") * 12,
            )
            canvas.connect(board.id, "shots", prompt.id, "ctx")
            for asset in assets:
                canvas.connect(asset.id, "ref", prompt.id, "ref")
            await self.manager.parent.context.canvas_repository.save(canvas)
        if name == "director" and role == "plan":
            answer = (
                "总时长严格为 5 秒，画幅 9:16，写实风格。验收标准：结构化分镜时长合计为 5 秒，"
                "每个片段有正式提示词，角色和场景都具备稳定资产特征。"
            )
        else:
            answer = f"{name} 已处理：{task[:80]}"
        return StepResult(member=name, role=role, answer=answer)


async def check_runner(root: Path) -> None:
    canvases = FileCanvasRepository(root)
    await canvases.start()
    await canvases.save(Canvas("runner-test", root / "runner-test.canvas.json"))
    runs = FileProductionRunRepository(root / "runs-runner")
    await runs.start()
    team = FakeTeam(root, canvases)
    result = await ProductionRunner(
        team, runs, session_id="runner-session", canvas_name="runner-test"
    ).run("制作一段 5 秒短剧")
    assert result.status == "waiting_approval"
    assert team.calls["screenwriter"] == 2, team.calls
    stored = await runs.get(result.production_run_id or "")
    assert stored and stored["stages"]["scripting"]["attempts"] == 2
    assert stored["stages"]["scripting"]["status"] == "succeeded"
    assert stored["status"] == "waiting_approval"
    prepared = await canvases.load("runner-test")
    assert len(prepared.generation_jobs()) == 1
    assert len(prepared.timeline_jobs()) == 1
    assert prepared.timeline_jobs()[0]["clips"][0]["node_id"] == prepared.generation_jobs()[0]["node_id"]
    event_types = [event.type for event in team.bus.log]
    assert "production_stage_retry" in event_types
    assert event_types[-1] == "production_waiting_approval"
    await runs.close()
    await canvases.close()


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="dramaflow-production-") as directory:
        root = Path(directory)
        canvas = make_valid_canvas(root)
        plan = (
            "总时长严格控制为 10 秒；采用 9:16 竖屏画幅；整体是冷色写实短剧风格。"
            "验收标准：分镜镜头时长合计必须等于 10 秒，每个片段都有正式提示词，"
            "所有角色与场景引用均能对应到包含稳定特征的资产节点。"
        )
        assert validate_production_stage(ProductionStage.PLANNING, canvas, plan).passed
        for stage in (
            ProductionStage.SCRIPTING,
            ProductionStage.ASSET_DESIGN,
            ProductionStage.STORYBOARDING,
            ProductionStage.PROMPTING,
            ProductionStage.REVIEWING,
        ):
            result = validate_production_stage(stage, canvas)
            assert result.passed, (stage, result.problems)

        # A real persisted defect must stop the relevant stage even if an Agent claims success.
        canvas.require(next(node.id for node in canvas.nodes.values() if node.type == "storyboard")).data["target_duration"] = 12
        failed = validate_production_stage(ProductionStage.STORYBOARDING, canvas)
        assert not failed.passed and any("时长" in item for item in failed.problems)

        await check_repository(root)
        await check_runner(root / "runner")
        print("PASS production contracts + bounded repair + checkpoints + revision CAS")


if __name__ == "__main__":
    asyncio.run(main())
