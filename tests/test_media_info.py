from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.media_info import MediaProbeError, parse_media_info


ASSETS = Path(__file__).resolve().parents[1] / "test_assets"


class MediaInfoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings.from_env()

    def test_local_video_audio_and_image_need_only_ffprobe(self) -> None:
        if self.settings.ffprobe_exe is None:
            self.skipTest("本机没有 ffprobe")
        settings = replace(self.settings, ffmpeg_exe=None)
        video = parse_media_info(settings, str(ASSETS / "compat_test_video.mp4"))
        self.assertTrue(video["success"])
        self.assertEqual(video["media_path"], str(ASSETS / "compat_test_video.mp4"))
        info = video["media_info"]
        self.assertEqual(info["type"], "video")
        self.assertEqual(info["duration_ms"], 8000)
        self.assertEqual(info["resolution"], "1280x720")
        self.assertEqual(info["frame_rate"], 30)
        self.assertTrue(info["has_audio"])

        audio = parse_media_info(settings, str(ASSETS / "compat_test_music.wav"))["media_info"]
        self.assertEqual(audio["type"], "audio")
        self.assertEqual(audio["duration"], 8)
        self.assertEqual((audio["sample_rate"], audio["channels"]), (48000, 1))
        self.assertFalse(audio["has_video"])

        image = parse_media_info(settings, str(ASSETS / "compat_test_image.png"))["media_info"]
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["resolution"], "1280x720")
        self.assertEqual(image["image_codec"], "png")
        self.assertIsNone(image["duration"])
        self.assertIsNone(image["frame_rate"])

    def test_cover_art_does_not_classify_audio_as_video(self) -> None:
        media = ASSETS / "compat_test_music.wav"
        settings = replace(self.settings, ffprobe_exe=media)
        with patch("jianying_ai_mcp.media_info._probe_streams", return_value={
            "format": {"format_name": "mp3", "duration": "N/A"},
            "streams": [
                {"codec_type": "audio", "codec_name": "mp3", "duration": "2.5"},
                {"codec_type": "video", "disposition": {"attached_pic": 1}},
            ],
        }) as probe:
            info = parse_media_info(settings, str(media))["media_info"]
        probe.assert_called_once_with(media, media, local_only=True)
        self.assertEqual(info["type"], "audio")
        self.assertEqual(info["duration_ms"], 2500)
        self.assertIsNone(info["video_codec"])

    def test_rejects_urls_and_reports_missing_ffprobe(self) -> None:
        settings = replace(self.settings, ffprobe_exe=None)
        with self.assertRaisesRegex(MediaProbeError, "本地绝对路径"):
            parse_media_info(settings, "https://example.com/video.mp4")
        with self.assertRaisesRegex(MediaProbeError, "JIANYING_FFPROBE"):
            parse_media_info(settings, str(ASSETS / "compat_test_video.mp4"))


if __name__ == "__main__":
    unittest.main()
