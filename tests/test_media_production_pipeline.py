from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jianying_ai_mcp.asset_batch import analyze_media_batch, get_media_batch_context
from jianying_ai_mcp.audio_production import create_music_bed, verify_audio_dialogue
from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.media_prepare import prepare_media_assets
from jianying_ai_mcp.server import mcp
from jianying_ai_mcp.video_animation import create_video_animation


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "test_assets"


class MediaProductionPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        work = ROOT / ".build"
        work.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work, prefix="media-production-test-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.settings = replace(Settings.from_env(), work_root=self.folder / "work", draft_root=self.folder / "drafts")

    def test_public_mcp_tools_are_registered(self) -> None:
        expected = {
            "analyze_media_batch", "get_media_batch_context", "prepare_media_assets", "verify_audio_dialogue",
            "synthesize_narration", "create_music_bed", "create_video_graphic", "create_video_animation", "compile_video_timeline",
        }
        self.assertTrue(expected.issubset({tool.name for tool in mcp._tool_manager.list_tools()}))

    def test_batch_retains_full_dialogue_and_only_sends_bounded_excerpts(self) -> None:
        if self.settings.ffprobe_exe is None or self.settings.ffmpeg_exe is None:
            self.skipTest("需要 FFmpeg 和 ffprobe")
        video = self.folder / "口播.mp4"
        shutil.copyfile(ASSETS / "compat_test_video.mp4", video)
        video.with_suffix(".srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\n今天讨论外包\n\n"
            "2\n00:00:06,000 --> 00:00:07,000\n交接文件是重点\n", encoding="utf-8",
        )
        result = analyze_media_batch(
            self.settings, [str(video), str(ASSETS / "compat_test_image.png"), str(video)],
            transcription="none", max_preview_frames=1, transcript_chars=5,
        )
        self.assertEqual(result["source_count"], 2)
        self.assertLessEqual(sum(len(cue["text"]) for cue in result["dialogue_evidence"]), 5)
        self.assertTrue(Path(result["preview_sheet_path"]).is_file())
        found = get_media_batch_context(self.settings, result["batch_id"], query="交接")
        self.assertEqual(found["dialogue_evidence"][0]["start_ms"], 6000)
        self.assertEqual(found["dialogue_evidence"][0]["text"], "交接文件是重点")

    def test_prepare_bakes_rotation_and_extracts_real_audio(self) -> None:
        if self.settings.ffprobe_exe is None or self.settings.ffmpeg_exe is None:
            self.skipTest("需要 FFmpeg 和 ffprobe")
        rotated = self.folder / "rotated.mp4"
        subprocess.run([
            str(self.settings.ffmpeg_exe), "-hide_banner", "-loglevel", "error", "-display_rotation", "90",
            "-i", str(ASSETS / "compat_test_video.mp4"), "-t", "0.5", "-c", "copy", str(rotated),
        ], check=True, capture_output=True)
        row = prepare_media_assets(self.settings, [str(rotated)], extract_audio=True)["items"][0]
        self.assertNotIn("error", row)
        self.assertEqual(row["media_info"]["height"], row["prepared_info"]["width"])
        self.assertEqual(row["media_info"]["width"], row["prepared_info"]["height"])
        result = prepare_media_assets(self.settings, [str(ASSETS / "speech_test.wav")], normalize=False, extract_audio=True)
        with wave.open(result["items"][0]["audio_path"], "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getframerate()), (1, 16000))

    def test_animation_packages_resolved_style_and_escapes_content(self) -> None:
        result = create_video_animation(
            self.settings, title="标题 <试>", motion="count_up", value=24, unit="</script>",
            colors={"accent": "#883322"},
        )
        scene = Path(result["scene_path"]).read_text(encoding="utf-8")
        self.assertIn("标题 &lt;试&gt;", scene)
        self.assertIn('"unit": "\\u003c/script>"', scene)
        self.assertIn("--accent: #883322", scene)
        self.assertIn("window.film", scene)
        self.assertIn('src="scene.html#t=0,3.000000"', Path(result["film_path"]).read_text(encoding="utf-8"))
        self.assertTrue(any(item["roles"] == ["accent_text", "accent"] for item in result["design_findings"]))

        custom = '<script>window.film={duration:3,frame(t){document.title=String(t)}}</script>'
        custom_result = create_video_animation(self.settings, scene_html=custom)
        self.assertEqual(Path(custom_result["scene_path"]).read_text(encoding="utf-8"), custom)

    def test_dialogue_check_uses_audio_even_with_a_burn_in_script_nearby(self) -> None:
        report = self.folder / "analysis.json"
        report.write_text(json.dumps({"transcript": [
            {"text": "今天讨论交接。", "start_ms": 1000, "end_ms": 2200},
            {"text": "去看完整清单。", "start_ms": 7900, "end_ms": 8500},
        ]}, ensure_ascii=False), encoding="utf-8")
        with patch("jianying_ai_mcp.audio_production.analyze_video", return_value={
            "analysis_id": "sample", "report_path": str(report), "transcript_status": "available",
        }) as observer:
            result = verify_audio_dialogue(self.settings, str(ASSETS / "speech_test.wav"), [
                {"text": "今天，讨论交接。", "end_ms": 2500},
                {"text": "去看完整清单", "end_ms": 2500},
            ])
        self.assertEqual([row["status"] for row in result["checks"]], ["matched", "not_matched"])
        self.assertFalse(observer.call_args.kwargs["prefer_subtitles"])

    def test_local_music_has_exact_duration(self) -> None:
        output = create_music_bed(self.settings, 1.25)
        with wave.open(output["audio_path"], "rb") as wav:
            self.assertEqual(wav.getnframes(), 30000)
            self.assertNotEqual(set(wav.readframes(30000)), {0})
