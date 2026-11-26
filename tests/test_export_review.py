from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.export_review import ExportReviewError, review_export


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "test_assets"


class ExportReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = Settings.from_env()
        if settings.ffmpeg_exe is None or settings.ffprobe_exe is None:
            self.skipTest("本机没有 ffmpeg 或 ffprobe")
        build = ROOT / ".build"
        build.mkdir(exist_ok=True)
        temporary = self.enterContext(tempfile.TemporaryDirectory(dir=build))
        self.settings = replace(settings, work_root=Path(temporary))

    def test_video_range_audio_metrics_and_local_frame_paths(self) -> None:
        path = ASSETS / "compat_test_video.mp4"
        modified = path.stat().st_mtime_ns
        result = review_export(self.settings, str(path), start="1s", end="3s", max_frames=4, target_loudness=-14)

        self.assertTrue(result["success"])
        self.assertEqual(result["range"], {"start_ms": 1000, "end_ms": 3000})
        self.assertEqual(result["black_segments"], [])
        frame_times = [frame["time_ms"] for frame in result["frames"]]
        self.assertEqual(frame_times, [1000, 1500, 2000, 2500])
        for sample in result["frames"]:
            frame = Path(sample["path"])
            self.assertTrue(frame.is_relative_to(self.settings.work_root / "export_reviews"))
            self.assertGreater(frame.stat().st_size, 0)

        audio = result["audio"]
        self.assertTrue(audio["has_audio"])
        self.assertAlmostEqual(audio["integrated_lufs"], -21.8, delta=1)
        self.assertAlmostEqual(audio["true_peak_dbtp"], -17.6, delta=1)
        self.assertAlmostEqual(audio["difference_from_target_lu"], audio["integrated_lufs"] + 14, places=1)
        self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_black_segments_include_end_after_thumbnail_cap_without_audio(self) -> None:
        path = self.settings.work_root / "silent black and blue.mp4"
        command = [str(self.settings.ffmpeg_exe), "-hide_banner", "-loglevel", "error"]
        for color, duration in [("black", 1), ("blue", 2), ("black", 1)]:
            command.extend(["-f", "lavfi", "-i", f"color={color}:s=128x96:r=30:d={duration}"])
        subprocess.run([
            *command, "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0", "-an", "-c:v", "mpeg4", str(path),
        ], check=True, capture_output=True, timeout=30)

        report = review_export(self.settings, str(path), start=0.4, end=3.8, max_frames=1)
        self.assertEqual(len(report["frames"]), 1)
        lead, tail = report["black_segments"]
        self.assertEqual(lead, {"start_ms": 400, "end_ms": 1000, "duration_ms": 600})
        self.assertEqual(tail["start_ms"], 3000)
        self.assertLessEqual(abs(tail["end_ms"] - 3800), 34)
        self.assertFalse(report["audio"]["has_audio"])
        self.assertIsNone(report["audio"]["integrated_lufs"])
        self.assertIsNone(report["audio"]["true_peak_dbtp"])

    def test_invalid_range_and_non_video_fail_before_output(self) -> None:
        video = ASSETS / "compat_test_video.mp4"
        for kwargs in [{"start": 4, "end": 3}, {"end": float("nan")}, {"end": 9}, {"max_frames": 31}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ExportReviewError):
                review_export(self.settings, str(video), **kwargs)
        with self.assertRaisesRegex(ExportReviewError, "视频文件"):
            review_export(self.settings, str(ASSETS / "compat_test_image.png"))
        self.assertFalse((self.settings.work_root / "export_reviews").exists())
