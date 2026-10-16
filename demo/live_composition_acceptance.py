"""Black-box acceptance test for the running composition API.

The test deliberately uses the HTTP service rather than importing the FastAPI
app.  It verifies the same path used by the Vue client: upload, canvas edits,
optimistic-lock rejection, approval, background work, asset download and media
stream validation.  All test artifacts are given a unique ``qa_`` canvas name
and are removed in ``finally``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
import sys

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.composition import ffmpeg_capabilities, probe_media  # noqa: E402


WORKSPACE = ROOT / "workspace"
BASE_URL = os.environ.get("DRAMAFLOW_URL", "http://127.0.0.1:8000").rstrip("/")


def request(method: str, path: str, *, expected: int = 200, **kwargs: Any) -> requests.Response:
    response = requests.request(method, f"{BASE_URL}{path}", timeout=30, **kwargs)
    if response.status_code != expected:
        raise AssertionError(
            f"{method} {path}: expected {expected}, got {response.status_code}: {response.text[:1000]}"
        )
    return response


def create_clip(path: Path, color: str) -> None:
    ffmpeg = str(ffmpeg_capabilities()["ffmpeg"])
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=640x360:r=24:d=1.25",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def remove_test_artifacts(canvas_name: str, paths: set[str], node_ids: set[str]) -> None:
    root = WORKSPACE.resolve()
    for relative in sorted(paths, reverse=True):
        target = (root / relative).resolve()
        target.relative_to(root)
        if target.is_file():
            target.unlink()
        parent = target.parent
        while parent != root and parent.name not in {"uploads", "generated", "rendered", "assets"}:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    for node_id in node_ids:
        upload_dir = (root / "assets" / "uploads" / node_id).resolve()
        upload_dir.relative_to(root)
        if upload_dir.is_dir():
            shutil.rmtree(upload_dir)
    canvas_file = (root / f"{canvas_name}.canvas.json").resolve()
    canvas_file.relative_to(root)
    if canvas_file.is_file():
        canvas_file.unlink()


def main() -> None:
    health = request("GET", "/api/health").json()
    assert health["ffmpeg"]["available"], health["ffmpeg"]
    canvas_name = f"qa_{uuid.uuid4().hex[:10]}"
    revision = 0
    created_paths: set[str] = set()
    node_ids: set[str] = set()

    try:
        with tempfile.TemporaryDirectory(prefix="dramaflow-live-qa-") as temp_name:
            temp = Path(temp_name)
            clip_a = temp / "clip-a.mp4"
            clip_b = temp / "clip-b.mp4"
            create_clip(clip_a, "red")
            create_clip(clip_b, "blue")

            video_nodes: list[str] = []
            for index, clip in enumerate((clip_a, clip_b)):
                response = request(
                    "POST",
                    "/api/canvas/upload-media",
                    params={
                        "canvas": canvas_name,
                        "kind": "video",
                        "filename": clip.name,
                        "x": 80 + index * 300,
                        "y": 80,
                        "revision": revision,
                    },
                    headers={"Content-Type": "video/mp4"},
                    data=clip.read_bytes(),
                ).json()
                revision = response["revision"]
                video_nodes.append(response["node_id"])
                node_ids.add(response["node_id"])
                created_paths.add(response["path"])

            def add_node(node_type: str, label: str, x: int, y: int, data: dict[str, Any]) -> str:
                nonlocal revision
                response = request(
                    "POST",
                    "/api/canvas/nodes",
                    json={
                        "canvas": canvas_name,
                        "type": node_type,
                        "label": label,
                        "x": x,
                        "y": y,
                        "data": data,
                        "revision": revision,
                    },
                ).json()
                revision = response["revision"]
                node_ids.add(response["node_id"])
                return response["node_id"]

            tts_id = add_node(
                "tts_gen",
                "验收旁白",
                700,
                300,
                {"text": "红色第一幕开始。蓝色第二幕结束。", "provider": "mock"},
            )
            timeline_id = add_node(
                "timeline",
                "验收最终成片",
                1000,
                200,
                {
                    "subtitle_text": "红色第一幕开始。蓝色第二幕结束。",
                    "output": {"width": 640, "height": 360, "fps": 24},
                },
            )

            def connect(source: str, source_handle: str, target_handle: str) -> None:
                nonlocal revision
                response = request(
                    "POST",
                    "/api/canvas/connect",
                    json={
                        "canvas": canvas_name,
                        "source": source,
                        "source_handle": source_handle,
                        "target": timeline_id,
                        "target_handle": target_handle,
                        "revision": revision,
                    },
                ).json()
                revision = response["revision"]

            connect(video_nodes[0], "video", "video")
            connect(video_nodes[1], "video", "video")
            connect(tts_id, "audio", "audio")

            # Reverse the upload order to prove that clip_order, not edge order,
            # controls playback.  This endpoint previously rejected status=None.
            response = request(
                "POST",
                f"/api/canvas/nodes/{timeline_id}",
                json={
                    "canvas": canvas_name,
                    "data": {
                        "clip_order": [video_nodes[1], video_nodes[0]],
                        "subtitle_text": "红色第一幕开始。蓝色第二幕结束。",
                        "output": {"width": 640, "height": 360, "fps": 24},
                    },
                    "revision": revision,
                },
            ).json()
            revision = response["revision"]

            approval = request(
                "POST",
                "/api/canvas/composition-approvals",
                json={"canvas": canvas_name, "only": [timeline_id], "revision": revision},
            ).json()
            assert [item["node_id"] for item in approval["plans"][0]["clips"]] == [
                video_nodes[1],
                video_nodes[0],
            ]

            # Any canvas edit after approval must invalidate the frozen request.
            response = request(
                "POST",
                f"/api/canvas/nodes/{timeline_id}",
                json={
                    "canvas": canvas_name,
                    "data": {
                        "clip_order": [video_nodes[1], video_nodes[0]],
                        "subtitle_text": "红色第一幕开始。蓝色第二幕结束。",
                        "output": {"width": 640, "height": 360, "fps": 25},
                    },
                    "revision": revision,
                },
            ).json()
            revision = response["revision"]
            request(
                "POST",
                "/api/canvas/compose",
                expected=409,
                json={
                    "canvas": canvas_name,
                    "only": [timeline_id],
                    "approved": True,
                    "approval_id": approval["approval_id"],
                    "revision": revision - 1,
                },
            )

            approval = request(
                "POST",
                "/api/canvas/composition-approvals",
                json={"canvas": canvas_name, "only": [timeline_id], "revision": revision},
            ).json()
            queued = request(
                "POST",
                "/api/canvas/compose",
                json={
                    "canvas": canvas_name,
                    "only": [timeline_id],
                    "approved": True,
                    "approval_id": approval["approval_id"],
                    "revision": revision,
                },
            ).json()
            job_id = queued["job_id"]
            created_paths.add(f"assets/rendered/{job_id}/{timeline_id}.srt")
            deadline = time.monotonic() + 45
            state: dict[str, Any] = {}
            while time.monotonic() < deadline:
                state = request("GET", f"/api/media/jobs/{job_id}").json()
                if state["status"] in {"succeeded", "failed", "unknown"}:
                    break
                time.sleep(0.2)
            assert state.get("status") == "succeeded", json.dumps(state, ensure_ascii=False, indent=2)

            canvas = request("GET", "/api/canvas", params={"name": canvas_name}).json()
            timeline = next(item for item in canvas["nodes"] if item["id"] == timeline_id)
            output = timeline["outputs"]["video"]["value"]
            output_path = output["path"]
            created_paths.add(output_path)
            for result in (state.get("result") or {}).get("results", []):
                for key in ("path", "audio_path", "subtitle_path"):
                    if result.get(key):
                        created_paths.add(str(result[key]))

            downloaded = request("GET", f"/{output_path}").content
            assert downloaded[:8].endswith(b"ftyp") or b"ftyp" in downloaded[:32]
            local_copy = temp / "downloaded-final.mp4"
            local_copy.write_bytes(downloaded)
            metadata = probe_media(local_copy)
            assert metadata["video"], metadata
            assert metadata["audio"], metadata
            assert 2.2 <= float(metadata["duration"]) <= 3.2, metadata
            video_stream = metadata["video"]
            assert int(video_stream["width"]) == 640 and int(video_stream["height"]) == 360, metadata

            print(
                "PASS live API: upload -> ordered timeline -> stale approval 409 -> "
                "background compose -> downloadable MP4 with video+audio"
            )
    finally:
        # Fetch once more so paths created just before a failure can also be
        # removed.  Cleanup is intentionally restricted to the unique QA canvas.
        try:
            canvas = request("GET", "/api/canvas", params={"name": canvas_name}).json()
            for node in canvas.get("nodes", []):
                node_ids.add(str(node.get("id") or ""))
                for output in (node.get("outputs") or {}).values():
                    value = output.get("value") if isinstance(output, dict) else None
                    if isinstance(value, dict) and value.get("path"):
                        created_paths.add(str(value["path"]))
        except Exception:
            pass
        remove_test_artifacts(canvas_name, created_paths, {item for item in node_ids if item})


if __name__ == "__main__":
    main()
