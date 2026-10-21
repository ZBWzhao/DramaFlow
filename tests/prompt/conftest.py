"""提示词侧（T1/T2/T3）测试夹具：直接调制片台函数，不起 HTTP、不联网、不花钱。

隔离原则：
- 每个测试一个临时 workspace，画布文件全在 tmp 内；
- ``studio.configure`` 注入临时 registry，绝不读写仓库里的真实 workspace；
- ``_run_generation`` 换成空实现：只关心「发出去的 request」，不关心出片。

运行：``python -m pytest tests/prompt -q``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.canvas import Canvas
from core.canvas_repository import create_canvas_repository
from server import studio

ASSET_NAME = "林直人"

SHOT: dict[str, Any] = {
    "no": 1,
    "segment_no": 1,
    "duration_s": 5,
    "shot_size": "中景",
    "camera": {"height": "与胸口齐平", "angle": "左前侧 30°", "movement": "固定"},
    "subjects": [
        {
            "id": "",
            "name": ASSET_NAME,
            "screen_pos": "画面左三分之一",
            "gaze": "看向对方喉咙",
            "start_state": "手垂在身侧",
            "end_state": "手指收拢",
        }
    ],
    "props": [],
    "action": "抬手又放下",
    "focus": "手上的泥",
    "light": {"direction": "侧逆光", "color": "暖橙 2700K", "contrast": "高"},
    "sound": "雨声",
    "dialogue": {"text": "别动。", "duration_s": 2},
    "forbidden": ["不出现第三个人"],
}


class StubRegistry:
    """studio 只用到 canvas_repository（load/save）。"""

    def __init__(self, repository: Any) -> None:
        self.canvas_repository = repository


class StudioEnv:
    def __init__(self, *, canvas: Canvas, prompt_id: str, asset_id: str, gen_id: str) -> None:
        self.canvas = canvas
        self.prompt_id = prompt_id
        self.asset_id = asset_id
        self.gen_id = gen_id

    # --- 画布状态操纵 ------------------------------------------------------
    def save(self) -> None:
        self.canvas.save()

    def node(self) -> Any:
        return self.canvas.nodes[self.prompt_id]

    def set_human_text(self, text: str) -> None:
        """等价于保存过一次人改正文（save_prompt 的落库结果）。"""
        node = self.node()
        node.data["rendered_by"] = "human"
        node.data["prompt"] = text
        self.save()

    def set_keyframe(self, path: str = "assets/kf.png") -> None:
        self.canvas.nodes[self.gen_id].data["keyframe"] = {"path": path, "shot_no": 1}
        self.save()

    def drop_ref_edges(self) -> None:
        self.canvas.edges = [edge for edge in self.canvas.edges if edge.target != self.prompt_id]
        self.save()

    # --- 生成 --------------------------------------------------------------
    def generate(self, **body: Any) -> dict[str, Any]:
        """调用真实 generate_segment，返回 job（含真正发给 provider 的 prompt）。"""
        # 每个测试用新的 asyncio.run，画布锁不能跨事件循环复用。
        studio._canvas_locks.clear()

        async def run() -> dict[str, Any]:
            try:
                return await studio.generate_segment(
                    self.prompt_id, self.canvas.name, studio.GenerateRequest(**body)
                )
            finally:
                # 让 _run_generation 的替身任务在这个循环里跑完，避免 pending 警告。
                for _ in range(3):
                    await asyncio.sleep(0)

        return asyncio.run(run())

    def preview(self) -> dict[str, Any]:
        return studio._segment_view(self.canvas, self.node(), with_detail=True)


async def _noop_generation(*args: Any, **kwargs: Any) -> None:
    """不真生成：测试只断言发出去的 request。"""


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StudioEnv:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    canvas = Canvas(name="main", path=workspace / "main.canvas.json")
    asset = canvas.add_node("asset", ASSET_NAME)
    asset.data.update(
        {
            "kind": "character",
            "name": ASSET_NAME,
            "anchors": ["左虎口旧疤"],
            "consistency_tags": ["湿透的灰蓝短褂"],
            "forbidden": ["不换脸"],
            "spec": "外观锚点：瘦削，湿透的灰蓝短褂",
            "looks": [{"id": "look_1", "image_path": "assets/look-1.png"}],
        }
    )
    board = canvas.add_node("storyboard", "第1集 分镜")
    prompt = canvas.add_node("prompt", "第1集 片段1")
    gen = canvas.add_node("video_gen", "片段1 视频")

    shot = dict(SHOT)
    shot["subjects"] = [{**SHOT["subjects"][0], "id": asset.id}]
    board.data.update({"shots": [shot], "segment_no": 1, "episode_no": 1})
    prompt.data.update({"segment_no": 1, "episode_no": 1, "duration_s": 5})
    canvas.meta["project"] = {
        "video_model": "Seedance 2.0 mini",
        "aspect_ratio": "16:9",
        "quality": "720P",
    }

    canvas.connect(board.id, "shots", prompt.id, "ctx")
    canvas.connect(asset.id, "ref", prompt.id, "ref")
    canvas.connect(prompt.id, "prompt", gen.id, "prompt")
    canvas.save()

    monkeypatch.setattr(studio, "_run_generation", _noop_generation)
    studio.configure(
        registry=StubRegistry(create_canvas_repository(workspace)),
        workspace=workspace,
        media_registry=None,
    )
    return StudioEnv(canvas=canvas, prompt_id=prompt.id, asset_id=asset.id, gen_id=gen.id)
