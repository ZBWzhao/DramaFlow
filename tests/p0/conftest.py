"""P0 复现夹具。

目标：在**不改业务代码**的前提下，把画布生成链路的关键状态机跑起来，
让 P0-1（Worker 写回被并发编辑打回）、P0-2（写接口不要求 revision）变成可执行的
RED 测试；修复后同一批测试应转 GREEN。

隔离原则：
- 每个测试一个临时 workspace（画布文件、生成产物、会话目录全在 tmp 内）；
- Provider 用 FakeProvider 替换 ``server.app.default_registry``，不联网、不花钱；
- 任务存储用内存实现（EVENT_STORE_BACKEND=memory），不起 Redis/Postgres；
- 通过 Starlette TestClient 触发真实 HTTP 路由与真实嵌入式 Worker。

运行：``python -m pytest tests/p0 -q``
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# 必须在 import server.app 之前设置：Settings.load() 缺 key 会 SystemExit。
os.environ.setdefault("DEEPSEEK_API_KEY", "p0-test-key")
os.environ.setdefault("CANVAS_STORE_BACKEND", "file")
os.environ.setdefault("EVENT_STORE_BACKEND", "memory")
os.environ.setdefault("MEDIA_WORKER_EMBEDDED", "true")
os.environ.setdefault("MEDIA_JOB_LEASE_SECONDS", "30")
for _name in ("DATABASE_URL", "REDIS_URL"):
    os.environ.pop(_name, None)

TERMINAL_STATUSES = {"succeeded", "failed", "unknown", "cancelled", "discarded"}

# 一个"最小可用"的短剧画布：提示词卡 → 视频生成卡。
# 只连 STRING 端口，不带参考图，保证 generation_jobs() 里 ready=True。
PROMPT_TEXT = "雨夜霓虹街道，女孩撑伞走过积水，镜头缓慢推进，电影感，35mm。"
PROMPT_CONTENT_PATH = "assets/prompt-1/content.md"


class FakeProvider:
    """替身 Provider：遵守 ``core.media`` 的 MediaProvider 协议。

    ``generate`` 在临时 workspace 里写一个占位文件并返回 MediaResult，
    这样 Worker 的"落地产物 → 写 take → 回写画布"整段逻辑都会真实执行。
    """

    name = "mock"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.delay = 0.0
        self.fail_with: Exception | None = None
        self.active = "mock"

    # --- MediaProvider 协议 -------------------------------------------------
    def list_models(self) -> list[dict[str, Any]]:
        return [
            {"id": "fake-image-v1", "kind": "image", "provider": "mock", "label": "Fake Image"},
            {"id": "fake-video-v1", "kind": "video", "provider": "mock", "label": "Fake Video"},
        ]

    def describe(self) -> list[dict[str, Any]]:
        return self.list_models()

    def activate(self, name: str = "mock") -> str:
        return name

    def generate(self, kind: str, request: dict[str, Any], root: Path) -> Any:
        from core.media import MediaResult

        self.calls.append({"kind": kind, "request": request})
        if self.delay:
            time.sleep(self.delay)
        if self.fail_with is not None:
            raise self.fail_with
        ext = "png" if kind == "image" else "mp4"
        relative = f"assets/generated/fake-{uuid.uuid4().hex[:12]}.{ext}"
        target = Path(root) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x89PNG\r\n\x1a\nP0" if kind == "image" else b"\x00\x00\x00\x18ftypmp42P0")
        return MediaResult(
            kind=kind,
            path=relative,
            mime="image/png" if kind == "image" else "video/mp4",
            provider="mock",
            mock=True,
            meta={},
        )

    def resume(self, kind: str, operation_id: str, request: dict[str, Any], root: Path) -> Any:
        return self.generate(kind, request, root)


class P0Env:
    """一个测试用到的全部句柄。"""

    def __init__(
        self,
        *,
        tmp: Path,
        workspace: Path,
        app_module: Any,
        client: Any,
        provider: FakeProvider,
        canvas: Any,
        repository: Any,
        prompt_id: str,
        image_id: str,
        video_id: str,
        timeline_id: str | None = None,
    ) -> None:
        self.tmp = tmp
        self.workspace = workspace
        self.app_module = app_module
        self.client = client
        self.provider = provider
        self.canvas = canvas
        self.repository = repository
        self.prompt_id = prompt_id
        self.image_id = image_id
        self.video_id = video_id
        self.timeline_id = timeline_id

    # --- HTTP helpers -------------------------------------------------------
    def revision(self, name: str = "main") -> int:
        response = self.client.get("/api/canvas", params={"name": name})
        assert response.status_code == 200, response.text
        return int(response.json()["revision"])

    def canvas_doc(self, name: str = "main") -> dict[str, Any]:
        response = self.client.get("/api/canvas", params={"name": name})
        assert response.status_code == 200, response.text
        return response.json()

    def canvas_node(self, node_id: str) -> dict[str, Any]:
        for node in self.canvas_doc()["nodes"]:
            if node["id"] == node_id:
                return node
        raise AssertionError(f"节点不存在：{node_id}")

    def delete_node(self, node_id: str, revision: int | None = None, *, name: str = "main"):
        """删节点：revision 缺省就不传（用于验证严格校验），传了就带上。"""
        params: dict[str, Any] = {"canvas": name}
        if revision is not None:
            params["revision"] = revision
        return self.client.delete(f"/api/canvas/nodes/{node_id}", params=params)

    # --- 生成链路 helpers ---------------------------------------------------
    def approve(self, node_ids: list[str], provider: str = "mock") -> str:
        response = self.client.post(
            "/api/canvas/approvals",
            json={
                "canvas": "main",
                "only": node_ids,
                "provider": provider,
                "revision": self.revision(),
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body.get("approval_id"), body
        return str(body["approval_id"])

    def run(self, node_ids: list[str], approval_id: str, provider: str = "mock") -> str:
        response = self.client.post(
            "/api/canvas/run",
            json={
                "canvas": "main",
                "only": node_ids,
                "provider": provider,
                "approved": True,
                "approval_id": approval_id,
                "duration": 5,
                "resolution": "720p",
                "ratio": "16:9",
                "generate_audio": False,
                "revision": self.revision(),
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body.get("job_id"), body
        return str(body["job_id"])

    def job(self, job_id: str) -> dict[str, Any]:
        response = self.client.get(f"/api/media/jobs/{job_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def wait_job(self, job_id: str, timeout: float = 20.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        last: dict[str, Any] = {}
        while time.time() < deadline:
            last = self.job(job_id)
            if last.get("status") in TERMINAL_STATUSES:
                return last
            time.sleep(0.1)
        raise AssertionError(f"媒体任务在 {timeout}s 内没有进入终态，最后状态：{last}")

    # --- 竞态放大 -----------------------------------------------------------
    def inflate_competing_edit_before_writeback(self, editor_node_id: str) -> dict[str, Any]:
        """把"生成期间用户拖动节点"放大到必现。

        真实时序里 Worker 在写回前会重新 load 画布，冲突窗口只有毫秒级；
        这里在 Worker 将要落盘"done + take"的那一刻，先让另一位写者
        （相当于用户拖了一下节点）提交一版，精确复现 TOCTOU：
        Worker 拿着旧 revision 写回 → CanvasConflictError。

        返回触发状态字典（``fired`` 表明注入是否发生）。
        """
        repo = self.app_module.registry.canvas_repository
        real_save = repo.save
        state: dict[str, Any] = {"fired": False}

        async def save_with_competing_edit(canvas: Any) -> Any:
            node = canvas.nodes.get(self.video_id)
            if (
                not state["fired"]
                and node is not None
                and node.status == "done"
                and node.data.get("current_take_id")
            ):
                state["fired"] = True
                other = await repo.load("main")
                other.update_node(editor_node_id, position={"x": 999.0, "y": 999.0})
                await real_save(other)  # 另一位写者先提交，revision 前进
            return await real_save(canvas)

        setattr(repo, "save", save_with_competing_edit)
        return state

    def supersede_generation_before_writeback(self, node_id: str, new_generation_id: str = "gen_newer") -> dict[str, Any]:
        """把"生成期间用户又点了一次生成"放大到必现（防串代保护的反向用例）。

        在旧任务将要落盘"done + take"的瞬间，让另一位写者把该节点的
        ``data.active_generation_id`` 换成新一代：旧任务的结果必须整份丢弃，
        不能覆盖新一代任务 —— 这是 P0-1 "合并式写回"不能破坏的语义。
        """
        repo = self.app_module.registry.canvas_repository
        real_save = repo.save
        state: dict[str, Any] = {"fired": False}

        async def save_with_newer_generation(canvas: Any) -> Any:
            node = canvas.nodes.get(node_id)
            if (
                not state["fired"]
                and node is not None
                and node.status == "done"
                and node.data.get("current_take_id")
            ):
                state["fired"] = True
                other = await repo.load("main")
                other.update_node(
                    node_id,
                    status="generating",
                    data={"active_generation_id": new_generation_id},
                )
                await real_save(other)  # 新一代任务先提交
            return await real_save(canvas)

        setattr(repo, "save", save_with_newer_generation)
        return state


def build_minimal_canvas(workspace: Path) -> tuple[Any, str, str, str]:
    """建一张"提示词卡 → 视频生成卡"的最小画布并落盘。"""
    from core.canvas import Canvas

    prompt_file = workspace / PROMPT_CONTENT_PATH
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(PROMPT_TEXT, encoding="utf-8")

    canvas = Canvas("main", path=workspace / "main.canvas.json")
    prompt = canvas.add_node(
        "prompt",
        "片段 1 提示词",
        position={"x": 0.0, "y": 0.0},
        data={"content_path": PROMPT_CONTENT_PATH, "segment_no": 1},
    )
    image = canvas.add_node("image_gen", "关键帧", position={"x": 320.0, "y": 0.0})
    video = canvas.add_node("video_gen", "镜头 1 视频", position={"x": 640.0, "y": 0.0})
    # 只连提示词端口：没有参考图也能生成（当前服务端语义的唯一 ready 路径）。
    canvas.connect(prompt.id, "prompt", image.id, "prompt")
    canvas.connect(prompt.id, "prompt", video.id, "prompt")
    canvas.save()
    return canvas, prompt.id, image.id, video.id


def build_compose_canvas(workspace: Path) -> tuple[Any, str, str, str, str]:
    """建一张"提示词卡 → 视频生成卡 → 时间线"的画布，用于合成（compose）链路测试。

    时间线只要求 clip 的 path 非空（``Canvas.timeline_jobs`` 的 ready 判定），
    真正的媒体探测由 ``composition_service.inspect`` 负责，测试里会替换掉它，
    因此这里写一个占位文件即可，不需要真的编码视频。
    """
    from core.canvas import Canvas

    prompt_file = workspace / PROMPT_CONTENT_PATH
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(PROMPT_TEXT, encoding="utf-8")

    clip_path = "assets/generated/clip1.mp4"
    clip_file = workspace / clip_path
    clip_file.parent.mkdir(parents=True, exist_ok=True)
    clip_file.write_bytes(b"\x00\x00\x00\x18ftypmp42P0")

    canvas = Canvas("main", path=workspace / "main.canvas.json")
    prompt = canvas.add_node(
        "prompt",
        "片段 1 提示词",
        position={"x": 0.0, "y": 0.0},
        data={"content_path": PROMPT_CONTENT_PATH, "segment_no": 1},
    )
    image = canvas.add_node("image_gen", "关键帧", position={"x": 320.0, "y": 0.0})
    video = canvas.add_node("video_gen", "镜头 1 视频", position={"x": 640.0, "y": 0.0})
    canvas.connect(prompt.id, "prompt", image.id, "prompt")
    canvas.connect(prompt.id, "prompt", video.id, "prompt")

    video.outputs["video"] = {
        "data_type": "VIDEO",
        "value": {"path": clip_path, "mime": "video/mp4", "mock": True, "provider": "mock"},
    }
    timeline = canvas.add_node(
        "timeline",
        "成片",
        position={"x": 960.0, "y": 0.0},
        data={"clip_order": [video.id], "output": {"width": 1280, "height": 720, "fps": 24}},
    )
    canvas.connect(video.id, "video", timeline.id, "video")
    canvas.save()
    return canvas, prompt.id, image.id, video.id, timeline.id


@contextlib.contextmanager
def _p0_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, builder: Any):
    """完整 P0 环境：临时 workspace + FakeProvider + 内存任务存储 + TestClient。"""
    from fastapi.testclient import TestClient

    import server.app as app_module
    from core.canvas_repository import create_canvas_repository
    from core.composition import CompositionService
    from core.media_job_store import create_media_job_store
    from core.session_repository import JsonlSessionRepository
    from core.tts import TTSRegistry
    from server.registry import SessionRegistry

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    built = builder(workspace)
    canvas, prompt_id, image_id, video_id = built[:4]
    timeline_id = built[4] if len(built) > 4 else None

    provider = FakeProvider()
    jobs = create_media_job_store()
    registry = SessionRegistry(
        llm=None,  # P0 测试不调用模型
        workspace=str(workspace),
        canvas_repository=create_canvas_repository(workspace),
        session_repository=JsonlSessionRepository(tmp_path / "sessions"),
    )

    monkeypatch.setattr(app_module, "WORKSPACE", workspace)
    monkeypatch.setattr(app_module, "default_registry", provider)
    monkeypatch.setattr(app_module, "media_jobs", jobs)
    monkeypatch.setattr(app_module, "registry", registry)
    monkeypatch.setattr(app_module, "composition_service", CompositionService(workspace))
    monkeypatch.setattr(app_module, "tts_registry", TTSRegistry(workspace))

    # 制片台路由在 import 阶段就用「真实 registry/workspace」装配过一次
    # （server/app.py:2394 studio.configure(...)），测试必须重新注入临时环境，
    # 否则 /api/studio/* 会去读写仓库里的真实 workspace。
    from server import studio as studio_module

    studio_module.configure(registry=registry, workspace=workspace, media_registry=provider)

    with TestClient(app_module.app) as client:
        yield P0Env(
            tmp=tmp_path,
            workspace=workspace,
            app_module=app_module,
            client=client,
            provider=provider,
            canvas=canvas,
            repository=registry.canvas_repository,
            prompt_id=prompt_id,
            image_id=image_id,
            video_id=video_id,
            timeline_id=timeline_id,
        )


@pytest.fixture()
def p0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with _p0_client(tmp_path, monkeypatch, builder=build_minimal_canvas) as env:
        yield env


@pytest.fixture()
def p0_compose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with _p0_client(tmp_path, monkeypatch, builder=build_compose_canvas) as env:
        yield env
