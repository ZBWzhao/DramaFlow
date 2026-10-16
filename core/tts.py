"""Small TTS provider layer used by approved composition jobs."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .composition import write_mock_audio


@dataclass(frozen=True)
class TTSResult:
    path: str
    mime: str
    provider: str
    mock: bool


class TTSRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def describe(self) -> list[dict[str, object]]:
        configured = bool(os.environ.get("TTS_API_KEY", "").strip() and os.environ.get("TTS_BASE_URL", "").strip())
        return [
            {"id": "mock", "label": "Mock 测试音轨", "configured": True, "mock": True},
            {"id": "openai-compatible", "label": "OpenAI-compatible TTS", "configured": configured, "mock": False},
        ]

    def synthesize(self, request: dict[str, object], job_id: str) -> TTSResult:
        text = str(request.get("text") or "").strip()
        if not text:
            raise ValueError("TTS 文本不能为空")
        provider = str(request.get("provider") or "mock").strip().lower()
        target_dir = self.root / "assets" / "generated" / "tts"
        target_dir.mkdir(parents=True, exist_ok=True)
        if provider == "mock":
            target = target_dir / f"{job_id}-{uuid.uuid4().hex[:8]}.wav"
            duration = max(1.0, min(60.0, len(text) / 4.5))
            write_mock_audio(target, duration)
            return TTSResult(target.relative_to(self.root).as_posix(), "audio/wav", "mock", True)
        if provider != "openai-compatible":
            raise ValueError(f"未知 TTS Provider：{provider}")

        base_url = os.environ.get("TTS_BASE_URL", "").strip().rstrip("/")
        api_key = os.environ.get("TTS_API_KEY", "").strip()
        if not base_url or not api_key:
            raise RuntimeError("未配置 TTS_BASE_URL/TTS_API_KEY")
        body = json.dumps(
            {
                "model": str(request.get("model") or os.environ.get("TTS_MODEL", "tts-1")),
                "voice": str(request.get("voice") or os.environ.get("TTS_VOICE", "alloy")),
                "input": text,
                "speed": max(0.25, min(4.0, float(request.get("speed") or 1.0))),
                "response_format": "mp3",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        http_request = Request(
            f"{base_url}/audio/speech",
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with urlopen(http_request, timeout=int(os.environ.get("TTS_TIMEOUT_SECONDS", "180"))) as response:  # noqa: S310
                content = response.read(50 * 1024 * 1024 + 1)
                mime = response.headers.get_content_type() or "audio/mpeg"
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            raise RuntimeError(f"TTS 请求失败（HTTP {exc.code}）：{detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"TTS 连接失败：{exc}") from exc
        if not content or len(content) > 50 * 1024 * 1024:
            raise RuntimeError("TTS 返回为空或超过 50 MB")
        target = target_dir / f"{job_id}-{uuid.uuid4().hex[:8]}.mp3"
        target.write_bytes(content)
        return TTSResult(target.relative_to(self.root).as_posix(), mime, provider, False)
