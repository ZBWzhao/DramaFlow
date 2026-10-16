"""Executable regression for the local AIGC composition loop."""

from __future__ import annotations

import tempfile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.canvas import Canvas
from core.composition import CompositionService, ffmpeg_capabilities, probe_media, write_srt
from core.media import MockProvider
from core.tts import TTSRegistry


def main() -> None:
    capability = ffmpeg_capabilities()
    assert capability["available"], capability["reason"]
    with tempfile.TemporaryDirectory(prefix="dramaflow-composition-") as directory:
        root = Path(directory)
        provider = MockProvider()
        first = provider.generate("video", {"prompt": "镜头一", "size": "640x360", "duration": 2}, root)
        second = provider.generate("video", {"prompt": "镜头二", "size": "640x360", "duration": 2}, root)
        narration = TTSRegistry(root).synthesize({"provider": "mock", "text": "第一幕。第二幕。"}, "test")
        subtitle = root / "assets" / "test.srt"
        write_srt(subtitle, "第一幕。第二幕。", 4)

        canvas = Canvas("test", root / "test.canvas.json")
        video_a = canvas.add_node("video", "镜头一")
        video_a.outputs["video"] = {"data_type": "VIDEO", "value": {"path": first.path, "mime": first.mime}}
        video_b = canvas.add_node("video", "镜头二")
        video_b.outputs["video"] = {"data_type": "VIDEO", "value": {"path": second.path, "mime": second.mime}}
        audio = canvas.add_node("audio", "旁白")
        audio.outputs["audio"] = {"data_type": "AUDIO", "value": {"path": narration.path, "mime": narration.mime}}
        timeline = canvas.add_node(
            "timeline", "最终成片",
            data={"clip_order": [video_b.id, video_a.id], "output": {"width": 640, "height": 360, "fps": 24}},
        )
        canvas.connect(video_a.id, "video", timeline.id, "video")
        canvas.connect(video_b.id, "video", timeline.id, "video")
        canvas.connect(audio.id, "audio", timeline.id, "audio")
        plan = canvas.timeline_jobs()[0]
        assert [clip["node_id"] for clip in plan["clips"]] == [video_b.id, video_a.id]
        assert plan["ready"] and plan["audio"]["path"] == narration.path

        rendered = CompositionService(root).render(
            {
                "clips": [{"path": clip["path"]} for clip in plan["clips"]],
                "audio": {"path": narration.path},
                "subtitle": {"path": "assets/test.srt"},
                "output": plan["output"],
            },
            "test-job",
        )
        inspected = probe_media(root / rendered.path)
        assert inspected["video"] and inspected["audio"]
        assert 3.9 <= inspected["duration"] <= 4.2, inspected
        assert rendered.width == 640 and rendered.height == 360 and rendered.fps == 24
        print("PASS mock MP4 -> ordered timeline -> TTS/subtitle -> validated final MP4")


if __name__ == "__main__":
    main()
