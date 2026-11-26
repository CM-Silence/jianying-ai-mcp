from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import pyJianYingDraft as draft
from PIL import Image, ImageColor

from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.draft_ops import build_manifest
from jianying_ai_mcp.production import compile_video_timeline
from jianying_ai_mcp.production_graphics import create_video_graphic
from jianying_ai_mcp.production_styles import list_video_style_presets
from jianying_ai_mcp.video_animation import create_video_animation


class ProductionStylesTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime = Path(__file__).resolve().parents[1] / ".build"
        runtime.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=runtime, prefix="production-styles-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        ffmpeg = shutil.which("ffmpeg")
        self.settings = Settings(root / "draft", root / "work", None, Path(ffmpeg) if ffmpeg else None)
        self.picture = root / "picture.png"
        Image.new("RGB", (320, 568), "#E5DFD6").save(self.picture)
        self.shots = [{"source": str(self.picture), "frames": 47, "chapter": "First topic"},
                      {"source": str(self.picture), "frames": 28, "chapter": "Second topic"}]
        self.captions = [{"start_frame": 5, "end_frame": 34, "text": "Chapter evidence"}]

    def test_presets_use_known_non_vip_resources_and_distinct_styles(self) -> None:
        profiles = list_video_style_presets()["presets"]
        self.assertEqual([item["id"] for item in profiles], ["editorial", "documentary", "broadcast", "clean", "seller_reference"])
        self.assertEqual(len({item["graphic_shape"] for item in profiles}), 4)
        self.assertEqual(len({item["animation_motion"] for item in profiles}), 4)
        self.assertEqual(len({item["transition"] for item in profiles}), 4)
        for item in profiles:
            name = item["id"]
            with self.subTest(preset=name):
                self.assertIs(draft.FontType[item["caption_font"]].value.is_vip, False)
                self.assertIs(draft.TransitionType[item["transition"]].value.is_vip, False)
                self.assertIs(draft.TextIntro[item["chapter_intro"]].value.is_vip, False)
                if item["caption_intro"]:
                    self.assertIs(draft.TextIntro[item["caption_intro"]].value.is_vip, False)
                graphic = create_video_graphic(self.settings, "title", "Style sample", style_preset=name, width=320, height=568)
                with Image.open(graphic["image_path"]) as picture:
                    self.assertEqual(picture.getpixel((0, 0)), ImageColor.getrgb(item["colors"]["background"]))
                self.assertEqual(graphic["graphic_shape"], item["graphic_shape"])
                animation = create_video_animation(self.settings, title="Motion sample", style_preset=name, width=320, height=568)
                html = Path(animation["scene_path"]).read_text(encoding="utf-8")
                self.assertIn(f'"motion": "{item["animation_motion"]}"', html)
                self.assertIn(item["colors"]["background"], html)
                self.assertIn(".copy::before", html)

    def test_profiles_build_editable_drafts_and_distinct_openfilm_captions(self) -> None:
        profile_ids = ["editorial", "documentary", "broadcast", "clean"]
        observed_backgrounds = set()
        for profile in profile_ids:
            with self.subTest(preset=profile):
                compiled = compile_video_timeline(
                    self.settings, "Style " + profile, self.shots, fps=25, width=320, height=568,
                    style_preset=profile, captions=self.captions, srt_mode="native", include_manifest=True,
                    openfilm_project=bool(self.settings.ffmpeg_exe),
                )
                manifest = compiled["manifest"]
                built = build_manifest(self.settings, manifest, dry_run=True)
                self.assertEqual(built["summary"]["duration_us"], 3_000_000)
                self.assertEqual(compiled["design"]["native_transitions"], 1)
                self.assertEqual(compiled["design"]["portable_transitions"], 1)
                decorated = manifest["srt_imports"][0]["decoration"]
                observed_backgrounds.add(decorated["background"]["color"])
                if compiled["openfilm_project_path"]:
                    project = Path(compiled["openfilm_project_path"])
                    vtt = (project / "captions.vtt").read_text(encoding="utf-8")
                    pages = "\n".join(path.read_text(encoding="utf-8") for path in (project / "scenes").glob("*.html"))
                    self.assertEqual(compiled["design"]["chapter_starts_s"], [0.0, 1.88])
                    self.assertFalse(any(t["type"] == "text" and t.get("segments") for t in manifest["tracks"]))
                    self.assertFalse(any(title in pages for title in ("First topic", "Second topic")))
                    if profile == "documentary":
                        self.assertIn("Noto Serif CJK SC", vtt)
                    elif profile == "broadcast":
                        self.assertIn("translate:-.80em 0", vtt)
                    elif profile == "clean":
                        self.assertIn("clip-path:inset(0 100% 0 0)", vtt)
        self.assertEqual(len(observed_backgrounds), len(profile_ids))

    def test_unstyled_defaults_and_explicit_overrides(self) -> None:
        baseline = compile_video_timeline(self.settings, "Baseline", self.shots, captions=self.captions, include_manifest=True)
        self.assertNotIn("decoration", baseline["manifest"]["srt_imports"][0])
        self.assertNotIn("transition", baseline["manifest"]["tracks"][0]["segments"][0])
        selected = compile_video_timeline(
            self.settings, "Override", self.shots, captions=self.captions, include_manifest=True, style_preset="broadcast",
            caption_style={"size": 7}, caption_clip={"transform_y": -0.5}, caption_decoration={"font": "Alice_Regular"},
        )["manifest"]["srt_imports"][0]
        self.assertEqual((selected["style"]["size"], selected["clip"]["transform_y"], selected["decoration"]["font"]), (7, -0.5, "Alice_Regular"))
        visual = create_video_animation(self.settings, title="Own choice", style_preset="clean", motion="none", colors={"accent": "#AACCEE"})
        self.assertEqual(visual["motion"], "none")
        self.assertEqual(visual["palette"]["accent"], "#AACCEE")


if __name__ == "__main__":
    unittest.main()
