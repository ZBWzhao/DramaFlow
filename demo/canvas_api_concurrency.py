"""HTTP-level stale canvas revision must return 409 and preserve the winner."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import server.app as server_module
from core.canvas_repository import FileCanvasRepository


def main() -> None:
    original_workspace = server_module.WORKSPACE
    original_repository = server_module.registry.canvas_repository
    with tempfile.TemporaryDirectory(prefix="dramaflow-canvas-cas-") as folder:
        root = Path(folder)
        server_module.WORKSPACE = root
        server_module.registry.canvas_repository = FileCanvasRepository(root)
        try:
            with TestClient(server_module.app) as client:
                snapshot = client.get("/api/canvas?name=cas-test")
                assert snapshot.status_code == 200
                revision = snapshot.json()["revision"]
                first = client.post("/api/canvas/nodes", json={
                    "canvas": "cas-test", "type": "text", "label": "winner",
                    "x": 10, "y": 10, "data": {}, "revision": revision,
                })
                assert first.status_code == 200, first.text
                stale = client.post("/api/canvas/nodes", json={
                    "canvas": "cas-test", "type": "text", "label": "stale",
                    "x": 20, "y": 20, "data": {}, "revision": revision,
                })
                assert stale.status_code == 409, stale.text
                assert stale.json()["code"] == "canvas_revision_conflict"
                final = client.get("/api/canvas?name=cas-test").json()
                assert [node["label"] for node in final["nodes"]] == ["winner"]
                assert final["revision"] == first.json()["revision"] == 1
                print("PASS: stale HTTP mutation returns 409 and cannot overwrite the winner")
        finally:
            server_module.WORKSPACE = original_workspace
            server_module.registry.canvas_repository = original_repository


if __name__ == "__main__":
    main()
