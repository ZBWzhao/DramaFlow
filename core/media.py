"""媒体生成：provider 抽象 + 零依赖的 mock 实现。

对照 Toonflow：
  · packages/providers/types.d.ts 的 ProviderDefinition（generateImage/Video/Audio）
  · apps/server/src/utils/media/generation.ts：提交 → 轮询 → 落盘 → 返回 { path, mimeType }

这里把之前只留了协议的 `MediaCapability` 真正接上：
  · `MockProvider` 生成**真的 PNG 文件**（渐变 + 条纹），不调用任何外部服务
  · 同一段提示词 → 同样的图（由提示词 hash 决定配色），所以结果可复现
  · 换真实 provider 时，只要实现同一个 Protocol，`ToolContext.media` 一换即可

为什么先做 mock（而不是直接接 API）：
  1. 生成链路（节点状态流转、产物落盘、前端展示）能先跑通并被测试
  2. 不花钱、不受外部服务抖动影响，回归测试可重复
  3. 换 provider 是配置问题，不是架构问题 —— 这正是能力位抽象的意义
"""

from __future__ import annotations

import hashlib
import base64
import json
import mimetypes
import os
import struct
import time
import uuid
import zlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .composition import CompositionError, ffmpeg_capabilities, probe_media


class TFRouterHTTPError(RuntimeError):
    """保留 HTTP 状态，便于区分任务创建后的短暂不可见和真正失败。"""

    def __init__(self, code: int, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"TF-router 请求失败（HTTP {code}）：{detail}")


@dataclass
class MediaResult:
    """一次生成的结果（对应 Toonflow 的 GeneratedMedia）。"""

    kind: str  # image / video / audio
    path: str  # 相对工作区的路径
    mime: str
    provider: str
    mock: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class MediaProvider(Protocol):
    name: str

    def list_models(self) -> list[dict[str, Any]]: ...
    def generate(self, kind: str, request: dict[str, Any], root: Path) -> MediaResult: ...
    def resume(self, kind: str, operation_id: str, request: dict[str, Any], root: Path) -> MediaResult: ...


