"""离线验证 TF-router 图片响应兼容性，不调用外部 Provider。"""

from __future__ import annotations

import base64
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.media import MediaResult, TFRouterProvider  # noqa: E402


ONE_PIXEL_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


class FakeTFRouter(TFRouterProvider):
    def __init__(self) -> None:
        super().__init__("test-key", "https://example.invalid/v1")
        self.requests: list[tuple[str, dict]] = []

    def _json(self, path: str, body: dict) -> dict:
        self.requests.append((path, body))
        if path.endswith("generateImage"):
            return {"data": {"taskICode": "image-task-1"}}
        return {
            "data": {
                "status": "completed",
                "output": f"data:image/png;base64,{ONE_PIXEL_PNG}",
            }
        }


def main() -> None:
    provider = FakeTFRouter()
    with tempfile.TemporaryDirectory() as directory:
        result: MediaResult = provider.generate(
            "image",
            {"model": "doubao-seedream-5.0-Pro", "prompt": "test", "size": "2k"},
            Path(directory),
        )
        target = Path(directory) / result.path
        assert result.mock is False
        assert result.mime == "image/png"
        assert target.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert provider.requests[0][1]["size"] == "2k"
    print("PASS TF-router image response parsing, size normalization and asset download")


if __name__ == "__main__":
    main()
