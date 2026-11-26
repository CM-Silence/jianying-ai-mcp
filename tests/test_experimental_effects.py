from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.draft_ops import ManifestError, build_manifest


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "test_assets"


class ExperimentalEffectsTests(unittest.TestCase):
    def setUp(self) -> None:
        build_root = ROOT / ".build"
        build_root.mkdir(exist_ok=True)
        workspace = tempfile.TemporaryDirectory(dir=build_root, prefix="effects-test-")
        self.addCleanup(workspace.cleanup)
        root = Path(workspace.name)
        self.settings = Settings(root / "drafts", root / "work", None)

    def manifest(self) -> dict:
        return {
            "draft": {"name": "试验效果"},
            "tracks": [
                {
                    "type": "video",
                    "name": "主画面",
                    "segments": [{"source": str(ASSETS / "compat_test_video.mp4"), "duration": "2s"}],
                },
                {
                    "type": "audio",
                    "name": "配乐",
                    "segments": [{"source": str(ASSETS / "compat_test_music.wav"), "duration": "2s"}],
                },
            ],
        }

    def test_effects_require_explicit_opt_in(self) -> None:
        volume_keyframe = {"property": "volume", "time": "0s", "value": 0.5}
        cases = [
            (0, "mask", {"name": "圆形"}),
            (0, "background_filling", {"type": "blur"}),
            (0, "keyframes", [volume_keyframe]),
            (1, "keyframes", [volume_keyframe]),
        ]
        for track, field, value in cases:
            with self.subTest(track=track, field=field):
                manifest = self.manifest()
                manifest["tracks"][track]["segments"][0][field] = value
                with self.assertRaisesRegex(ManifestError, "allow_experimental_effects=true"):
                    build_manifest(self.settings, manifest, dry_run=True)
        self.assertEqual(list(self.settings.draft_root.iterdir()), [])

    def test_effects_are_serialized_and_referenced(self) -> None:
        manifest = self.manifest()
        video_segments = manifest["tracks"][0]["segments"]
        video_segments[0].update({
            "mask": {"name": "矩形", "size": 0.6, "rect_width": 0.7, "feather": 20, "round_corner": 10},
            "background_filling": {"type": "blur", "blur": 0.375},
            "keyframes": [
                {"property": "volume", "time": "2s", "value": 1.25},
                {"property": "volume", "time": 0, "value": 0},
                {"property": "alpha", "time": "0s", "value": 0.9},
            ],
        })
        video_segments.append({
            "source": str(ASSETS / "compat_test_image.png"),
            "start": "2s",
            "duration": "2s",
            "background_filling": {"type": "color", "color": "#0066CCFF"},
        })
        manifest["tracks"][1]["segments"][0]["keyframes"] = [
            {"property": "volume", "time": ".5s", "value": 0.2},
            {"property": "volume", "time": 1_000_000, "value": 0.75},
        ]

        result = build_manifest(self.settings, manifest, allow_experimental_effects=True)
        content = json.loads((Path(result["draft_path"]) / "draft_content.json").read_text(encoding="utf-8"))
        materials = content["materials"]
        video, audio = content["tracks"]
        first_video, second_video = video["segments"]

        legacy_mask, = materials["masks"]
        common_mask, = materials["common_mask"]
        self.assertEqual(legacy_mask["id"], common_mask["id"])
        self.assertIn(common_mask["id"], first_video["extra_material_refs"])
        self.assertEqual(common_mask["name"], "矩形")
        self.assertEqual(common_mask["config"]["height"], 0.6)
        self.assertEqual(common_mask["config"]["width"], 0.7)
        self.assertEqual(common_mask["config"]["feather"], 0.2)
        self.assertEqual(common_mask["config"]["roundCorner"], 0.1)

        canvases = {canvas["type"]: canvas for canvas in materials["canvases"]}
        self.assertEqual(canvases["canvas_blur"]["blur"], 0.375)
        self.assertIn(canvases["canvas_blur"]["id"], first_video["extra_material_refs"])
        self.assertEqual(canvases["canvas_color"]["color"], "#0066ccff")
        self.assertIn(canvases["canvas_color"]["id"], second_video["extra_material_refs"])

        video_properties = {group["property_type"] for group in first_video["common_keyframes"]}
        self.assertEqual(video_properties, {"KFTypeVolume", "KFTypeAlpha"})
        self.assert_volume_keyframes(first_video, [(0, 0), (2_000_000, 1.25)])
        self.assert_volume_keyframes(audio["segments"][0], [(500_000, 0.2), (1_000_000, 0.75)])

    def assert_volume_keyframes(self, segment: dict, expected: list[tuple[int, float]]) -> None:
        volume, = [group for group in segment["common_keyframes"] if group["property_type"] == "KFTypeVolume"]
        points = [(frame["time_offset"], frame["values"][0]) for frame in volume["keyframe_list"]]
        self.assertEqual(points, expected)
        self.assertTrue(all(type(offset) is int for offset, _ in points))

    def test_invalid_effects_are_rejected_after_opt_in(self) -> None:
        cases = [
            (0, "mask", {"name": "圆形", "rect_width": 0.5}, "只适用于矩形蒙版"),
            (0, "mask", {"name": "圆形", "feather": 101}, "mask.feather"),
            (0, "background_filling", {"type": "blur", "blur": -0.5}, "background_filling.blur"),
            (0, "background_filling", {"type": "color", "color": "#FF0000"}, "#RRGGBBAA"),
            (1, "keyframes", [{"property": "volume", "time": "1", "value": 0.5}], "带单位"),
            (1, "keyframes", [{"property": "volume", "time": "3s", "value": 0.5}], "片段起点和结束之间"),
            (1, "keyframes", [{"property": "volume", "time": 0, "value": float("nan")}], "有限数值"),
            (1, "keyframes", [{"property": "position_x", "time": 0, "value": 0.5}], "property=volume"),
            (1, "keyframes", [
                {"property": "volume", "time": 0, "value": 0.5},
                {"property": "volume", "time": "0s", "value": 1},
            ], "time 不能重复"),
        ]
        for track, field, value, error in cases:
            with self.subTest(track=track, field=field, value=value):
                manifest = self.manifest()
                manifest["tracks"][track]["segments"][0][field] = value
                with self.assertRaisesRegex(ManifestError, error):
                    build_manifest(self.settings, manifest, dry_run=True, allow_experimental_effects=True)

    def test_background_filling_is_only_supported_on_bottom_video_track(self) -> None:
        manifest = self.manifest()
        manifest["tracks"].append({
            "type": "video",
            "name": "上层画面",
            "segments": [{
                "source": str(ASSETS / "compat_test_image.png"),
                "duration": "2s",
                "background_filling": {"type": "blur"},
            }],
        })
        with self.assertRaisesRegex(ManifestError, "最底层视频轨道"):
            build_manifest(self.settings, manifest, dry_run=True, allow_experimental_effects=True)