# ---------------------------------------------------------------------------
# 纯 Python 生成 PNG（零依赖）
# ---------------------------------------------------------------------------
def _png_bytes(width: int, height: int, rows: list[bytes]) -> bytes:
    """把 RGB 行数据编码成 PNG：IHDR + IDAT(zlib) + IEND。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + row for row in rows)  # 每行前面一个 filter 字节
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8bit truecolor
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def _placeholder_png(seed: str, width: int, height: int) -> bytes:
    """按 seed 生成一张可复现的占位图：对角渐变 + 斜条纹。

    配色由 seed 决定 —— 所以同一个片段每次生成的图长得一样，
    一眼就能看出"这张图是这个片段的"，也方便回归比对。
    """
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    base = (digest[0], digest[1], digest[2])
    accent = (digest[3], digest[4], digest[5])

    rows: list[bytes] = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            # 对角渐变（0-1）
            t = (x / max(width - 1, 1) + y / max(height - 1, 1)) / 2
            r = int(base[0] * (1 - t) + accent[0] * t)
            g = int(base[1] * (1 - t) + accent[1] * t)
            b = int(base[2] * (1 - t) + accent[2] * t)
            # 斜条纹：每 48px 一条，稍微提亮，让图看起来"有内容"
            if ((x + y) // 48) % 2 == 0:
                r, g, b = min(255, r + 22), min(255, g + 22), min(255, b + 22)
            row += bytes((r, g, b))
        rows.append(bytes(row))
    return _png_bytes(width, height, rows)


# ---------------------------------------------------------------------------
# mock provider
# ---------------------------------------------------------------------------
class MockProvider:
    """零依赖、可复现的占位实现。真写 PNG 文件，但**不调用任何外部服务**。"""

    name = "mock"

    MODELS: list[dict[str, Any]] = [
        {"id": "mock-image-v1", "kind": "image", "label": "占位图片（可复现）", "sizes": ["1024x576", "768x1024"]},
        {"id": "mock-video-v1", "kind": "video", "label": "占位视频（首帧 + 说明）", "durations": [3, 5, 10]},
    ]

    def list_models(self) -> list[dict[str, Any]]:
        return list(self.MODELS)

    def generate(self, kind: str, request: dict[str, Any], root: Path) -> MediaResult:
        prompt = str(request.get("prompt") or "")
        if not prompt.strip():
            raise ValueError("prompt 不能为空")
        model = str(request.get("model") or f"mock-{kind}-v1")

        size = str(request.get("size") or ("1024x576" if kind == "image" else "1280x720"))
        try:
            width, height = (int(v) for v in size.lower().split("x"))
        except ValueError as exc:
            raise ValueError(f"size 格式应为 宽x高，收到：{size}") from exc
        if not (64 <= width <= 2048 and 64 <= height <= 2048):
            raise ValueError("尺寸需在 64–2048 之间")

        if kind not in {"image", "video"}:
            raise ValueError(f"MockProvider 暂不支持 {kind}")
        # 文件名对齐 Toonflow 的 <mediaType><uuid>.<ext>。视频必须是真的 MP4，
        # 不能再拿 PNG 首帧伪装，否则后面的时间线永远无法被 FFmpeg 解码。
        extension = ".png" if kind == "image" else ".mp4"
        filename = f"{kind}-{uuid.uuid4().hex[:10]}{extension}"
        relative = f"assets/generated/{filename}"
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)

        meta: dict[str, Any] = {"prompt_head": prompt[:60], "size": f"{width}x{height}", "model": model}
        if kind == "image":
            target.write_bytes(_placeholder_png(prompt, width, height))
            return MediaResult(kind=kind, path=relative, mime="image/png", provider=self.name, mock=True, meta=meta)

        capability = ffmpeg_capabilities()
        if not capability["available"]:
            raise CompositionError("Mock 视频需要 FFmpeg 才能生成真实 MP4；" + capability["reason"])
        duration = max(1, min(30, int(request.get("duration") or 5)))
        digest = hashlib.sha256(prompt.encode("utf-8")).digest()
        color = f"0x{digest[0]:02x}{digest[1]:02x}{digest[2]:02x}"
        command = [
            str(capability["ffmpeg"]), "-nostdin", "-y", "-f", "lavfi",
            "-i", f"color=c={color}:s={width}x{height}:r=24:d={duration}",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(target),
        ]
        completed = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=120, check=False, shell=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Mock 视频生成失败：{completed.stderr[-1000:]}")
        inspected = probe_media(target)
        meta.update({"duration": inspected["duration"], "fps": 24, "note": "FFmpeg 生成的可复现 Mock MP4"})
        return MediaResult(kind=kind, path=relative, mime="video/mp4", provider=self.name, mock=True, meta=meta)

    def resume(self, kind: str, operation_id: str, request: dict[str, Any], root: Path) -> MediaResult:
        raise RuntimeError("Mock 任务没有远端 operation_id，无法恢复")


class TFRouterProvider:
    """ToonFlow 官方 TF-router 媒体 Provider。

    凭据只从服务端环境变量读取；浏览器只能看到 Provider/模型状态，拿不到 Key。
    接口和轮询格式与原 ToonFlow 的 packages/providers/src/media/tfRouter.ts 对齐。
    """

    name = "tfrouter"
    api_url = os.environ.get("TF_ROUTER_BASE_URL", "https://api.toonflow.net/v1").rstrip("/")
    MODELS: list[dict[str, Any]] = [
        {"id": "Seedance 2.5", "kind": "video", "label": "Seedance 2.5", "durations": list(range(4, 31)), "resolutions": ["480p", "720p", "1080p"]},
        {"id": "Seedance 2.0", "kind": "video", "label": "Seedance 2.0", "durations": list(range(4, 16)), "resolutions": ["480p", "720p"]},
        {"id": "Seedance 2.0 fast", "kind": "video", "label": "Seedance 2.0 fast", "durations": list(range(4, 16)), "resolutions": ["480p", "720p"]},
        {"id": "Seedance 2.0 mini", "kind": "video", "label": "Seedance 2.0 mini", "durations": list(range(4, 16)), "resolutions": ["480p", "720p"]},
        {"id": "wan-3.0", "kind": "video", "label": "Wan 3.0", "durations": list(range(2, 31)), "resolutions": ["480p", "720p", "1080p"]},
        {"id": "MiniMax-H3", "kind": "video", "label": "MiniMax H3", "durations": list(range(4, 16)), "resolutions": ["480p", "768p"]},
        {"id": "doubao-seedream-5.0-Pro", "kind": "image", "label": "Seedream 5.0 Pro", "sizes": ["1K", "1.5K", "2K"]},
        {"id": "doubao-seedream-5.0-Lite", "kind": "image", "label": "Seedream 5.0 Lite", "sizes": ["2K", "3K", "4K"]},
        {"id": "全能图片G-2.5", "kind": "image", "label": "全能图片 G-2.5", "sizes": ["1K", "2K", "4K"]},
    ]

    def __init__(self, api_key: str, api_url: str | None = None) -> None:
        self.api_key = api_key.strip().removeprefix("Bearer ").strip()
        if not self.api_key:
            raise ValueError("TF-router API Key 为空")
        # 在实例化时读取地址，避免测试/部署过程中修改环境变量后仍使用旧地址。
        self.api_url = (api_url or os.environ.get("TF_ROUTER_BASE_URL") or "https://api.toonflow.net/v1").rstrip("/")

    def list_models(self) -> list[dict[str, Any]]:
        return [dict(model) for model in self.MODELS]

    def _json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{self.api_url}/{path}",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=60) as response:  # noqa: S310 - URL is deployment configuration
                value = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise TFRouterHTTPError(exc.code, detail) from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"TF-router 连接失败：{exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("TF-router 响应格式错误")
        return value

    @staticmethod
    def _result_value(payload: dict[str, Any]) -> str | None:
        """兼容 Provider 常见的 data/url/output/result 媒体返回形态。"""
        def find(value: Any) -> str | None:
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                for key in ("url", "data", "output", "result", "file_url", "download_url"):
                    found = find(value.get(key))
                    if found:
                        return found
            if isinstance(value, list):
                for item in value:
                    found = find(item)
                    if found:
                        return found
            return None

        return find(payload.get("data")) or find(payload.get("output")) or find(payload.get("result"))

    @staticmethod
    def _task_id(payload: dict[str, Any]) -> str | None:
        """从不同版本的创建响应中提取远端任务 ID。"""
        data = payload.get("data")
        candidates: list[Any] = [payload.get("taskICode"), payload.get("task_id"), payload.get("id")]
        if isinstance(data, dict):
            candidates.extend([data.get("taskICode"), data.get("task_id"), data.get("id"), data.get("code")])
        else:
            candidates.append(data)
        for value in candidates:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _save_asset(self, value: str, kind: str, root: Path, model: str) -> MediaResult:
        if value.startswith("data:"):
            header, encoded = value.split(",", 1)
            mime = header[5:].split(";", 1)[0].lower()
            try:
                content = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise RuntimeError("TF-router 返回的 data URL 不是有效 Base64") from exc
        elif value.startswith(("http://", "https://")):
            try:
                with urlopen(value, timeout=180) as response:  # noqa: S310 - Provider result URL
                    mime = response.headers.get_content_type()
                    content = response.read(100 * 1024 * 1024 + 1)
            except (HTTPError, URLError, TimeoutError) as exc:
                raise RuntimeError(f"下载 TF-router 生成结果失败：{exc}") from exc
        else:
            raise RuntimeError("TF-router 返回的媒体地址无效")
        if not content or len(content) > 100 * 1024 * 1024:
            raise RuntimeError("生成文件为空或超过 100 MB")
        if not mime.startswith(f"{kind}/"):
            raise RuntimeError(f"Provider 返回类型 {mime}，与请求的 {kind} 不一致")
        extension = mimetypes.guess_extension(mime) or (".mp4" if kind == "video" else ".png")
        relative = f"assets/generated/{kind}-{uuid.uuid4().hex[:10]}{extension}"
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return MediaResult(kind=kind, path=relative, mime=mime, provider=self.name, mock=False, meta={"model": model, "bytes": len(content)})

    def generate(self, kind: str, request: dict[str, Any], root: Path) -> MediaResult:
        if kind not in {"image", "video"}:
            raise ValueError(f"TF-router 暂不支持 {kind}")
        prompt = str(request.get("prompt") or "").strip()
        model = str(request.get("model") or "").strip()
        if not prompt or not model:
            raise ValueError("TF-router 生成需要 prompt 和 model")
        if not any(item["id"] == model and item["kind"] == kind for item in self.MODELS):
            raise ValueError(f"TF-router 不存在匹配的 {kind} 模型：{model}")

        if kind == "image":
            requested_size = str(request.get("size") or "2K")
            if "x" in requested_size.lower():
                requested_size = "2K"
            size_alias = {"1k": "1K", "1.5k": "1.5K", "2k": "2K", "3k": "3K", "4k": "4K"}
            requested_size = size_alias.get(requested_size.strip().lower(), requested_size.strip())
            body = {
                "model": model,
                "prompt": prompt,
                # TF-router 的 Seedream 兼容层要求尺寸为小写，元数据字段与官方适配器一致。
                "size": requested_size.lower(),
                "metadata": {
                    "response_format": "url",
                    "aspectRatio": str(request.get("ratio") or "16:9"),
                    "sequential_image_generation": "disabled",
                    "stream": False,
                    "watermark": False,
                },
            }
            suffix = "Image"
        else:
            body = {
                "model": model,
                "prompt": prompt,
                "duration": int(request.get("duration") or 5),
                "resolution": str(request.get("resolution") or "720p"),
                "metadata": {
                    "ratio": str(request.get("ratio") or "16:9"),
                    "generate_audio": bool(request.get("generate_audio", False)),
                    "references": request.get("references") or [],
                },
            }
            suffix = "Video"

        created = self._json(f"{kind}/generate{suffix}", body)
        task_id = self._task_id(created)
        if not task_id:
            raise RuntimeError("TF-router 未返回任务 ID")
        callback = request.get("operation_callback")
        if callable(callback):
            callback(task_id)
        return self._wait_for_result(kind, task_id, request, root)

    def resume(self, kind: str, operation_id: str, request: dict[str, Any], root: Path) -> MediaResult:
        """只轮询已有远端任务，不再次调用 Provider 的创建接口。"""
        if kind not in {"image", "video"}:
            raise ValueError(f"TF-router 暂不支持恢复 {kind}")
        if not operation_id.strip():
            raise ValueError("operation_id 不能为空")
        return self._wait_for_result(kind, operation_id, request, root)

    def _wait_for_result(self, kind: str, task_id: str, request: dict[str, Any], root: Path) -> MediaResult:
        model = str(request.get("model") or "").strip()
        suffix = "Image" if kind == "image" else "Video"
        deadline = time.monotonic() + int(os.environ.get("MEDIA_GENERATION_TIMEOUT", "1800"))
        interval = max(1.0, float(os.environ.get("MEDIA_POLL_INTERVAL", "3")))
        # 创建接口落库与状态查询接口之间可能有几秒最终一致性窗口。
        # Provider 创建任务后可能需要几十秒才在查询服务可见；这段时间不能把
        # “暂时查不到”误判成远端失败，否则会留下已扣费但未落盘的任务。
        not_found_grace_deadline = time.monotonic() + min(60.0, max(interval * 10, 30.0))
        while time.monotonic() < deadline:
            try:
                result = self._json(f"{kind}/get{suffix}Status", {"taskICode": task_id})
            except TFRouterHTTPError as exc:
                detail = exc.detail.lower()
                transient_not_found = exc.code in {400, 404} and any(
                    marker in detail for marker in ("不存在", "not found", "not exist", "does not exist")
                )
                if transient_not_found and time.monotonic() < not_found_grace_deadline:
                    time.sleep(interval)
                    continue
                raise
            nested = result.get("data") if isinstance(result.get("data"), dict) else {}
            status = str(
                result.get("status")
                or result.get("state")
                or nested.get("status")
                or nested.get("state")
                or ""
            ).lower()
            if status in {"success", "completed"}:
                value = self._result_value(result)
                if not value:
                    raise RuntimeError("TF-router 任务成功但未返回媒体地址")
                return self._save_asset(value, kind, root, model)
            if status in {"failed", "failure"}:
                reason = nested.get("failReason") or result.get("message") or f"{kind} 生成失败"
                raise RuntimeError(str(reason))
            time.sleep(interval)
        raise TimeoutError("TF-router 生成超时，可稍后到供应商控制台查询任务")


class MediaRegistry:
    """多 Provider 注册表；默认保持 mock，避免未审批时意外产生费用。"""

    def __init__(self, provider: MediaProvider | None = None) -> None:
        mock = provider or MockProvider()
        self.providers: dict[str, MediaProvider] = {mock.name: mock}
        key = os.environ.get("TF_ROUTER_API_KEY", "").strip()
        if key:
            real = TFRouterProvider(key)
            self.providers[real.name] = real
        configured = os.environ.get("MEDIA_PROVIDER", "mock").strip().lower()
        self.active = configured if configured in self.providers else "mock"
        self.provider = self.providers[self.active]

    def list_models(self) -> list[dict[str, Any]]:
        return [
            {**model, "provider": name, "mock": name == "mock"}
            for name, provider in self.providers.items()
            for model in provider.list_models()
        ]

    def models_for(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.list_models() if m.get("kind") == kind]

    def generate(self, kind: str, request: dict[str, Any], root: Path) -> MediaResult:
        provider_name = str(request.get("provider") or "").strip().lower()
        model = str(request.get("model") or "")
        if not provider_name and model:
            for name, candidate in self.providers.items():
                if any(item.get("id") == model and item.get("kind") == kind for item in candidate.list_models()):
                    provider_name = name
                    break
        provider_name = provider_name or self.active
        selected = self.providers.get(provider_name)
        if selected is None:
            raise ValueError(f"媒体 Provider 未配置：{provider_name}")
        return selected.generate(kind, request, root)

    def resume(self, kind: str, operation_id: str, request: dict[str, Any], root: Path) -> MediaResult:
        provider_name = str(request.get("provider") or self.active).strip().lower()
        selected = self.providers.get(provider_name)
        if selected is None:
            raise ValueError(f"媒体 Provider 未配置：{provider_name}")
        resume = getattr(selected, "resume", None)
        if not callable(resume):
            raise RuntimeError(f"媒体 Provider {provider_name} 不支持恢复远端任务")
        return resume(kind, operation_id, request, root)

    def describe(self) -> list[dict[str, Any]]:
        known = [
            {"id": "mock", "label": "Mock 占位（不产生费用）", "configured": True, "mock": True},
            {"id": "tfrouter", "label": "TF-router", "configured": "tfrouter" in self.providers, "mock": False},
        ]
        for item in known:
            provider = self.providers.get(item["id"])
            item["active"] = item["id"] == self.active
            item["models"] = [dict(model) for model in provider.list_models()] if provider else []
        return known


# 进程级默认注册表（ToolContext.media 用它）
default_registry = MediaRegistry()


class MediaCapabilityAdapter:
    """把注册表适配成 ToolContext 需要的 MediaCapability 协议。

    这样做的好处：能力位只依赖一个窄接口（list_models/generate），
    换 provider 不用动 ToolContext、也不用动任何工具代码。
    """

    def __init__(self, registry: MediaRegistry, root: Path) -> None:
        self.registry = registry
        self.root = root

    def list_models(self) -> list[dict[str, Any]]:
        return self.registry.list_models()

    def generate(self, kind: str, request: dict[str, Any]) -> MediaResult:
        return self.registry.generate(kind, request, self.root)
