from __future__ import annotations

import json
import math
import shutil
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from PIL import Image

from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.draft_ops import build_manifest
from jianying_ai_mcp.production import ProductionError, compile_video_timeline
from jianying_ai_mcp.production_graphics import create_video_graphic


class ProductionTest(unittest.TestCase):
    def setUp(self):
        build = Path(__file__).resolve().parents[1] / ".build"
        build.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=build, prefix="production-test-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        self.settings = Settings(root / "drafts", root / "work", None, Path(ffmpeg) if ffmpeg else None,
                                 Path(ffprobe) if ffprobe else None)
        self.picture = root / "picture.png"
        image = Image.new("RGB", (480, 320), "#D8343A")
        image.paste("#334EF0", (220, 0, 480, 320))
        image.save(self.picture)
        self.voice = root / "voice.wav"
        self._tone(self.voice, 24_000, 660)
        self.music = root / "music.wav"
        self._tone(self.music, 14_400, 440)

    def _tone(self, target: Path, samples: int, hz: int) -> None:
        with wave.open(str(target), "wb") as output:
            output.setparams((1, 2, 48_000, 0, "NONE", "not compressed"))
            values = [round(8_000 * math.sin(2 * math.pi * hz * i / 48_000)) for i in range(samples)]
            output.writeframes(struct.pack(f"<{samples}h", *values))

    def test_graphic_crops_source_and_exports_alpha_overlay(self):
        card = create_video_graphic(
            self.settings, "cover", "When to outsource", subtitle="Three checks", image_path=str(self.picture),
            image_focus=(1, 0.5), width=480, height=800, colors={"background": "#112233", "foreground": "#EEEEEE"},
        )
        with Image.open(card["image_path"]) as png, Image.open(card["preview_path"]) as jpg:
            self.assertEqual(png.getpixel((0, 0)), (17, 34, 51))
            self.assertGreater(png.getpixel((380, 150))[2], 200)
            self.assertEqual(jpg.format, "JPEG")
            self.assertLessEqual(jpg.height, 720)

        overlay = create_video_graphic(self.settings, "lower_third", "Speaker Name", subtitle="Producer", width=480, height=800)
        with Image.open(overlay["image_path"]) as png:
            self.assertEqual(png.getpixel((0, 0))[3], 0)
            self.assertEqual(png.getchannel("A").getextrema(), (0, 255))

    def test_reference_bgm_preserves_library_track_loop_and_dialogue_ducking(self):
        reference = self.picture.parent / "audio-reference.json"
        reference.write_text(json.dumps({"tracks": [], "materials": {"audios": [
            {"id": "music-row", "name": "Library music fixture", "type": "music", "music_id": "12345",
             "path": "<cloud://12345>", "duration": 300_000},
            {"id": "sound-row", "name": "Library cue fixture", "type": "sound", "effect_id": "98765",
             "path": str(self.music), "duration": 300_000},
        ]}}), encoding="utf-8")
        bgm = {"jianying_audio": {"draft_path": str(reference), "material_id": "music-row"},
               "volume": 0.4, "duck_volume": 0.1, "loop": True}
        effect = {"jianying_audio": {"draft_path": str(reference), "material_id": "sound-row"},
                  "start_frame": 15, "frames": 2}
        shots = [{"source": str(self.picture), "frames": 25}]
        result = compile_video_timeline(
            self.settings, "曲库测试", shots, fps=25, bgm=bgm, audio=[effect], include_manifest=True,
            captions=[{"text": "测试压低配乐", "start_frame": 5, "end_frame": 10, "duck_bgm": True}],
        )
        self.assertIsNone(result["bgm_path"])
        manifest = result["manifest"]
        music = next(track["segments"] for track in manifest["tracks"] if track["name"] == "配乐")
        self.assertEqual([(s["start"], s["source_start"], s["volume"]) for s in music], [
            (0, 0, 0.4), (200_000, 200_000, 0.1), (300_000, 0, 0.1),
            (400_000, 100_000, 0.4), (600_000, 0, 0.4), (900_000, 0, 0.4),
        ])
        built = build_manifest(self.settings, manifest, dry_run=False)
        output = json.loads((Path(built["draft_path"]) / "draft_content.json").read_text(encoding="utf-8"))
        audios = output["materials"]["audios"]
        self.assertEqual(sum(row.get("music_id") == "12345" and row["type"] == "music" for row in audios), 1)
        self.assertTrue(any(row.get("effect_id") == "98765" and row["type"] == "sound" for row in audios))
        self.assertTrue(all("jianying_audio" in segment and "source" not in segment for segment in music))

        if self.settings.ffmpeg_exe:
            with self.assertRaisesRegex(ProductionError, "需要可读取的本地音频"):
                compile_video_timeline(self.settings, "曲库混音", shots, fps=25, bgm=bgm, mixdown=True)
            local = compile_video_timeline(self.settings, "音效混音", shots, fps=25, audio=[effect], mixdown=True)
            with wave.open(local["mix_path"], "rb") as mixed:
                self.assertEqual(mixed.getnframes(), 48_000)

    def test_editorial_packaging_builds_native_resources_and_portable_chapters(self):
        if not self.settings.ffmpeg_exe:
            self.skipTest("ffmpeg is required to build an OpenFilm project")
        video = Path(__file__).resolve().parents[1] / "test_assets" / "compat_test_video.mp4"
        shots = [
            {"source": str(video), "frames": 20, "chapter": "先看需求", "volume": 0},
            {"source": str(video), "source_start_frame": 10, "frames": 27, "chapter": "小单验证", "volume": 0},
            {"source": str(video), "source_start_frame": 20, "frames": 20, "chapter": "小单验证", "volume": 0},
        ]
        result = compile_video_timeline(
            self.settings, "设计预设实测", shots, width=320, height=568, fps=25,
            captions=[{"start_frame": 2, "end_frame": 20, "text": "先看需求是否说清"}],
            srt_mode="native", style_preset="editorial", portable_caption_animation="fade",
            openfilm_project=True, include_manifest=True,
        )
        self.assertEqual(result["design"]["chapter_starts_s"], [0, 0.8])
        self.assertEqual(result["design"]["native_transitions"], 1)
        self.assertEqual(result["design"]["portable_transitions"], 1)
        self.assertEqual(result["manifest"]["tracks"][0]["segments"][0]["transition"]["name"], "闪黑")
        self.assertEqual(result["manifest"]["srt_imports"][0]["decoration"]["font"], "HarmonyOS_Sans_SC_Bold")
        build_manifest(self.settings, result["manifest"], dry_run=True)
        project = Path(result["openfilm_project_path"])
        film = (project / "film.html").read_text(encoding="utf-8")
        self.assertIn("transition-", film)
        self.assertIn("captions.vtt", film)
        self.assertIn("::cue", (project / "captions.vtt").read_text(encoding="utf-8"))

    def test_portable_transition_requires_two_adjacent_shots_and_even_frame_span(self):
        source = str(self.picture)
        with self.assertRaisesRegex(ProductionError, "最后一个镜头"):
            compile_video_timeline(self.settings, "末尾", [
                {"source": source, "frames": 20, "portable_transition": {"kind": "dip", "frames": 8}},
            ])
        with self.assertRaisesRegex(ProductionError, "偶数帧"):
            compile_video_timeline(self.settings, "不合适的区间", [
                {"source": source, "frames": 20, "portable_transition": {"kind": "wipe_left", "frames": 5}},
                {"source": source, "frames": 30},
            ])

    def test_interview_composition_and_caption_packaging_build_both_backends(self):
        if not self.settings.ffmpeg_exe:
            self.skipTest("ffmpeg is required to crop the video panels")
        video = Path(__file__).resolve().parents[1] / "test_assets" / "compat_test_video.mp4"
        result = compile_video_timeline(
            self.settings, "访谈构图", [
                {"source": str(self.picture), "frames": 25},
                {"source": str(video), "frames": 25, "source_start_frame": 3,
                 "layout": {"kind": "stacked", "top": {"source": str(video), "source_start_frame": 2}},
                 "overlays": [{"role": "person", "text": "受访者", "frames": 18}]},
                {"source": str(video), "frames": 25, "source_start_frame": 4,
                 "layout": {"kind": "interview_box", "top": {"source": str(self.picture), "focus": [1, 0.5]},
                            "background": "#E9DFCC"},
                 "overlays": [{"role": "emphasis", "text": "短句", "frames": 20},
                              {"role": "section", "text": "小节", "frames": 16}]},
            ], fps=25, width=320, height=568, srt_mode="native", caption_theme="interview",
            captions=[{"start_frame": 27, "end_frame": 60, "text": "白字<b>黄字", "highlights": ["黄字"]}],
            include_manifest=True, openfilm_project=True,
        )
        self.assertEqual(result["design"]["layouts"], {"full": 1, "stacked": 1, "interview_box": 1})
        self.assertEqual(result["design"]["callouts"], {"person": 1, "emphasis": 1, "section": 1})
        self.assertEqual(result["design"]["highlighted_cues"], 1)
        manifest = result["manifest"]
        self.assertEqual(manifest["draft"]["name"], "访谈构图")
        tracks = {track["name"]: track["segments"] for track in manifest["tracks"]}
        self.assertEqual(tracks["主画面"][0]["source"], str(self.picture))
        self.assertEqual([clip["volume"] for clip in tracks["分屏上方画面"]], [0, 0])
        self.assertEqual(tracks["主画面"][1]["volume"], 1)
        self.assertEqual(tracks["分屏上方画面"][0]["panel_rect"], [0, 0, 320, 284])
        lower = tracks["主画面"][2]
        self.assertGreater(lower["panel_rect"][0], 0)
        self.assertEqual(lower["source_start"], 0)
        self.assertEqual(manifest["srt_imports"][0]["highlights"], [{"cue": 1, "ranges": [[5, 7]]}])
        self.assertEqual(manifest["srt_imports"][0]["style"]["color"], [1, 1, 1])
        self.assertIn("shadow", manifest["srt_imports"][0]["decoration"])
        built = build_manifest(self.settings, manifest, dry_run=False)
        self.assertEqual(built["summary"]["duration_us"], 3_000_000)
        draft_path = Path(built["draft_path"]) / "draft_content.json"
        draft = json.loads(draft_path.read_text(encoding="utf-8"))
        subtitle = next(item for item in draft["materials"]["texts"] if item.get("recognize_task_id"))
        self.assertEqual(json.loads(subtitle["content"])["text"], "白字<b>黄字")
        project = Path(result["openfilm_project_path"])
        vtt = (project / "captions.vtt").read_text(encoding="utf-8")
        self.assertIn("<c.jianying-highlight>黄字</c>", vtt)
        self.assertIn("text-shadow:", vtt)
        titles = [file.read_text(encoding="utf-8") for file in (project / "scenes").glob("*.html")]
        self.assertEqual(sum("jianying-portable-enter" in page and "jianying-portable-exit" in page for page in titles), 3)

    def test_editing_modules_preserve_style_and_position_overrides(self):
        result = compile_video_timeline(
            self.settings, "模块覆盖", [{
                "source": str(self.picture), "frames": 30, "layout": {"kind": "full"},
                "overlays": [
                    {"role": "person", "text": "负责人", "frames": 15,
                     "style": {"size": 7}, "clip": {"transform_x": -.25}},
                    {"role": "person", "text": "受访者", "offset_frame": 15},
                ],
            }],
            captions=[{"start_frame": 0, "end_frame": 30, "text": "保留样式"}],
            style_preset="documentary", caption_theme="interview",
            caption_style={"size": 9}, caption_clip={"transform_y": -.5},
            include_manifest=True,
        )
        manifest = result["manifest"]
        self.assertEqual(result["design"]["layouts"], {"full": 1})
        subtitle = manifest["srt_imports"][0]
        self.assertEqual(subtitle["style"]["size"], 9)
        self.assertEqual(subtitle["style"]["color"], [1, 1, 1])
        self.assertEqual(subtitle["style"]["max_line_width"], .82)
        self.assertEqual(subtitle["clip"]["transform_y"], -.5)
        self.assertEqual(subtitle["decoration"]["font"], "思源中宋")
        self.assertNotIn("background", subtitle["decoration"])
        first, second = next(track["segments"] for track in manifest["tracks"] if track["type"] == "text")
        self.assertEqual(first["style"]["size"], 7)
        self.assertEqual(first["clip"], {"transform_x": -.25, "transform_y": -.28})
        self.assertEqual(second["style"]["size"], 6)
        self.assertEqual(second["clip"]["transform_x"], -.47)

    def test_compiles_exact_boundaries_and_buildable_editable_tracks(self):
        shots = [
            {"source": str(self.picture), "frames": 17, "overlays": [
                {"text": "Editable title", "frames": 17}, {"source": str(self.picture), "offset_frame": 6, "frames": 3},
            ]},
            {"source": str(self.picture), "frames": 18},
        ]
        result = compile_video_timeline(
            self.settings, "Shared timeline", shots, fps=29,
            narration=[{"source": str(self.voice), "start_frame": 0, "text": "Narration words"}],
            captions=[{"start_frame": 17, "end_frame": 35, "text": "Last subtitle"}],
            srt_mode="native", preview_frames=2,
        )
        self.assertNotIn("manifest", result)
        self.assertTrue(Path(result["preview_path"]).is_file())
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        segments = manifest["tracks"][0]["segments"]
        self.assertEqual(segments[0]["duration"], segments[1]["start"])
        self.assertEqual(segments[1]["start"] + segments[1]["duration"], result["duration_us"])
        self.assertIn("00:00:01,206", Path(result["srt_path"]).read_text(encoding="utf-8"))
        built = build_manifest(self.settings, manifest, dry_run=True)
        self.assertEqual(built["summary"]["duration_us"], result["duration_us"])
        self.assertEqual(built["summary"]["track_types"].count("text"), 2)

    def test_reports_overlapping_subtitles_without_creating_deliverables(self):
        with self.assertRaisesRegex(ProductionError, "字幕第 2 段重叠"):
            compile_video_timeline(
                self.settings, "Overlay", [{"source": str(self.picture), "frames": 30}],
                captions=[{"start_frame": 0, "end_frame": 15, "text": "One"}, {"start_frame": 14, "end_frame": 20, "text": "Two"}],
            )
        folder = self.settings.work_root / "production" / "timelines"
        self.assertEqual(list(folder.iterdir()), [])

    def test_bgm_loop_ducking_and_mix_preserve_exact_audio_length(self):
        if self.settings.ffmpeg_exe is None:
            self.skipTest("FFmpeg is unavailable")
        result = compile_video_timeline(
            self.settings, "Ducking", [{"source": str(self.picture), "frames": 60}],
            narration=[{"source": str(self.voice), "start_frame": 24}],
            bgm={"source": str(self.music), "volume": 0.8, "duck_volume": 0.2},
            mixdown=True, include_manifest=True,
        )
        with wave.open(result["bgm_path"], "rb") as stem, wave.open(result["mix_path"], "rb") as mix:
            self.assertEqual(stem.getnframes(), 96_000)
            self.assertEqual(mix.getnframes(), 96_000)
            self.assertEqual(mix.getframerate(), 48_000)

            def rms(start: float) -> float:
                stem.setpos(round(start * 48_000))
                values = struct.unpack("<9600h", stem.readframes(4_800))
                return math.sqrt(sum(value * value for value in values) / len(values))

            self.assertLess(rms(0.95), rms(0.3) * 0.35)
            self.assertGreater(rms(1.65), rms(0.3) * 0.85)
        built = build_manifest(self.settings, result["manifest"], dry_run=True)
        self.assertEqual(built["summary"]["duration_us"], 2_000_000)

    def test_preview_can_use_the_final_single_frame_of_a_video(self):
        if self.settings.ffmpeg_exe is None:
            self.skipTest("FFmpeg is unavailable")
        video = Path(__file__).resolve().parents[1] / "test_assets" / "compat_test_video.mp4"
        result = compile_video_timeline(
            self.settings, "Last frame", [{"source": str(video), "source_start_frame": 239, "frames": 1}],
            preview_frames=1,
        )
        self.assertTrue(Path(result["preview_path"]).is_file())

    def test_source_transcript_is_trimmed_and_retimed_and_explicit_cues_win(self):
        video = Path(__file__).resolve().parents[1] / "test_assets" / "compat_test_video.mp4"
        srt = self.picture.parent / "source.srt"
        srt.write_text(
            "1\n00:00:00,700 --> 00:00:01,200\nAt the source cut\n\n"
            "2\n00:00:02,000 --> 00:00:02,600\nReplace in the timeline\n\n"
            "3\n00:00:04,000 --> 00:00:05,600\nOutside the source cut\n",
            encoding="utf-8",
        )
        result = compile_video_timeline(
            self.settings, "Transcript", [{"source": str(self.picture), "frames": 15}, {
                "source": str(video), "source_start_frame": 30, "source_frames": 60, "frames": 30,
                "transcript_srt_path": str(srt),
            }], captions=[{"start_frame": 31, "end_frame": 40, "text": "Explicit caption"}],
        )
        timeline = json.loads(Path(result["timeline_path"]).read_text(encoding="utf-8"))
        self.assertEqual(result["source_caption_count"], 1)
        self.assertEqual(result["source_caption_omitted_count"], 1)
        self.assertEqual(timeline["captions"], [
            {"start_frame": 15, "end_frame": 18, "text": "At the source cut"},
            {"start_frame": 31, "end_frame": 40, "text": "Explicit caption"},
        ])

    def test_openfilm_project_preserves_tracks_and_local_assets(self):
        if self.settings.ffmpeg_exe is None or self.settings.ffprobe_exe is None:
            self.skipTest("FFmpeg and ffprobe are unavailable")
        video = Path(__file__).resolve().parents[1] / "test_assets" / "compat_test_video.mp4"
        shots = [{"source": str(video), "frames": 17, "volume": 0, "overlays": [
            {"text": "<Text> & title", "offset_frame": 3, "frames": 9},
        ]}, {"source": str(self.picture), "frames": 13}, {"source": str(video), "frames": 18, "volume": 0}]
        result = compile_video_timeline(
            self.settings, "OpenFilm", shots, fps=24, width=320, height=568, openfilm_project=True,
            narration=[{"source": str(self.voice), "start_frame": 0, "text": "One line"}],
            captions=[{"start_frame": 30, "end_frame": 48, "text": "<Caption>"}],
        )
        root = Path(result["openfilm_project_path"])
        self.assertEqual({path.suffix for path in (root / "assets").iterdir()}, {".mp4", ".png", ".wav"})
        self.assertEqual(len(list((root / "assets").iterdir())), 3)
        with wave.open(str(root / "assets" / "mix.wav"), "rb") as mix:
            self.assertEqual(mix.getnframes(), 96_000)
        film = (root / "film.html").read_text(encoding="utf-8")
        self.assertLess(film.index("<track"), film.index("<iframe"))
        self.assertLess(film.index("<iframe"), film.index("<video"))
        self.assertIn('at="0.708"', film)
        self.assertNotIn(str(video), film)
        self.assertIn("&lt;Text&gt; &amp; title", next((root / "scenes").iterdir()).read_text(encoding="utf-8"))
        self.assertIn("&lt;Caption&gt;", (root / "captions.vtt").read_text(encoding="utf-8"))
        self.assertEqual(result["total_frames"], 48)

    def test_openfilm_rejects_jianying_only_effects(self):
        if self.settings.ffmpeg_exe is None:
            self.skipTest("FFmpeg is unavailable")
        shots = [{"source": str(self.picture), "frames": 30, "transition": {"name": "叠化"}}]
        self.assertIsNone(compile_video_timeline(self.settings, "Native", shots)["openfilm_project_path"])
        with self.assertRaisesRegex(ProductionError, "transition"):
            compile_video_timeline(self.settings, "OpenFilm", shots, openfilm_project=True)


if __name__ == "__main__":
    unittest.main()
