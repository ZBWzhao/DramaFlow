"""图片/视频素材节点上传与替换回归（完全在临时目录运行）。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import server.app as server_module
from core.canvas import Canvas
from core.canvas_repository import FileCanvasRepository


def main() -> None:
    original_workspace = server_module.WORKSPACE
    original_repository = server_module.registry.canvas_repository
    with tempfile.TemporaryDirectory(prefix="toonflow-media-test-") as directory:
        server_module.WORKSPACE = Path(directory)
        server_module.registry.canvas_repository = FileCanvasRepository(Path(directory))
        client = TestClient(server_module.app)

        card = client.post(
            "/api/canvas/nodes",
            json={"canvas": "test", "type": "text", "label": "右键文本", "x": 321, "y": 654, "data": {}},
        )
        assert card.status_code == 200, card.text

        image = client.post(
            "/api/canvas/upload-media?canvas=test&kind=image&filename=reference.png&x=111&y=222",
            content=b"\x89PNG\r\n\x1a\n" + b"test-image",
            headers={"Content-Type": "image/png"},
        )
        assert image.status_code == 200, image.text
        image_result = image.json()

        replacement = client.post(
            f"/api/canvas/upload-media?canvas=test&kind=image&filename=replacement.webp&node_id={image_result['node_id']}",
            content=b"RIFFtest-webp",
            headers={"Content-Type": "image/webp"},
        )
        assert replacement.status_code == 200 and replacement.json()["replaced"] is True

        video = client.post(
            "/api/canvas/upload-media?canvas=test&kind=video&filename=reference.mp4",
            content=b"\x00\x00\x00\x18ftypmp42test-video",
            headers={"Content-Type": "video/mp4"},
        )
        assert video.status_code == 200, video.text

        canvas = Canvas.load(Path(directory) / "test.canvas.json")
        assert {node.type for node in canvas.nodes.values()} == {"text", "image", "video"}
        assert canvas.nodes[card.json()["node_id"]].position == {"x": 321.0, "y": 654.0}
        assert canvas.nodes[image_result["node_id"]].position == {"x": 111.0, "y": 222.0}
        assert canvas.nodes[image_result["node_id"]].outputs["image"]["data_type"] == "IMAGE"
        assert canvas.nodes[video.json()["node_id"]].outputs["video"]["data_type"] == "VIDEO"
        assert all((Path(directory) / result["path"]).exists() for result in (replacement.json(), video.json()))

        asset = canvas.add_node("asset", "角色参考", data={"kind": "character"})
        prompt = canvas.add_node("prompt", "片段提示词", data={"segment_no": 1})
        generator = canvas.add_node("video_gen", "视频生成")
        canvas.connect(image_result["node_id"], "image", asset.id, "image")
        canvas.connect(asset.id, "ref", prompt.id, "ref")
        removable_edge = canvas.connect(video.json()["node_id"], "video", prompt.id, "video_ref")
        canvas.connect(prompt.id, "prompt", generator.id, "prompt")
        refs = canvas.generation_refs(generator.id)
        assert {ref["type"] for ref in refs} == {"IMAGE", "VIDEO"}
        assert {ref["path"] for ref in refs} == {replacement.json()["path"], video.json()["path"]}
        canvas.save()

        node_ids_before = set(canvas.nodes)
        delete = client.post(
            "/api/canvas/edges/delete",
            json={"canvas": "test", "edge_id": removable_edge.id},
        )
        assert delete.status_code == 200, delete.text
        reloaded = Canvas.load(Path(directory) / "test.canvas.json")
        assert removable_edge.id not in {edge.id for edge in reloaded.edges}
        assert set(reloaded.nodes) == node_ids_before
        missing = client.post(
            "/api/canvas/edges/delete",
            json={"canvas": "test", "edge_id": removable_edge.id},
        )
        assert missing.status_code == 404
        print("PASS media references + exact edge deletion: nodes remain intact")
    server_module.WORKSPACE = original_workspace
    server_module.registry.canvas_repository = original_repository


if __name__ == "__main__":
    main()
