"""Local video composition powered by FFmpeg/ffprobe.

The module deliberately owns process execution and path validation so API and
worker code only deal with a frozen render plan.  It never invokes a shell.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CompositionError(RuntimeError):
    """A render plan cannot be inspected or rendered safely."""


def _binary(env_name: str, fallback: str) -> str:
    configured = os.environ.get(env_name, "").strip()
    if configured:
        return configured
    discovered = shutil.which(fallback)
    if discovered:
        return discovered
    # A user-scope WinGet install updates PATH for new terminals only.  The
    # already-running API process can still use the stable WinGet command link.
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        winget_link = Path(local_app_data) / "Microsoft" / "WinGet" / "Links" / f"{fallback}.exe"
        if winget_link.is_file():
            return str(winget_link)
    return ""


def ffmpeg_capabilities() -> dict[str, Any]:
    ffmpeg = _binary("FFMPEG_BIN", "ffmpeg")
    ffprobe = _binary("FFPROBE_BIN", "ffprobe")
    return {
        "available": bool(ffmpeg and ffprobe),
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "reason": "" if ffmpeg and ffprobe else "未找到 ffmpeg/ffprobe，请安装后重启服务",
    }


def _inside(root: Path, relative: str, *, must_exist: bool = True) -> Path:
    base = root.resolve()
    value = str(relative or "").replace("\\", "/").lstrip("/")
    target = (base / value).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise CompositionError(f"媒体路径越界：{relative}") from exc
    if must_exist and not target.is_file():
        raise CompositionError(f"媒体文件不存在：{relative}")
    return target


def _run(command: list[str], *, timeout: int, purpose: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CompositionError(f"{purpose}超时（{timeout}s）") from exc
    except OSError as exc:
        raise CompositionError(f"无法启动 {purpose}：{exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "未知错误").strip()[-1600:]
        raise CompositionError(f"{purpose}失败：{detail}")
    return result


def probe_media(path: Path) -> dict[str, Any]:
    binary = _binary("FFPROBE_BIN", "ffprobe")
    if not binary:
        raise CompositionError("未找到 ffprobe，请安装 FFmpeg 后重启服务")
    result = _run(
        [
            binary, "-v", "error", "-show_entries",
            "format=duration,size:stream=index,codec_type,codec_name,width,height,r_frame_rate,duration",
            "-of", "json", str(path),
        ],
        timeout=int(os.environ.get("FFPROBE_TIMEOUT_SECONDS", "30")),
        purpose="ffprobe 检查",
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CompositionError("ffprobe 返回了无效 JSON") from exc
    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    duration = float((payload.get("format") or {}).get("duration") or (video or {}).get("duration") or 0)
    return {
        "duration": round(duration, 3),
        "size": int((payload.get("format") or {}).get("size") or path.stat().st_size),
        "video": video,
        "audio": audio,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class RenderResult:
    path: str
    mime: str
    duration: float
    width: int
    height: int
    fps: int
    file_size: int
    sha256: str


class CompositionService:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def inspect(self, relative: str, expected: str = "video") -> dict[str, Any]:
        path = _inside(self.root, relative)
        metadata = probe_media(path)
        if expected == "video" and not metadata.get("video"):
            raise CompositionError(f"文件不是可解码视频：{relative}")
        if expected == "audio" and not metadata.get("audio"):
            raise CompositionError(f"文件不是可解码音频：{relative}")
        return {**metadata, "path": relative, "sha256": sha256_file(path)}

    def render(self, plan: dict[str, Any], job_id: str) -> RenderResult:
        capabilities = ffmpeg_capabilities()
        if not capabilities["available"]:
            raise CompositionError(capabilities["reason"])
        clips = plan.get("clips") if isinstance(plan.get("clips"), list) else []
        if not clips:
            raise CompositionError("时间线没有可合成的视频片段")

        output = plan.get("output") if isinstance(plan.get("output"), dict) else {}
        width = max(320, min(3840, int(output.get("width") or 1280)))
        height = max(240, min(2160, int(output.get("height") or 720)))
        fps = max(12, min(60, int(output.get("fps") or 24)))
        timeout = int(os.environ.get("COMPOSITION_TIMEOUT_SECONDS", "1800"))
        ffmpeg = str(capabilities["ffmpeg"])

        render_root = self.root / "assets" / "rendered" / job_id
        render_root.mkdir(parents=True, exist_ok=True)
        final_path = render_root / "final.mp4"
        temporary_final = render_root / "final.tmp.mp4"
        if final_path.exists():
            checked = probe_media(final_path)
            if checked.get("video"):
                return self._result(final_path, checked, width, height, fps)

        with tempfile.TemporaryDirectory(prefix=f"dramaflow-{job_id}-") as temp_name:
            temp = Path(temp_name)
            normalized: list[Path] = []
            for index, clip in enumerate(clips):
                source = _inside(self.root, str(clip.get("path") or ""))
                target = temp / f"clip-{index:04d}.mp4"
                trim_start = max(0.0, float(clip.get("trim_start") or 0))
                trim_end = clip.get("trim_end")
                command = [ffmpeg, "-nostdin", "-y"]
                if trim_start:
                    command += ["-ss", f"{trim_start:.3f}"]
                command += ["-i", str(source)]
                if trim_end not in (None, ""):
                    duration = float(trim_end) - trim_start
                    if duration <= 0:
                        raise CompositionError(f"片段 {index + 1} 的结束时间必须晚于开始时间")
                    command += ["-t", f"{duration:.3f}"]
                vf = (
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,fps={fps}"
                )
                command += [
                    "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "21", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target),
                ]
                _run(command, timeout=timeout, purpose=f"规范化片段 {index + 1}")
                normalized.append(target)

            concat_file = temp / "concat.txt"
            concat_file.write_text(
                "".join(f"file '{path.as_posix().replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n" for path in normalized),
                encoding="utf-8",
            )
            silent_video = temp / "video.mp4"
            _run(
                [
                    ffmpeg, "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
                    "-c", "copy", "-movflags", "+faststart", str(silent_video),
                ],
                timeout=timeout,
                purpose="拼接视频片段",
            )

            command = [ffmpeg, "-nostdin", "-y", "-i", str(silent_video)]
            audio = plan.get("audio") if isinstance(plan.get("audio"), dict) else None
            subtitle = plan.get("subtitle") if isinstance(plan.get("subtitle"), dict) else None
            audio_index = None
            if audio and audio.get("path"):
                audio_index = 1
                command += ["-i", str(_inside(self.root, str(audio["path"])))]
            if subtitle and subtitle.get("path"):
                subtitle_path = _inside(self.root, str(subtitle["path"]))
                escaped = subtitle_path.as_posix().replace("'", r"\'").replace(":", r"\:")
                subtitle_filter = (
                    f"subtitles=filename='{escaped}':charenc=UTF-8:"
                    "force_style='FontSize=18,Outline=2,Alignment=2,MarginV=30'"
                )
                command += [
                    "-map", "0:v:0", "-vf", subtitle_filter, "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
                ]
            else:
                command += ["-map", "0:v:0", "-c:v", "copy"]
            if audio_index is not None:
                # Pad short narration with silence so it never truncates the
                # final shot; -shortest then stops exactly with the video.
                command += [
                    "-map", f"{audio_index}:a:0", "-af", "apad",
                    "-c:a", "aac", "-b:a", "192k", "-shortest",
                ]
            command += ["-movflags", "+faststart", str(temporary_final)]
            _run(command, timeout=timeout, purpose="封装成片")
            checked = probe_media(temporary_final)
            if not checked.get("video") or checked.get("duration", 0) <= 0:
                raise CompositionError("成片校验失败：没有有效视频流或时长为 0")
            os.replace(temporary_final, final_path)

        return self._result(final_path, checked, width, height, fps)

    def _result(self, path: Path, checked: dict[str, Any], width: int, height: int, fps: int) -> RenderResult:
        return RenderResult(
            path=path.relative_to(self.root).as_posix(),
            mime="video/mp4",
            duration=float(checked.get("duration") or 0),
            width=width,
            height=height,
            fps=fps,
            file_size=path.stat().st_size,
            sha256=sha256_file(path),
        )


def write_mock_audio(path: Path, duration: float, frequency: int = 220) -> None:
    """Create deterministic PCM WAV for tests; it is intentionally labelled mock."""
    import math
    import struct

    rate = 24000
    frames = max(1, int(max(0.2, duration) * rate))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        for index in range(frames):
            envelope = min(1.0, index / 1200, (frames - index) / 1200)
            value = int(1000 * envelope * math.sin(2 * math.pi * frequency * index / rate))
            output.writeframesraw(struct.pack("<h", value))


def write_srt(path: Path, text: str, duration: float) -> None:
    """Create a readable fallback SRT when a TTS provider has no word timings."""
    import re

    sentences = [part.strip() for part in re.split(r"(?<=[。！？!?；;])\s*|\n+", text) if part.strip()]
    if not sentences:
        return
    weights = [max(1, len(re.sub(r"\s+", "", item))) for item in sentences]
    total = sum(weights)
    cursor = 0.0

    def stamp(seconds: float) -> str:
        milliseconds = max(0, int(round(seconds * 1000)))
        hours, milliseconds = divmod(milliseconds, 3_600_000)
        minutes, milliseconds = divmod(milliseconds, 60_000)
        secs, milliseconds = divmod(milliseconds, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"

    lines: list[str] = []
    for index, (sentence, weight) in enumerate(zip(sentences, weights), 1):
        end = duration if index == len(sentences) else cursor + duration * weight / total
        lines.extend([str(index), f"{stamp(cursor)} --> {stamp(end)}", sentence, ""])
        cursor = end
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
