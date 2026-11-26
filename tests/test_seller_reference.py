import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.delivery import deliver_edit
from jianying_ai_mcp.draft_ops import build_manifest
from jianying_ai_mcp.edit_recipe import EditClip, plan_edit
from jianying_ai_mcp.production_graphics import ProductionError
from jianying_ai_mcp.production_styles import SELLER_HIGHLIGHT
from jianying_ai_mcp.storyboard import compile_episode


ROOT = Path(__file__).resolve().parents[1]
VIDEO = str(ROOT / "test_assets/compat_test_video.mp4")


class SellerReferenceTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".build"
        scratch.mkdir(exist_ok=True)
        work = Path(self.enterContext(tempfile.TemporaryDirectory(dir=scratch)))
        self.settings = replace(Settings.from_env(), work_root=work, draft_root=work / "drafts")

    def compile(self, beats, **kwargs):
        return compile_episode(self.settings, {
            "name": "参考口播", "style_preset": "seller_reference", "width": 540, "height": 960,
            "fps": 30, "beats": beats,
        }, include_manifest=True, **kwargs)

    def test_timed_hook_and_quiet_dialogue_in_draft_and_preview(self):
        beats = [
            {"kind": "talk", "source": VIDEO, "start": 0, "end": 3.7,
             "highlights": ["构图", "色彩"],
             "caption": {"mode": "stack", "cues": [
                 {"at": 0, "text": "我们都懂工具"}, {"at": 1.6, "text": "懂构图"}, {"at": 2.53, "text": "懂色彩搭配"},
             ]}},
            {"kind": "talk", "source": VIDEO, "start": 3.7, "end": 5.0,
             "text": "下一句话", "chapter": "转入观点"},
        ]
        result = self.compile(beats, openfilm_project=bool(self.settings.ffmpeg_exe))
        self.assertEqual(result["episode"]["caption_sfx_frames"], [48, 76])
        self.assertEqual(result["design"]["native_transitions"], 0)
        tracks = result["manifest"]["tracks"]
        captions = sorted([s for t in tracks if t["type"] == "text" for s in t["segments"]], key=lambda s: s["start"])
        self.assertEqual(len(captions), 4)
        self.assertEqual([round(s["start"] * 30e-6) for s in captions], [0, 48, 76, 111])
        self.assertEqual([round((s["start"] + s["duration"]) * 30e-6) for s in captions[:3]], [111] * 3)
        self.assertEqual(len({s["clip"]["transform_y"] for s in captions[:3]}), 3)
        self.assertEqual(captions[-1]["clip"]["transform_y"], -0.435)
        self.assertNotIn("animations", captions[-1])
        self.assertEqual(captions[-1]["style"]["size"], 12)
        self.assertEqual(captions[-1]["font"], "烟波宋")
        self.assertEqual(captions[-1]["shadow"]["color"], [0, 0, 0])
        self.assertEqual(captions[1]["highlight_color"], list(SELLER_HIGHLIGHT))
        published = build_manifest(self.settings, result["manifest"], dry_run=False)
        content = json.loads((Path(published["draft_path"]) / "draft_content.json").read_text("utf-8"))
        style_runs = [run for mat in content["materials"]["texts"] for run in json.loads(mat["content"])["styles"]]
        self.assertIn(list(SELLER_HIGHLIGHT), [run["fill"]["content"]["solid"]["color"] for run in style_runs])
        if result["openfilm_project_path"]:
            scenes = Path(result["openfilm_project_path"]) / "scenes"
            html = "\n".join(page.read_text("utf-8") for page in scenes.glob("*.html"))
            self.assertIn("text-shadow:", html)
            self.assertIn("255,245,137", html)

    def test_item_accents_use_card_frame_clock_even_when_speech_has_no_captions(self):
        if not self.settings.ffmpeg_exe:
            self.skipTest("requires ffmpeg for split panel")
        beats = [
            {"kind": "talk", "source": VIDEO, "start": 0, "end": 4, "captions": False},
            {"kind": "talk", "source": VIDEO, "start": 4, "end": 8, "captions": False,
             "layout": {"kind": "stacked", "with": {"graphic": {"recipe": "steps", "data": {"items": [
                 {"title": "第一问", "at": 0}, {"title": "第二问", "at": 1.33}, {"title": "第三问", "at": 2.3},
             ]}}}}},
        ]
        with patch("jianying_ai_mcp.storyboard.render_motion", return_value={"mp4_path": VIDEO}):
            result = self.compile(beats)
        self.assertEqual(result["episode"]["item_sfx_frames"], [120, 160, 189])
        audio = [s for t in result["manifest"]["tracks"] if t["type"] == "audio" for s in t["segments"]]
        self.assertEqual([round(s["start"] * 30e-6) for s in audio], [120, 160, 189])

    def test_seller_pacing_checks_visible_shots_and_short_returns(self):
        # Source contains continuous dialogue split into short captions.
        clips = [EditClip(source=VIDEO, start=a, end=a + 2, caption="原话") for a in (0, 2, 4, 6)]
        side = str(ROOT / "test_assets/compat_test_video_alt.mp4")
        plan_edit(self.settings, "连续口播", clips, style_preset="seller_reference",
                  broll=[{"source": side, "at": 4, "duration": 4}])
        with self.assertRaisesRegex(ProductionError, "4.00–5.00"):
            plan_edit(self.settings, "短暂回切", clips, style_preset="seller_reference",
                      broll=[{"source": side, "at": 0, "duration": 4},
                             {"source": side, "at": 5, "duration": 3, "layout": {"kind": "stacked"}}])
        with self.assertRaisesRegex(ProductionError, "分镜停留"):
            self.compile([{"kind": "talk", "source": source, "start": 0, "end": 2, "captions": False}
                          for source in (VIDEO, side)])

    def test_authored_cues_cannot_collapse_to_one_frame_or_start_after_cut(self):
        for second in [0.005, 2.0]:
            with self.subTest(second=second), self.assertRaisesRegex(ProductionError, "caption.cues"):
                self.compile([{"kind": "talk", "source": VIDEO, "start": 0, "end": 2,
                               "caption": {"cues": [{"at": 0, "text": "先来"}, {"at": second, "text": "再来"}]}}])

    def test_circle_cutout_only_overlays_marked_speech_interval(self):
        result = self.compile([
            {"kind": "talk", "source": VIDEO, "start": 0, "end": 1, "captions": False},
            {"kind": "talk", "source": VIDEO, "start": 1, "end": 4, "captions": False,
             "circle_mask": {"at": 0.5, "duration": 1.3, "center": [0.5, 0.35], "diameter": 0.72}},
        ], openfilm_project=bool(self.settings.ffmpeg_exe))
        tracks = result["manifest"]["tracks"]
        main = next(t["segments"] for t in tracks if t["name"] == "主画面")
        self.assertEqual([s["volume"] for s in main], [1, 1])
        matte, = [s for t in tracks if t["type"] == "video" and t["name"] != "主画面" for s in t["segments"]]
        self.assertEqual((matte["start"], matte["duration"], matte["volume"]), (1_500_000, 1_300_000, 0))
        with Image.open(matte["source"]) as image:
            self.assertEqual(image.getpixel((270, 336))[3], 0)
            self.assertEqual(image.getpixel((0, 0)), (0, 0, 0, 255))
        self.assertTrue(build_manifest(self.settings, result["manifest"], dry_run=True)["success"])

    def test_plan_and_deliver_retain_reference_style_for_plain_dialogue(self):
        if not self.settings.ffprobe_exe:
            self.skipTest("requires ffprobe for recipe source information")
        plan = plan_edit(self.settings, "口播配方", [
            EditClip(source=VIDEO, start=0, end=2, caption="这是重要的反馈", highlights=["反馈"]),
            EditClip(source=VIDEO, start=2, end=3, caption="谢谢", chapter="结束"),
        ], style_preset="seller_reference")
        with patch("jianying_ai_mcp.delivery.export_or_handoff", return_value={"success": False}):
            result = deliver_edit(self.settings, plan["recipe_path"])
        self.assertTrue(result["draft_ready"])
        manifest = json.loads(Path(result["manifest_path"]).read_text("utf-8"))
        captions = [s for t in manifest["tracks"] if t["type"] == "text" for s in t["segments"]]
        self.assertEqual(captions[0]["highlight_color"], list(SELLER_HIGHLIGHT))
        self.assertEqual(captions[1]["clip"]["transform_y"], -0.435)
        self.assertNotIn("animations", captions[1])
