from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.video_local_asr import LocalASRError, VAD_PARAMETERS, transcribe_local
from jianying_ai_mcp.video_observer import analyze_video, get_analysis_context


ASSETS = Path(__file__).resolve().parents[1] / "test_assets"


class VideoLocalASRTests(unittest.TestCase):
    def setUp(self) -> None:
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("ffmpeg/ffprobe unavailable")
        build = ASSETS.parent / ".build"
        build.mkdir(exist_ok=True)
        scratch = tempfile.TemporaryDirectory(dir=build, prefix="video-asr-test-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.settings = Settings(self.root, self.root, None, Path(ffmpeg), Path(ffprobe))
        self.speech = ASSETS / "speech_test.wav"

    def test_local_asr_decodes_real_wav_and_silero_gates_model(self) -> None:
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy unavailable")

        def speech_timestamps(samples, options):
            self.assertEqual(options, VAD_PARAMETERS)
            self.assertGreater(len(samples), 16_000)
            self.assertLessEqual(float(np.max(np.abs(samples))), 1)
            return [{"start": 1600, "end": 12_800}]

        def infer(samples, **kwargs):
            self.assertIsInstance(samples, np.ndarray)
            self.assertTrue(kwargs["vad_filter"])
            self.assertFalse(kwargs["condition_on_previous_text"])
            return iter([
                SimpleNamespace(text="测试语音", start=0.1, end=0.8, no_speech_prob=0.1,
                                avg_logprob=-0.3, compression_ratio=1.0),
            ]), SimpleNamespace(language="zh")

        model_options = {"model": "small", "model_revision": (), "allow_download": False, "language": None}
        vad = SimpleNamespace(VadOptions=dict, get_speech_timestamps=speech_timestamps)
        with patch.dict(sys.modules, {"faster_whisper.vad": vad}), patch(
            "jianying_ai_mcp.video_local_asr._load_model", return_value=SimpleNamespace(transcribe=infer),
        ) as model:
            result = transcribe_local(self.speech, self.settings.ffmpeg_exe, model_options)
            self.assertEqual(result["status"], "available")
            self.assertEqual(result["segments"][0]["start_ms"], 100)
            self.assertEqual(result["evidence"]["accepted_segments"], 1)
            model.assert_called_once()

            model.reset_mock()
            vad.get_speech_timestamps = lambda samples, options: []
            result = transcribe_local(self.speech, self.settings.ffmpeg_exe, model_options)
            self.assertEqual(result["status"], "no_speech_detected")
            self.assertEqual(result["segments"], [])
            model.assert_not_called()

    def test_local_asr_owns_overlap_by_midpoint(self) -> None:
        def decode(video, ffmpeg, start_ms, end_ms):
            return range((end_ms - start_ms) * 16)

        def line(start, end):
            return SimpleNamespace(text="边界语句", start=start, end=end, no_speech_prob=0.1,
                                   avg_logprob=-0.3, compression_ratio=1.0)

        options = {"model": "small", "model_revision": (), "allow_download": False, "language": None}
        vad = SimpleNamespace(VadOptions=dict)
        with patch.dict(sys.modules, {"faster_whisper.vad": vad}), patch.object(
            vad, "get_speech_timestamps", create=True,
            side_effect=[[{"start": 28_000, "end": 36_000}], [{"start": 0, "end": 8_000}], []],
        ), patch("jianying_ai_mcp.video_local_asr.WINDOW_MS", 2_000), patch(
            "jianying_ai_mcp.video_local_asr.OVERLAP_MS", 250,
        ), patch("jianying_ai_mcp.video_local_asr._decode_audio", side_effect=decode) as decode_audio, patch(
            "jianying_ai_mcp.video_local_asr._load_model",
        ) as load_model:
            load_model.return_value.transcribe.side_effect = [
                ([line(1.85, 2.15)], SimpleNamespace(language="zh")),
                ([line(0.10, 0.40)], SimpleNamespace(language="zh")),
            ]
            result = transcribe_local(self.speech, self.settings.ffmpeg_exe, options, duration_ms=4_500)

        self.assertEqual([args.args[-2:] for args in decode_audio.call_args_list], [(0, 2250), (1750, 4250), (3750, 4500)])
        self.assertEqual([(s["start_ms"], s["end_ms"]) for s in result["segments"]], [(1850, 2150)])
        self.assertEqual(result["evidence"]["audio_seconds"], 4.5)
        self.assertEqual(result["evidence"]["vad_seconds"], 0.5)
        self.assertEqual(result["evidence"]["speech_segments"], 1)
        self.assertTrue(result["evidence"]["complete"])

    def test_unavailable_local_asr_retries_without_cloud_and_shares_success(self) -> None:
        recognized = {
            "segments": [{"start_ms": 100, "end_ms": 750, "text": "测试语音"}],
            "status": "available",
            "evidence": {"speech_segments": 1},
        }
        with patch.dict(os.environ, {"JIANYING_WHISPER_MODEL": "small", "GROQ_API_KEY": "placeholder"}), patch(
            "jianying_ai_mcp.video_observer.transcribe_local", side_effect=[LocalASRError("model missing"), recognized],
        ) as local, patch("jianying_ai_mcp.video_observer.transcribe_video") as cloud:
            failed = analyze_video(self.settings, str(self.speech), detail="transcript")
            self.assertEqual(failed["transcript_status"], "unavailable")
            self.assertIn("model missing", failed["transcript_warning"])

            recovered = analyze_video(self.settings, str(self.speech), detail="transcript", transcript_limit=0)
            self.assertEqual(recovered["transcript_status"], "available")
            self.assertFalse(recovered["cache_hit"])
            self.assertEqual(recovered["transcript"], [])
            self.assertEqual(recovered["transcript_total"], 1)
            self.assertTrue(recovered["transcript_truncated"])

            ranged = analyze_video(self.settings, str(self.speech), start=0.2, end=0.7, transcription="local")
            self.assertTrue(ranged["asr_cache_hit"])
            self.assertEqual(ranged["audio_evidence"]["scope"], "source")
            self.assertEqual(ranged["transcript"], recognized["segments"])
            self.assertEqual(ranged["frames"], [])
            self.assertEqual(local.call_count, 2)
            cloud.assert_not_called()

        context = get_analysis_context(self.settings, recovered["analysis_id"])
        self.assertEqual(context["transcript"], recognized["segments"])

    def test_asr_only_ignores_sidecar(self) -> None:
        video = self.root / "speech.wav"
        shutil.copyfile(self.speech, video)
        video.with_suffix(".srt").write_text("1\n00:00:00,100 --> 00:00:00,750\n旁挂字幕\n", encoding="utf-8")
        with patch("jianying_ai_mcp.video_observer.transcribe_local", return_value={
            "segments": [], "status": "no_speech_detected", "evidence": {"speech_segments": 0},
        }) as local:
            sidecar = analyze_video(self.settings, str(video), detail="transcript", transcription="local")
            self.assertEqual(sidecar["transcript_source"], "sidecar")
            local.assert_not_called()

            audio = analyze_video(
                self.settings, str(video), detail="transcript", transcription="local", prefer_subtitles=False,
            )
            self.assertEqual(audio["transcript_source"], "whisper:local")
            self.assertEqual(audio["transcript_status"], "no_speech_detected")
            local.assert_called_once()

    def test_embedded_text_subtitles_before_asr(self) -> None:
        video = self.root / "muxed.mp4"
        captions = self.root / "captions-to-mux.srt"
        captions.write_text("1\n00:00:00,250 --> 00:00:01,500\n内嵌字幕\n", encoding="utf-8")
        subprocess.run([
            str(self.settings.ffmpeg_exe), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=white:s=160x96:r=5:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-i", str(captions),
            "-c:v", "mpeg4", "-c:a", "aac", "-c:s", "mov_text", str(video),
        ], check=True, capture_output=True)
        with patch("jianying_ai_mcp.video_observer.transcribe_local", return_value={
            "segments": [], "status": "no_speech_detected", "evidence": {"speech_segments": 0},
        }) as local:
            report = analyze_video(self.settings, str(video), detail="transcript", transcription="local")
            self.assertEqual(report["transcript_source"], "embedded:2")
            self.assertEqual(report["transcript"], [{"start_ms": 250, "end_ms": 1500, "text": "内嵌字幕"}])
            self.assertTrue(Path(report["transcript_srt_path"]).is_file())
            local.assert_not_called()
            with patch("jianying_ai_mcp.video_observer._embedded_transcript") as extract:
                cached = analyze_video(self.settings, str(video), detail="transcript", transcription="local")
                self.assertTrue(cached["cache_hit"])
                extract.assert_not_called()

            audio = analyze_video(
                self.settings, str(video), detail="transcript", transcription="local", prefer_subtitles=False,
            )
            self.assertEqual(audio["transcript_source"], "whisper:local")
            local.assert_called_once()

            video.with_suffix(".srt").write_text("1\n00:00:00,250 --> 00:00:01,500\n旁挂字幕\n", encoding="utf-8")
            sidecar = analyze_video(self.settings, str(video), detail="transcript", transcription="local")
            self.assertEqual(sidecar["transcript_source"], "sidecar")
            self.assertEqual(sidecar["transcript"][0]["text"], "旁挂字幕")

    def test_scene_candidates_survive_uniform_scaffold_and_subranges(self) -> None:
        video = self.root / "single-cut.mp4"
        subprocess.run([
            str(self.settings.ffmpeg_exe), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=160x96:rate=5:duration=3",
            "-f", "lavfi", "-i", "color=c=white:s=160x96:r=5:d=3",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0", "-c:v", "mpeg4",
            "-g", "100", "-sc_threshold", "1000000000", str(video),
        ], check=True, capture_output=True)
        whole = analyze_video(
            self.settings, str(video), detail="balanced", max_frames=5, transcription="none",
        )
        self.assertLessEqual(whole["frame_count"], 5)
        self.assertTrue(any(item["reason"] == "scene" and item["time_ms"] == 3000 for item in whole["frames"]))
        self.assertLess(whole["max_frame_gap_ms"], whole["range"]["end_ms"])

        local = analyze_video(
            self.settings, str(video), detail="efficient", start=0.6, end=2.2, max_frames=4, transcription="none",
        )
        self.assertTrue(local["frames"])
        self.assertTrue(all(600 <= item["time_ms"] <= 2200 for item in local["frames"]))


if __name__ == "__main__":
    unittest.main()
