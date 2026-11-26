from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jianying_ai_mcp.audio_production import master_video_audio, verify_audio_dialogue
from jianying_ai_mcp.config import Settings


class AudioProductionTests(unittest.TestCase):
    def setUp(self) -> None:
        build = Path(__file__).resolve().parents[1] / ".build"
        build.mkdir(exist_ok=True)
        scratch = self.enterContext(tempfile.TemporaryDirectory(prefix="audio-test-", dir=build))
        self.root = Path(scratch)
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        self.settings = Settings(
            self.root / "drafts", self.root / "work", None,
            Path(ffmpeg) if ffmpeg else None, Path(ffprobe) if ffprobe else None,
        )

    def _ffmpeg(self, *args: str) -> str:
        if self.settings.ffmpeg_exe is None or self.settings.ffprobe_exe is None:
            self.skipTest("FFmpeg and ffprobe are unavailable")
        result = subprocess.run(
            [str(self.settings.ffmpeg_exe), "-hide_banner", "-loglevel", "error", *args],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    def test_master_preserves_video_stream_and_remeasures_aac(self) -> None:
        source = self.root / "source.mp4"
        self._ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=160x96:rate=12:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=6", "-c:v", "mpeg4", "-c:a", "aac", str(source),
        )
        result = master_video_audio(self.settings, str(source), target_loudness=-16, true_peak=-1.5)
        output = Path(result["mp4_path"])
        self.assertNotEqual(source, output)
        self.assertTrue(output.is_file())
        self.assertTrue(result["master_meets_targets"])
        self.assertGreater(abs(result["before"]["integrated_lufs"] + 16), 1)
        self.assertAlmostEqual(result["after"]["integrated_lufs"], -16, delta=0.5)
        self.assertLessEqual(result["after"]["true_peak_dbtp"], -1.5)

        def video_hash(path: Path) -> str:
            return self._ffmpeg("-i", str(path), "-map", "0:V:0", "-c", "copy", "-f", "md5", "-")

        self.assertEqual(video_hash(source), video_hash(output))

    def test_master_reports_missing_audio(self) -> None:
        source = self.root / "silent-video.mp4"
        self._ffmpeg("-f", "lavfi", "-i", "color=c=white:s=160x96:r=5:d=1", "-c:v", "mpeg4", str(source))
        with self.assertRaisesRegex(ValueError, "没有音轨"):
            master_video_audio(self.settings, str(source))

    def test_dialogue_matches_long_cue_and_flags_approximate_or_homophone(self) -> None:
        source = self.root / "mixed.wav"
        source.touch()
        report = self.root / "analysis.json"
        report.write_text(json.dumps({"transcript": [
            {"start_ms": 100, "end_ms": 6000, "text": "先看例子。" * 80 + "內部定標準，外包做產能。"},
            {"start_ms": 8000, "end_ms": 12000, "text": "先聊一下產品。" * 70 + "點擊走停留時長和轉化率。"},
            {"start_ms": 14000, "end_ms": 19000, "text": "我们的襄薰绝不含酒精。"},
            {"start_ms": 21000, "end_ms": 24000, "text": "先找品牌负责人。", "quality": "review"},
        ]}, ensure_ascii=False), encoding="utf-8")
        expected = [
            {"text": "内部定标准，外包做产能", "start_ms": 0, "end_ms": 7000},
            {"text": "点击率，停留时长和转化率", "start_ms": 7500, "end_ms": 13000},
            {"text": "我们的香薰绝不含酒精", "start_ms": 13500, "end_ms": 20000},
            {"text": "先找品牌负责人", "start_ms": 20500, "end_ms": 24500},
        ]
        with patch("jianying_ai_mcp.audio_production.analyze_video", return_value={
            "analysis_id": "fixture", "report_path": str(report), "transcript_status": "available",
        }) as analyze:
            checked = verify_audio_dialogue(self.settings, str(source), expected)["checks"]
        self.assertFalse(analyze.call_args.kwargs["prefer_subtitles"])
        self.assertEqual([item["status"] for item in checked], ["matched", "review", "review", "review"])
        self.assertEqual(checked[0]["evidence"]["matched_text"], "内部定标准外包做产能")
        self.assertEqual(checked[1]["match_method"], "approximate_text")
        self.assertEqual(checked[2]["review_reason"], "homophone_only")
        self.assertEqual(checked[3]["review_reason"], "asr_low_confidence")
        self.assertTrue(all(item["needs_manual_review"] for item in checked[1:]))


if __name__ == "__main__":
    unittest.main()
