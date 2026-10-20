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
    warnings: tuple[str, ...] = ()


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

            # --- 对白轨：逐句按镜头边界对齐（每句先 atempo 压进镜头剩余空间，仍超则截断并告警）---
            warnings: list[str] = []
            dialogue_path: Path | None = None
            dialogue_srt: Path | None = None
            dialogue = plan.get("dialogue") if isinstance(plan.get("dialogue"), dict) else None
            dialogue_lines = [ln for ln in (dialogue or {}).get("lines") or [] if isinstance(ln, dict)]
            if dialogue_lines:
                clip_durs = [float(probe_media(p).get("duration") or 0) for p in normalized]
                starts_acc: list[float] = []
                acc = 0.0
                for d in clip_durs:
                    starts_acc.append(acc)
                    acc += d
                clip_cursor = [0.0] * len(normalized)
                pieces: list[Path] = []
                placed_total = 0.0
                srt_items: list[dict[str, Any]] = []
                ordered = sorted(
                    ((int(ln.get("clip_index") or 0), i, ln) for i, ln in enumerate(dialogue_lines)),
                    key=lambda t: (t[0], t[1]),
                )
                for ci, i, ln in ordered:
                    text = str(ln.get("text") or "").strip()
                    if not text:
                        continue
                    if ci >= len(normalized):
                        warnings.append(f"对白第 {i + 1} 句的镜头序号越界，已跳过")
                        continue
                    offset = clip_cursor[ci]
                    room = clip_durs[ci] - offset
                    if room <= 0.2:
                        warnings.append(f"对白第 {i + 1} 句在本镜头内已无空间，已跳过")
                        continue
                    src = _inside(self.root, str(ln.get("path") or ""))
                    dur = float(probe_media(src).get("duration") or 0)
                    tempo = 1.0
                    if dur > room:
                        # 先加速（上限 1.3x），仍装不下就在镜头末尾截断——lint 的台词时长
                        # 校验保证这是少数；宁可截断也不能让台词漂进下一个镜头错位
                        tempo = min(1.3, dur / room)
                        dur = dur / tempo
                    placed = min(dur, room)
                    piece = temp / f"dl-{i:03d}.wav"
                    af = [f"atempo={tempo:.3f}"] if tempo > 1.001 else []
                    af.append("apad")
                    _run(
                        [ffmpeg, "-nostdin", "-y", "-i", str(src), "-af", ",".join(af),
                         "-t", f"{placed:.3f}", "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(piece)],
                        timeout=timeout, purpose=f"对白第 {i + 1} 句预处理",
                    )
                    target_start = starts_acc[ci] + offset
                    lead = max(0.0, target_start - placed_total)
                    if lead > 0.005:
                        silent = temp / f"dl-{i:03d}-lead.wav"
                        _run(
                            [ffmpeg, "-nostdin", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                             "-t", f"{lead:.3f}", "-c:a", "pcm_s16le", str(silent)],
                            timeout=timeout, purpose="对白前静音",
                        )
                        pieces.append(silent)
                        placed_total += lead
                    pieces.append(piece)
                    placed_total += placed
                    clip_cursor[ci] = offset + placed
                    srt_items.append({"start": target_start, "end": target_start + placed, "text": text})
                if srt_items:
                    dialogue_path = temp / "dialogue.wav"
                    dlist = temp / "dialogue-concat.txt"
                    dlist.write_text(
                        "".join(f"file '{p.as_posix()}'\n" for p in pieces), encoding="utf-8"
                    )
                    _run(
                        [ffmpeg, "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(dlist),
                         "-af", f"apad,atrim=end={acc:.3f}", "-ar", "44100", "-ac", "2",
                         "-c:a", "pcm_s16le", str(dialogue_path)],
                        timeout=timeout, purpose="拼接对白轨",
                    )
                    # 字幕用真实时间轴，且落盘到渲染目录（burn 完仍可查）
                    dialogue_srt = render_root / "dialogue.srt"
                    write_dialogue_srt(dialogue_srt, srt_items)

            command = [ffmpeg, "-nostdin", "-y", "-i", str(silent_video)]
            audio = plan.get("audio") if isinstance(plan.get("audio"), dict) else None
            subtitle = plan.get("subtitle") if isinstance(plan.get("subtitle"), dict) else None
            if dialogue_srt is not None:
                # 对白字幕优先：时间轴来自真实配音，比按字数估算的 subtitle_text 准
                subtitle = {"path": dialogue_srt}
            audio_inputs: list[str] = []
            if audio and audio.get("path"):
                audio_inputs.append(str(_inside(self.root, str(audio["path"]))))
            if dialogue_path is not None:
                audio_inputs.append(str(dialogue_path))
            # 所有 -i 必须先于输出选项（ffmpeg 按「最后一个 -i 之后」划分输出段）
            for src in audio_inputs:
                command += ["-i", src]
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
            if len(audio_inputs) == 1:
                # Pad short narration with silence so it never truncates the
                # final shot; -shortest then stops exactly with the video.
                command += [
                    "-map", "1:a:0", "-af", "apad",
                    "-c:a", "aac", "-b:a", "192k", "-shortest",
                ]
            elif len(audio_inputs) >= 2:
                # 旁白 + 对白：两路都 apad 到片长再混合，normalize=0 防止 amix 把音量减半
                command += [
                    "-filter_complex",
                    "[1:a]apad[a1];[2:a]apad[a2];[a1][a2]amix=inputs=2:normalize=0[out]",
                    "-map", "0:v:0", "-map", "[out]",
                    "-c:a", "aac", "-b:a", "192k", "-shortest",
                ]
            command += ["-movflags", "+faststart", str(temporary_final)]
            _run(command, timeout=timeout, purpose="封装成片")
            checked = probe_media(temporary_final)
            if not checked.get("video") or checked.get("duration", 0) <= 0:
                raise CompositionError("成片校验失败：没有有效视频流或时长为 0")
            os.replace(temporary_final, final_path)

        return self._result(final_path, checked, width, height, fps, warnings=tuple(warnings))

    def _result(
        self,
        path: Path,
        checked: dict[str, Any],
        width: int,
        height: int,
        fps: int,
        warnings: tuple[str, ...] = (),
    ) -> RenderResult:
        return RenderResult(
            path=path.relative_to(self.root).as_posix(),
            mime="video/mp4",
            duration=float(checked.get("duration") or 0),
            width=width,
            height=height,
            fps=fps,
            file_size=path.stat().st_size,
            sha256=sha256_file(path),
            warnings=warnings,
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


def write_dialogue_srt(path: Path, items: list[dict[str, Any]]) -> None:
    """按真实音频时间轴写对白字幕；items=[{start,end,text}]（秒）。

    与 write_srt 的区别：这里的时间来自逐句 TTS 的实测时长，不是按字数比例估算。
    """
    rows = [it for it in items if str(it.get("text") or "").strip()]
    if not rows:
        return

    def stamp(seconds: float) -> str:
        milliseconds = max(0, int(round(seconds * 1000)))
        hours, milliseconds = divmod(milliseconds, 3_600_000)
        minutes, milliseconds = divmod(milliseconds, 60_000)
        secs, milliseconds = divmod(milliseconds, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"

    lines: list[str] = []
    for index, item in enumerate(rows, 1):
        lines.extend([str(index), f"{stamp(item['start'])} --> {stamp(item['end'])}", str(item["text"]), ""])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


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
