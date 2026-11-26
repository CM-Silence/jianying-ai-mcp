from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path

from PIL import Image

from jianying_ai_mcp.production_graphics import ProductionError
from jianying_ai_mcp.production_openfilm import write_openfilm_project


class PortableOpenFilmTests(unittest.TestCase):
    def setUp(self) -> None:
        build = Path(__file__).resolve().parents[1] / ".build"
        build.mkdir(exist_ok=True)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=build)))
        self.image = self.root / "background.png"
        Image.new("RGB", (160, 284), "#13212c").save(self.image)
        self.audio = self.root / "sound.wav"
        with wave.open(str(self.audio), "wb") as wav:
            wav.setparams((1, 2, 48_000, 0, "NONE", "not compressed"))
            wav.writeframes(bytes(96_000))
        self.manifest = {
            "draft": {"width": 160, "height": 284},
            "tracks": [
                {"type": "video", "name": "Picture", "segments": [{
                    "source": str(self.image), "start": 0, "duration": 1_000_000,
                }]},
                {"type": "text", "name": "Title", "segments": [{
                    "text": "Opening & first point", "start": 0, "duration": 1_000_000,
                }]},
            ],
            "srt_imports": [{"style": {"align": 1}, "clip": {"transform_y": -.75}}],
        }

    def write(self, **kwargs) -> Path:
        return write_openfilm_project(
            self.root, self.manifest, {str(self.image): True}, [(0, 1_000_000, "Watch <closely>")],
            self.audio, 1_000_000, **kwargs,
        )

    def test_scene_and_caption_effects_keep_a_real_caption_track(self) -> None:
        title = self.manifest["tracks"][1]["segments"][0]
        title["portable_animation"] = {"kind": "rise", "duration_us": 180_000}
        root = self.write(
            portable_transitions=[{"start_us": 333_333, "duration_us": 333_333, "kind": "wipe_left", "color": "#20374a"}],
            caption_decoration={"border": {"width": 40}, "background": {"color": "#301f10", "alpha": .6}, "portable_animation": "fade"},
        )
        film = (root / "film.html").read_text(encoding="utf-8")
        self.assertLess(film.index('<track kind="captions"'), film.index('id="track-2-clip-1"'))
        self.assertLess(film.index('id="track-2-clip-1"'), film.index('id="portable-transition-1"'))
        self.assertLess(film.index('id="portable-transition-1"'), film.index('id="track-1-clip-1"'))
        self.assertIn('at="0.333" src="scenes/portable-transition-1.html#t=0,0.333"', film)
        transition = (root / "scenes" / "portable-transition-1.html").read_text(encoding="utf-8")
        self.assertIn("background:#20374a", transition)
        self.assertIn("(t - 0.0003330) / 0.3333330", transition)
        title_html = (root / "scenes" / "track-2-clip-1.html").read_text(encoding="utf-8")
        self.assertIn("animation:jianying-portable-enter 0.180000s", title_html)
        self.assertIn("translate:0 .72em", title_html)
        self.assertIn("Opening &amp; first point", title_html)
        captions = (root / "captions.vtt").read_text(encoding="utf-8")
        self.assertIn("Watch &lt;closely&gt;", captions)
        self.assertIn("-webkit-text-stroke:0.08000em", captions)
        self.assertIn("background:rgba(48,31,16,0.60000)", captions)
        self.assertIn("@keyframes jianying-portable-enter", captions)

    def test_overlapping_transition_intervals_are_rejected(self) -> None:
        with self.assertRaisesRegex(ProductionError, "不能重叠"):
            self.write(portable_transitions=[
                {"start_us": 0, "duration_us": 500_000, "kind": "dip"},
                {"start_us": 250_000, "duration_us": 500_000, "kind": "wipe_right"},
            ])

    def test_jianying_only_effects_are_still_rejected_with_portable_options(self) -> None:
        self.manifest["tracks"][0]["segments"][0]["transition"] = {"name": "叠化"}
        with self.assertRaisesRegex(ProductionError, "transition"):
            self.write(portable_transitions=[{"start_us": 0, "duration_us": 500_000, "kind": "dip"}])

    def test_portable_caption_keeps_escaped_highlighted_text_and_transparent_base(self) -> None:
        root = self.write(
            caption_decoration={"background": False, "shadow": {"alpha": .8, "angle": -90}, "highlight_color": [1, .8, .25]},
            cue_highlights={(0, 1_000_000, "Watch <closely>"): [(6, 10), (9, 15)]},
        )
        captions = (root / "captions.vtt").read_text(encoding="utf-8")
        self.assertIn("Watch <c.jianying-highlight>&lt;closely&gt;</c>", captions)
        self.assertIn("::cue(.jianying-highlight) { color:rgba(255,204,64,1.00000); }", captions)
        self.assertIn("background:transparent;padding:0", captions)
        self.assertIn("text-shadow:0.00000em 0.09000em 0.15000em rgba(0,0,0,0.80000)", captions)
        self.assertNotIn("background:rgba(0,0,0,0.56)", captions)

    def test_invalid_highlight_character_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(ProductionError, "高亮的字符范围"):
            self.write(cue_highlights={(0, 1_000_000, "Watch <closely>"): [(4, 800)]})

    def test_portable_exit_combines_with_entrance_at_end_of_local_clip(self) -> None:
        title = self.manifest["tracks"][1]["segments"][0]
        title["portable_animation"] = {"kind": "rise", "duration_us": 160_000}
        title["portable_exit_animation"] = {"kind": "wipe_out", "duration_us": 180_000}
        root = self.write()
        title_html = (root / "scenes" / "track-2-clip-1.html").read_text(encoding="utf-8")
        self.assertIn("animation:jianying-portable-enter 0.160000s", title_html)
        self.assertIn("both,jianying-portable-exit 0.180000s cubic-bezier(.35,0,.76,.4) 0.819000s forwards", title_html)
        self.assertIn("@keyframes jianying-portable-exit", title_html)
        self.assertIn("clip-path:inset(0 100% 0 0)", title_html)

    def test_cropped_panel_uses_stage_pixel_rect_and_preserves_alpha(self) -> None:
        segment = self.manifest["tracks"][0]["segments"][0]
        Image.new("RGB", (100, 212), "#13212c").save(self.image)
        segment["panel_rect"] = [60, 72, 100, 212]
        segment["clip"] = {
            "transform_x": .375, "transform_y": -.4, "scale_x": .625, "scale_y": .75,
            "rotation": 12, "alpha": .75,
        }
        film = (self.write() / "film.html").read_text(encoding="utf-8")
        self.assertIn(
            'style="left:60.00000px;top:72.00000px;width:100.00000px;height:212.00000px;rotate:0.00000deg;opacity:0.75000"',
            film,
        )

    def test_out_of_stage_panel_is_rejected(self) -> None:
        self.manifest["tracks"][0]["segments"][0]["panel_rect"] = [61, 72, 100, 212]
        with self.assertRaisesRegex(ProductionError, "panel_rect 必须位于画布内"):
            self.write()
