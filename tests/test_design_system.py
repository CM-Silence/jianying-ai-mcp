from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from jianying_ai_mcp.caption_text import caption_entities, emphasis_ranges, split_caption, tidy_caption
from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.editing_modules.layouts import read_interview_layout
from jianying_ai_mcp.media_info import parse_media_info
from jianying_ai_mcp.media_prepare import editing_clip, needs_upright_copy
from jianying_ai_mcp.motion_library import RECIPES, _cache_key, render_motion, scene_html
from jianying_ai_mcp.production_graphics import ProductionError
from jianying_ai_mcp.sound_design import CUES, plan_sound, synthesize_sfx
from jianying_ai_mcp.storyboard import check_structure, compile_episode, select_take, text_cues


ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT / "test_assets" / "compat_test_video.mp4"
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class DesignSystemTests(unittest.TestCase):
    def setUp(self) -> None:
        build = ROOT / ".build"
        build.mkdir(exist_ok=True)
        scratch = self.enterContext(tempfile.TemporaryDirectory(prefix="design-test-", dir=build))
        self.root = Path(scratch)
        self.settings = Settings(self.root / "drafts", self.root / "work", None)

    def test_every_recipe_renders_a_scene_page(self) -> None:
        samples = {
            "hook": {"title": "美工\n外包还是自己养", "subtitle": "没有标准答案"},
            "chapter": {"index": "02", "title": "外包的坑"},
            "headline": {"eyebrow": "第二件事", "title": "先试稿"},
            "list": {"title": "三件事", "items": ["需求表", "先试稿"]},
            "compare": {"left": {"title": "外包", "items": ["快"]}, "right": {"title": "自养", "items": ["稳"]}},
            "quote": {"text": "没有标准答案"},
            "number": {"value": 3000, "unit": "+", "label": "服务卖家"},
            "steps": {"items": ["需求不清", "反复修改"]},
            "checklist": {"items": ["产品线稳定"]},
            "end": {"brand": "阿兰", "slogan": "做细做准做透", "cta": "评论区留外包"},
        }
        self.assertEqual(set(samples), set(RECIPES))
        for recipe, data in samples.items():
            with self.subTest(recipe=recipe):
                page = scene_html(recipe, data, duration=2.5)
                self.assertIn("window.film", page)
                self.assertIn("frame", page)
                self.assertIn(recipe if recipe != "quote" else "quote", page)

    def test_long_cjk_title_scales_down_to_fit(self) -> None:
        page = scene_html("hook", {"title": "外包还是自己养"}, duration=2.5)
        self.assertIn("calc(81vw / 7)", page)

    def test_number_card_scales_down_to_fit_long_values(self) -> None:
        short = scene_html("number", {"value": 3000}, duration=2.5)
        long = scene_html("number", {"value": 9_999_999}, duration=2.5)
        # "3,000" is 5 glyphs, "9,999,999" is 9; the divisor is 0.58em per digit.
        self.assertIn("calc(84vw / 2.90)", short)
        self.assertIn("calc(84vw / 5.22)", long)
        # A unit badge eats width, so the number gets a tighter budget.
        with_unit = scene_html("number", {"value": 3000, "unit": "+"}, duration=2.5)
        self.assertIn("calc(74vw / 2.90)", with_unit)

    def test_motion_cache_key_tracks_recipe_markup(self) -> None:
        base = _cache_key("number", "<html>one</html>", 30, 1080, 1920)
        self.assertEqual(base, _cache_key("number", "<html>one</html>", 30, 1080, 1920))
        self.assertNotEqual(base, _cache_key("number", "<html>two</html>", 30, 1080, 1920))
        self.assertNotEqual(base, _cache_key("number", "<html>one</html>", 24, 1080, 1920))

    def test_unknown_recipe_and_bad_number_are_rejected(self) -> None:
        with self.assertRaises(ProductionError):
            scene_html("nope", {})
        with self.assertRaises(ProductionError):
            scene_html("number", {"value": "3000"})

    def test_render_motion_can_stop_before_rendering(self) -> None:
        result = render_motion(self.settings, "hook", {"title": "测试"}, duration=1.5, fps=24, render=False)
        self.assertNotIn("mp4_path", result)
        self.assertTrue(Path(result["project_path"], "film.html").is_file())

    def test_sound_cues_are_stereo_wav_of_requested_length(self) -> None:
        for name in ("whoosh", "impact", "tick"):
            with self.subTest(name=name):
                asset = synthesize_sfx(self.settings, name, gain_db=-8)
                path = Path(asset["path"])
                self.assertTrue(path.is_file())
                with wave.open(str(path), "rb") as handle:
                    self.assertEqual(handle.getnchannels(), 2)
                    self.assertEqual(handle.getframerate(), 48_000)
                    self.assertAlmostEqual(handle.getnframes() / 48_000, CUES[name].seconds, places=2)

    def test_plan_sound_maps_events_and_respects_overrides(self) -> None:
        placements = plan_sound([
            {"kind": "chapter", "at": 8.0},
            {"kind": "list", "at": 12.5, "gain_db": -14},
            {"kind": "quote", "at": 20.0, "sfx": False},
            {"kind": "talk", "at": 0.0},
        ])
        self.assertEqual([item["name"] for item in placements], ["impact", "tick"])
        self.assertEqual(placements[1]["gain_db"], -14)

    def test_select_take_prefers_the_best_matching_cue(self) -> None:
        pool = [
            {"source": "a.mp4", "duration_ms": 9000, "cues": [
                {"start_ms": 1000, "end_ms": 3000, "text": "美工是要找外包还是自己养"},
                {"start_ms": 5000, "end_ms": 7000, "text": "我做了很多年跨境视觉"},
            ]},
            {"source": "b.mp4", "duration_ms": 9000, "cues": [
                {"start_ms": 200, "end_ms": 2400, "text": "美工是找外包还是自己养呢"},
            ]},
        ]
        match = select_take(self.settings, "美工是要找外包还是自己养", pool=pool)
        self.assertIn(match["source"], {"a.mp4", "b.mp4"})
        self.assertGreater(match["score"], 0.8)
        self.assertLess(match["start"], match["end"])

    @unittest.skipUnless(VIDEO.is_file(), "compat test video missing")
    def test_compile_episode_builds_timeline_with_sound_and_beats(self) -> None:
        fake = {"mp4_path": str(VIDEO), "project_path": str(self.root)}
        spec = {
            "name": "design-test",
            "fps": 24,
            "width": 540,
            "height": 960,
            "style_preset": "editorial",
            "beats": [
                {"kind": "graphic", "recipe": "hook", "seconds": 2.0, "data": {"title": "开场"}},
                {"kind": "talk", "source": str(VIDEO), "start": 0.0, "end": 3.0,
                 "text": "美工是要找外包还是自己养。没有标准答案。"},
                {"kind": "graphic", "recipe": "end", "seconds": 2.0,
                 "data": {"brand": "阿兰", "slogan": "做细做准做透"}, "sfx": "swell"},
            ],
        }
        with patch("jianying_ai_mcp.storyboard.render_motion", return_value=fake):
            result = compile_episode(self.settings, spec)
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertTrue(Path(result["srt_path"]).is_file())
        self.assertEqual(len(result["episode"]["beats"]), 3)
        self.assertAlmostEqual(result["episode"]["duration_s"], 7.0, places=2)
        self.assertEqual(result["episode"]["cue_count"], 2)
        self.assertTrue(manifest)
        audio = [track for track in manifest.get("tracks", []) if track.get("type") == "audio"]
        self.assertTrue(audio)

    @unittest.skipUnless(VIDEO.is_file(), "compat test video missing")
    def test_compile_episode_rejects_out_of_order_beat(self) -> None:
        spec = {"name": "bad", "beats": [{"kind": "talk", "source": str(VIDEO), "start": 5.0, "end": 2.0}]}
        with self.assertRaises(ProductionError):
            compile_episode(self.settings, spec)


    def _tooled(self) -> Settings:
        return Settings(self.root / "drafts", self.root / "work", None,
                        ffmpeg_exe=Path(FFMPEG), ffprobe_exe=Path(FFPROBE))

    # ------------------------------------------------------------ captions

    def test_captions_break_at_clauses_and_never_open_with_punctuation(self) -> None:
        text = ("说实话，这个问题没有标准答案。但有一个很现实的判断：你现在处在什么阶段，就选什么样的方式。"
                "我是阿兰，把跨境视觉这件事，做细、做准、做透。")
        cues = split_caption(text, max_chars=14)
        self.assertTrue(all(cue[0] not in "，。、；：！？" for cue in cues))
        self.assertIn("做细、做准、做透。", cues)  # a short enumeration stays on one line
        self.assertIn("但有一个很现实的判断：", cues)  # a colon closes the cue

    def test_long_clause_breaks_at_a_function_word_not_mid_word(self) -> None:
        cues = split_caption("这时候养一个懂品牌也懂运营的设计负责人会更划算。", max_chars=14)
        self.assertGreater(len(cues), 1)
        self.assertNotIn("负", [cue[-1] for cue in cues])
        self.assertTrue(all(len(cue) <= 15 for cue in cues))

    def test_spaced_punctuation_drops_pauses_and_keeps_tone(self) -> None:
        self.assertEqual(tidy_caption("，风格是不是你们想要的。"), "风格是不是你们想要的")
        self.assertEqual(tidy_caption("先沟通，看顺不顺；"), "先沟通 看顺不顺")
        self.assertEqual(tidy_caption("那什么时候该养人呢？"), "那什么时候该养人呢？")
        self.assertEqual(tidy_caption("评论区留「外包」。"), "评论区留「外包」")
        self.assertEqual(tidy_caption("先沟通，看顺不顺；", "keep"), "先沟通，看顺不顺；")

    def test_text_cues_cover_the_beat_without_gaps(self) -> None:
        cues = text_cues("如果你找外包，尽量做到三件事。第一，先做一个详细的需求表。",
                         offset=2.0, seconds=6.0, fps=30, highlights=["需求表"], duck=True)
        self.assertEqual(cues[0]["start_frame"], 60)
        self.assertEqual(cues[-1]["end_frame"], 240)
        for left, right in zip(cues, cues[1:]):
            self.assertEqual(left["end_frame"], right["start_frame"])
        self.assertEqual([cue["highlights"] for cue in cues if cue.get("highlights")], [["需求表"]])
        self.assertTrue(all(cue["duck_bgm"] for cue in cues))

    # ------------------------------------------------------------ cards

    def test_chapter_card_is_title_only(self) -> None:
        with self.assertRaises(ProductionError):
            scene_html("chapter", {"index": "01", "title": "先说外包", "note": "快、灵活、不养闲人"})

    def test_list_items_can_follow_the_speech(self) -> None:
        page = scene_html("list", {"items": [{"title": "竞品链接", "at": 1.5}, {"title": "目标人群", "at": 4.2}]}, duration=8)
        self.assertIn("const starts=[1.5, 4.2]", page)
        with self.assertRaises(ProductionError):
            scene_html("list", {"items": [{"title": "a", "at": 4}, {"title": "b", "at": 2}]}, duration=8)

    def test_recap_cards_are_full_screen_only_in_the_summary(self) -> None:
        talk = {"kind": "talk", "source": "x.mp4", "start": 0, "end": 3}
        recap = {"kind": "graphic", "recipe": "list", "data": {"items": ["a"]}}
        with self.assertRaises(ProductionError):
            check_structure([talk, recap])
        summary = {"kind": "graphic", "recipe": "chapter", "role": "summary", "data": {"title": "总结"}}
        check_structure([talk, summary, recap])
        check_structure([{**talk, "layout": {"kind": "stacked", "with": {"graphic": recap}}}])

    # ------------------------------------------------------------ layouts

    def test_split_layout_can_put_the_speaker_on_top(self) -> None:
        layout = read_interview_layout(
            {"kind": "stacked", "main": "top", "top": {"source": "card.mp4", "fit": "contain"}}, 540, 960, "t",
        )
        self.assertEqual(layout.main_rect[1], 0)
        self.assertGreater(layout.secondary_rect[1], 0)
        self.assertEqual(layout.top_fit, "contain")
        with self.assertRaises(ProductionError):
            read_interview_layout({"kind": "stacked", "main": "left", "top": {"source": "x"}}, 540, 960, "t")

    @unittest.skipUnless(VIDEO.is_file() and FFMPEG and FFPROBE, "ffmpeg and compat video required")
    def test_split_beat_renders_its_card_at_panel_size(self) -> None:
        calls = []

        def fake_render(settings, recipe, data, **kwargs):
            calls.append((recipe, kwargs["width"], kwargs["height"]))
            return {"mp4_path": str(VIDEO), "project_path": str(self.root)}

        spec = {
            "name": "split-test", "fps": 24, "width": 540, "height": 960, "style_preset": "editorial",
            "beats": [
                {"kind": "talk", "source": str(VIDEO), "start": 0.0, "end": 2.0, "text": "先做一个详细的需求表。",
                 "layout": {"kind": "stacked", "with": {"graphic": {"recipe": "headline", "data": {"title": "需求表"}}}}},
                {"kind": "talk", "source": str(VIDEO), "start": 2.0, "end": 4.0, "text": "再看数据。",
                 "layout": {"kind": "stacked", "main": "top", "split": 0.55,
                            "with": {"media": {"source": str(VIDEO), "start": 4.0}}}},
            ],
        }
        with patch("jianying_ai_mcp.storyboard.render_motion", side_effect=fake_render):
            result = compile_episode(self._tooled(), spec, include_manifest=True)
        self.assertEqual(calls, [("headline", 540, 480)])
        beats = result["episode"]["beats"]
        self.assertEqual(beats[0]["with"], "graphic:headline")
        self.assertEqual(beats[1]["main"], "top")
        tracks = {track["name"]: track["segments"] for track in result["manifest"]["tracks"]}
        self.assertEqual(tracks["主画面"][1]["panel_rect"][1], 0)  # speaker panel sits at the top
        self.assertGreater(tracks["分屏上方画面"][1]["panel_rect"][1], 0)

    @unittest.skipUnless(VIDEO.is_file() and FFMPEG and FFPROBE, "ffmpeg and compat video required")
    def test_music_block_becomes_a_music_bed(self) -> None:
        spec = {
            "name": "music-test", "fps": 24, "width": 540, "height": 960,
            "music": {"preset": "warm", "volume": 0.16, "duck_volume": 0.06},
            "beats": [{"kind": "talk", "source": str(VIDEO), "start": 0.0, "end": 3.0, "text": "先说外包。"}],
        }
        result = compile_episode(self._tooled(), spec)
        self.assertTrue(result["bgm_path"] and Path(result["bgm_path"]).is_file())

    @unittest.skipUnless(VIDEO.is_file(), "compat test video missing")
    def test_traditional_caption_font_is_flagged(self) -> None:
        spec = {
            "name": "font-test", "fps": 24, "width": 540, "height": 960,
            "caption_decoration": {"font": "台北黑体_Bold"},
            "beats": [{"kind": "talk", "source": str(VIDEO), "start": 0.0, "end": 2.0, "text": "先说外包，再说自养。"}],
        }
        result = compile_episode(self.settings, spec)
        self.assertTrue(any("标点" in warning for warning in result.get("warnings", [])))

    @unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg required")
    def test_rotated_footage_gets_an_upright_copy_of_just_the_range(self) -> None:
        settings = self._tooled()
        flat, rotated = self.root / "flat.mp4", self.root / "rotated.mp4"
        subprocess.run([
            FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24:duration=4",
            "-f", "lavfi", "-i", "sine=duration=4", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(flat),
        ], check=True)
        subprocess.run([
            FFMPEG, "-v", "error", "-y", "-display_rotation", "90", "-i", str(flat), "-c", "copy", str(rotated),
        ], check=True)
        info = parse_media_info(settings, str(rotated))["media_info"]
        if not needs_upright_copy(info):
            self.skipTest("this ffmpeg build did not write a display rotation")
        clip = editing_clip(settings, rotated, 1.0, 2.5, width=270, height=480, fps=24, handle=0.5)
        upright = parse_media_info(settings, clip["path"])["media_info"]
        self.assertEqual((upright["width"], upright["height"]), (270, 480))
        self.assertFalse(needs_upright_copy(upright))
        self.assertAlmostEqual(clip["offset"], 0.5, places=3)
        self.assertLess(upright["duration"], 2.7)
        self.assertTrue(upright["has_audio"])


    # ------------------------------------------------- caption presentation

    def test_caption_entities_build_stack_and_block_modes(self) -> None:
        cues = [(0.0, 2.0, "如果你找外包"), (2.0, 4.0, "尽量做到三件事"), (4.0, 6.0, "第一 先做需求表")]
        self.assertTrue(all(item["anim"] == "single" for item in caption_entities(cues, mode="stack")))
        for mode in ("stack", "blocks"):
            entities = caption_entities(cues, mode=mode, groups=[0, 0, None])
            self.assertEqual([item.get("slot") for item in entities], [0, 1, None])
            self.assertEqual([item["end"] for item in entities], [4, 4, 6])
            self.assertEqual(entities[0]["siblings"], [cues[0][2], cues[1][2]])
        words = caption_entities([(0.0, 3.0, "先做需求表 再做试稿")], mode="words")
        self.assertEqual([entity["text"] for entity in words], ["先做需求表", "先做需求表 再做试稿"])
        with self.assertRaises(ProductionError):
            caption_entities(cues, mode="nope")

    def test_emphasis_ranges_marks_only_present_terms(self) -> None:
        self.assertEqual(emphasis_ranges("先做需求表", ["需求表", "试稿"]), [(2, 5)])
        self.assertEqual(emphasis_ranges("先做需求表", ["试稿"]), [])

    @unittest.skipUnless(VIDEO.is_file(), "compat test video missing")
    def test_episode_captions_are_drawn_as_items_with_keywords_and_motion(self) -> None:
        spec = {
            "name": "caption-item-test", "fps": 24, "width": 540, "height": 960, "style_preset": "editorial",
            "caption_mode": "stack", "caption_keywords": ["需求表"],
            "caption_zone": "bottom",
            "beats": [{"kind": "talk", "source": str(VIDEO), "start": 0.0, "end": 3.0,
                       "text": "如果你找外包，先做一个详细的需求表。"}],
        }
        result = compile_episode(self.settings, spec, include_manifest=True)
        manifest = result["manifest"]
        self.assertFalse(manifest.get("srt_imports"))
        self.assertTrue(Path(result["srt_path"]).is_file())  # still delivered as a file
        self.assertEqual(result["episode"]["caption_items"], len([
            segment for track in manifest["tracks"] if track["name"].startswith("字幕")
            for segment in track["segments"]
        ]))
        items = [segment for track in manifest["tracks"] if track["name"].startswith("字幕")
                 for segment in track["segments"]]
        self.assertTrue(any(item.get("highlight_ranges") for item in items))
        self.assertEqual(len(items), 2)
        self.assertTrue(all(item["portable_animation"]["kind"] == "fade" for item in items))
        self.assertTrue(all(not item.get("keyframes") for item in items))
        ordered = sorted(items, key=lambda item: item["start"])
        self.assertGreater(ordered[0]["clip"]["transform_y"], ordered[1]["clip"]["transform_y"])
        self.assertEqual(len({item["start"] + item["duration"] for item in items}), 1)
        self.assertEqual({beat["caption_zone"] for beat in result["episode"]["beats"]}, {"bottom"})

    @unittest.skipUnless(VIDEO.is_file() and FFMPEG, "ffmpeg and compat video required")
    def test_summary_card_can_carry_the_speaker_voice(self) -> None:
        spec = {
            "name": "voice-card-test", "fps": 24, "width": 540, "height": 960, "style_preset": "editorial",
            "beats": [
                {"kind": "talk", "source": str(VIDEO), "start": 0.0, "end": 2.0, "text": "先说结论。"},
                {"kind": "graphic", "recipe": "list", "role": "summary",
                 "voice": {"source": str(VIDEO), "start": 2.0, "end": 5.0, "text": "内部定标准，外包做产能。"},
                 "data": {"items": [{"title": "内部定标准", "at": 0.2}, {"title": "外包做产能", "at": 1.6}]}},
            ],
        }
        fake = {"mp4_path": str(VIDEO), "project_path": str(self.root)}
        with patch("jianying_ai_mcp.storyboard.render_motion", return_value=fake):
            result = compile_episode(self._tooled(), spec, include_manifest=True)
        self.assertEqual(result["episode"]["voice_cards"], 1)
        narration = [track for track in result["manifest"]["tracks"] if track["name"].startswith("旁白")]
        self.assertEqual(len(narration), 1)
        self.assertTrue(Path(narration[0]["segments"][0]["source"]).is_file())
        self.assertAlmostEqual(result["episode"]["beats"][1]["end_s"] - result["episode"]["beats"][1]["start_s"], 3.0, places=2)
        captions = [track for track in result["manifest"]["tracks"] if track["name"].startswith("字幕")]
        self.assertTrue(any("定标准" in segment["text"] for track in captions for segment in track["segments"]))

    @unittest.skipUnless(VIDEO.is_file(), "compat test video missing")
    def test_transitions_connect_picture_changes_not_every_cut(self) -> None:
        spec = {
            "name": "transition-test", "fps": 24, "width": 540, "height": 960, "style_preset": "editorial",
            "beats": [
                {"kind": "graphic", "recipe": "hook", "seconds": 1.0, "data": {"title": "开场"}},
                {"kind": "talk", "source": str(VIDEO), "start": 0.0, "end": 2.0, "text": "第一句。"},
                {"kind": "talk", "source": str(VIDEO), "start": 2.0, "end": 4.0, "text": "第二句。"},
            ],
        }
        with patch("jianying_ai_mcp.storyboard.render_motion", return_value={"mp4_path": str(VIDEO), "project_path": str(self.root)}):
            result = compile_episode(self.settings, spec, include_manifest=True)
        main = [track for track in result["manifest"]["tracks"] if track["name"] == "主画面"][0]["segments"]
        self.assertIn("transition", main[0])       # card to footage
        self.assertNotIn("transition", main[1])    # same-camera cut stays hard
        self.assertEqual(result["design"]["native_transitions"], 1)


    def test_packaging_template_follows_the_documented_rules(self) -> None:
        spec = json.loads((ROOT / "examples" / "episodes" / "packaged-talk-template.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["caption_size"], 11.5)
        self.assertEqual(spec["transitions"], "auto")
        check_structure(spec["beats"])                       # no recap card outside the summary
        self.assertTrue(any(beat.get("role") == "summary" for beat in spec["beats"]))
        self.assertTrue(any(beat.get("chapter") for beat in spec["beats"]))
        self.assertEqual({beat["caption"]["mode"] for beat in spec["beats"] if beat.get("caption")} - {None},
                         {"stack", "blocks", "single"})
        for beat in spec["beats"]:
            if beat["kind"] == "graphic":
                self.assertIn(beat["recipe"], RECIPES)
            graphic = ((beat.get("layout") or {}).get("with") or {}).get("graphic")
            if graphic:
                self.assertIn(graphic["recipe"], RECIPES)
                panel = RECIPES[graphic["recipe"]]
                self.assertTrue(set(graphic["data"]) <= set(panel.data_fields))
                for item in graphic["data"].get("items", []):
                    if isinstance(item, dict) and "at" in item:
                        self.assertLess(item["at"], beat["end"] - beat["start"])
        voice = next(beat for beat in spec["beats"] if beat.get("voice"))
        self.assertGreater(voice["voice"]["end"], voice["voice"]["start"])


    @unittest.skipUnless(VIDEO.is_file(), "compat test video missing")
    def test_small_caption_size_gets_a_warning(self) -> None:
        spec = {
            "name": "small-caption", "fps": 24, "width": 540, "height": 960, "style_preset": "editorial",
            "caption_size": 8,
            "beats": [{"kind": "talk", "source": str(VIDEO), "start": 0.0, "end": 2.0, "text": "先做需求表。"}],
        }
        result = compile_episode(self.settings, spec)
        self.assertTrue(any("11.5" in warning for warning in result.get("warnings", [])))


    def test_only_semantic_breaks_and_timed_steps_can_share_slots(self) -> None:
        from jianying_ai_mcp.storyboard import text_cues, transcript_cues
        from jianying_ai_mcp.caption_placement import fixed_caption_slots

        for line in ("预算不超过3.5万元", "如果，要在明天下午三点之前提交文档", "确认明天下午三点之前交付全部源文件"):
            rows = text_cues(line, offset=0, seconds=3, fps=30, mode="stack")
            self.assertTrue(all("caption_group" not in row for row in rows))
        rows = text_cues("先核算3.5万元成本，再安排设计排期。", offset=0, seconds=3, fps=30, mode="stack")
        self.assertEqual([r["text"] for r in rows], ["先核算3.5万元成本", "再安排设计排期"])
        self.assertEqual({r["caption_group"] for r in rows}, {0})
        items = text_cues("样稿、工期、报价", offset=0, seconds=3, fps=30, mode="blocks")
        self.assertEqual([r["text"] for r in items], ["样稿", "工期", "报价"])
        raw = [{"text": t, "start_ms": a, "end_ms": b} for a, b, t in [
            (0, 900, "首先核对原稿"), (1000, 1800, "其次确认交付"), (1900, 2600, "最后签收"),
            (2700, 3400, "今天是晴天"), (3500, 4100, "换个话题")]]
        timed = transcript_cues(raw, source_start=0, source_end=4.1, offset=0, fps=30, mode="stack")
        self.assertEqual([r.get("caption_group") for r in timed], [0, 0, 0, None, None])
        self.assertEqual([r["start_frame"] for r in timed], [0, 30, 57, 81, 105])
        slots = fixed_caption_slots(("样稿", "工期"), "blocks", 720, 1280, 11.5, -0.7, False)
        self.assertEqual(slots[0][1], slots[1][1])
        em = 11.5 * 720 / 144
        gap = (slots[1][0] - slots[0][0]) * 720 / 2 - 2 * em
        self.assertTrue(0.45 * em < gap < 0.7 * em, gap)
        vertical = fixed_caption_slots(("先确定本周交付的范围", "再确定下一次的设计排期"),
                                       "blocks", 720, 1280, 11.5, -0.7, False)
        self.assertGreater(vertical[0][1], vertical[1][1])


if __name__ == "__main__":
    shutil.which("ffmpeg") or unittest.skip("ffmpeg required")
    unittest.main()
