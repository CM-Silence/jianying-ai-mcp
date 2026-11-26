from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from jianying_ai_mcp.broll_library import bind_stock_selection, index_broll_shots, search_broll_shots
from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.draft_ops import build_manifest
from jianying_ai_mcp.delivery import _recipe_captions
from jianying_ai_mcp.edit_recipe import EditClip, plan_edit, read_edit_inputs
from jianying_ai_mcp.episode_broll import plan_broll
from jianying_ai_mcp.broll_placement import place_broll_shots
from jianying_ai_mcp.broll_library import get_broll_shot
from jianying_ai_mcp.media_prepare import run_ffmpeg
from jianying_ai_mcp.production_graphics import ProductionError
from jianying_ai_mcp.storyboard import compile_episode, read_episode_file


ROOT = Path(__file__).resolve().parents[1]


class BrollPlacementTests(unittest.TestCase):
    def setUp(self):
        build = ROOT / ".build"
        build.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=build)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = replace(Settings.from_env(), draft_root=self.root / "drafts", work_root=self.root / "work")
        if not self.settings.ffmpeg_exe or not self.settings.ffprobe_exe:
            self.skipTest("需要 FFmpeg 与 FFprobe")
        self.camera = str(ROOT / "test_assets/compat_test_video.mp4")
        self.footage = self.root / "warehouse.mp4"
        run_ffmpeg(self.settings, ["-stream_loop", "1", "-i", str(ROOT / "test_assets/compat_test_video_alt.mp4"),
                                   "-t", "12", "-c", "copy", str(self.footage)])
        self.shot_id = index_broll_shots(self.settings, [
            {"source": str(self.footage), "start_ms": 8000, "end_ms": 11000, "description": "扫码分拣"},
        ], source_pool="personal")["shot_ids"][0]

    def test_native_stock_selection_replays_both_layouts_and_keeps_catalog_identity(self):
        data = self.root / "User Data"
        settings = replace(self.settings, draft_root=data / "Projects" / "com.lveditor.draft")
        settings.draft_root.mkdir(parents=True)
        cache = data / "Cache" / "onlineMaterial" / "stock.mp4"
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b"editor-only test fixture")
        shot = {"source": str(cache), "start_ms": 0, "end_ms": 8000, "editor_verified_ms": 8000,
                "description": "封箱复核"}
        key, = index_broll_shots(settings, [shot])["shot_ids"]
        unbound = get_broll_shot(settings, key)
        self.assertIsNone(unbound["media"])
        self.assertFalse(unbound["timeline_ready"])
        pending = place_broll_shots(settings, {}, [{"shot_id": key, "at": 0, "duration": 6}], dry_run=False)
        self.assertEqual(pending["status"], "not_timeline_ready")
        self.assertFalse(list(settings.draft_root.iterdir()))
        selection = self.root / "selection.json"
        material = {"id": "local-slot", "material_id": "catalog-42", "category_name": "official",
                    "type": "video", "duration": 8000000, "width": 1920, "height": 1080,
                    "path": str(cache), "copyright_limit_type": "editor_managed", "check_flag": 17}
        selection.write_text(json.dumps({"materials": {"videos": [material], "sound_channel_mappings": [{"id": "aux"}]},
            "tracks": [{"type": "video", "segments": [{"material_id": "local-slot", "extra_material_refs": ["aux"],
                                                        "stock_origin_flag": 7}]}]}), encoding="utf8")
        with self.assertRaises(ValueError):
            bind_stock_selection(settings, str(selection), [key, "missing"])
        self.assertEqual(search_broll_shots(settings, "封箱", availability="timeline_ready")["matches"], [])
        self.assertEqual(bind_stock_selection(settings, str(selection), [key])["asset_ids"], {key: "catalog-42"})
        self.assertEqual(index_broll_shots(settings, [shot])["shot_ids"], [key])
        selection.unlink()  # Subsequent projects use the indexed receipt.
        hit, = search_broll_shots(settings, "封箱", availability="timeline_ready")["matches"]
        self.assertEqual((hit["availability"], hit["asset_id"]), ("native_draft", "catalog-42"))
        self.assertEqual(get_broll_shot(settings, key)["media"], {
            "kind": "native_draft", "asset_id": "catalog-42", "shot_id": key})
        self.assertNotIn("receipt", hit)
        placements = [{"shot_id": key, "at": 0, "duration": 4}, {"shot_id": key, "at": 4, "duration": 4,
                       "layout": {"kind": "stacked", "main": "bottom", "split": .5}}]
        recipe = plan_edit(settings, "双栏", [EditClip(source=self.camera, end=8)], width=320, height=480,
                           style_preset="seller_reference", broll=placements)
        planned, _, replay = read_edit_inputs(recipe["recipe_path"], None, None)
        self.assertTrue(all(row["shot_id"] == key and "source" not in row for row in replay))
        _, _, rooted = read_edit_inputs(recipe["recipe_path"], str(self.root), {planned.cuts[0].source_id: self.camera})
        self.assertEqual(replay, rooted)
        result = compile_episode(settings, {"name": "原生素材测试", "width": 320, "height": 480,
                                 "beats": [{"kind": "talk", "source": self.camera, "start": 0, "end": 8,
                                            "captions": False, "sfx": False}], "broll": replay},
                                 include_manifest=True, openfilm_project=True, preview_frames=4)
        self.assertIsNone(result["openfilm_project_path"])
        self.assertIsNone(result["preview_path"])
        self.assertTrue(result["warnings"])
        published = build_manifest(settings, result["manifest"])
        content = json.loads((Path(published["draft_path"]) / "draft_content.json").read_text("utf8"))
        by_id = {m["id"]: m for m in content["materials"]["videos"]}
        tracks = {t["name"]: t["segments"] for t in content["tracks"]}
        full, split = tracks["补充镜头"]
        self.assertEqual(tracks["主画面"][0]["volume"], 1)
        self.assertEqual(tracks["补充镜头人物"][0]["target_timerange"], {"start": 4000000, "duration": 4000000})
        self.assertEqual(full["clip"]["transform"]["y"], 0)
        self.assertAlmostEqual(split["clip"]["transform"]["y"], .5)
        self.assertNotEqual(full["material_id"], split["material_id"])
        for index, seg in enumerate((full, split)):
            m = by_id[seg["material_id"]]
            self.assertEqual(m["material_id"], "catalog-42")
            self.assertEqual(m["check_flag"], 17)
            self.assertEqual(m["copyright_limit_type"], "editor_managed")
            self.assertEqual(Path(m["path"]), cache)
            self.assertGreater(m["crop"]["upper_left_x"], 0)
            self.assertEqual(seg["volume"], 0)
            self.assertEqual(seg["stock_origin_flag"], 7)
            self.assertEqual(seg["target_timerange"], {"start": index * 4000000, "duration": 4000000})
        refs = {r["id"] for r in content["materials"]["sound_channel_mappings"]}
        self.assertTrue(refs.intersection(full["extra_material_refs"]))
        self.assertTrue(refs.intersection(split["extra_material_refs"]))
        placed = place_broll_shots(settings, {"name": "原生素材入轨回读", "width": 320, "height": 480,
                                   "beats": [{"kind": "talk", "source": self.camera, "start": 0, "end": 8,
                                              "captions": False, "sfx": False}]}, placements, dry_run=False)
        self.assertEqual(placed["status"], "placed")
        self.assertEqual([s["asset_id"] for s in placed["readback"]], ["catalog-42", "catalog-42"])
        cache.write_bytes(b"changed")
        self.assertFalse(search_broll_shots(settings, "封箱", availability="native_draft")["matches"])

    def test_place_tool_reads_published_track_and_preserves_existing_inserts(self):
        spec = {"name": "实际入轨", "width": 320, "height": 480, "style_preset": "editorial",
                "beats": [{"kind": "talk", "source": self.camera, "start": 0, "end": 8,
                           "captions": False, "sfx": False}],
                "broll": [{"source": str(self.footage), "at": 0, "duration": 2}]}
        row = {"shot_id": self.shot_id, "at": 3, "duration": 3,
               "layout": {"kind": "stacked", "main": "bottom", "split": 0.48}}
        self.assertEqual(get_broll_shot(self.settings, self.shot_id)["media"]["path"], str(self.footage))
        preview = place_broll_shots(self.settings, spec, [row])
        self.assertEqual(preview["status"], "validated")
        self.assertIsNone(preview["draft_path"])
        result = place_broll_shots(self.settings, spec, [row], dry_run=False)
        self.assertEqual(result["status"], "placed")
        earlier, selected = result["readback"]
        self.assertEqual(earlier["target_range_us"], {"start": 0, "duration": 2_000_000})
        self.assertEqual(selected["shot_id"], self.shot_id)
        self.assertEqual(selected["target_range_us"], {"start": 3_000_000, "duration": 3_000_000})
        self.assertEqual(selected["selected_source_start_s"], 8)
        self.assertEqual(selected["track_source_range_us"]["start"], 0)
        self.assertEqual(selected["volume"], 0)
        self.assertIsNone(selected["asset_id"])
        self.assertEqual(selected["speaker_panel_segments"][0]["volume"], 0)
        self.assertGreater(selected["transform"]["y"], 0)
        self.assertEqual(len(spec["broll"]), 1)
        with self.assertRaises(FileExistsError):
            place_broll_shots(self.settings, spec, [row], dry_run=False)

    def test_library_and_relative_inserts_preserve_dialogue_through_a_cut(self):
        beats = [{"kind": "talk", "source": self.camera, "start": 0, "end": 8, "captions": False, "sfx": False}
                 for _ in range(4)]
        spec = {"name": "B-roll", "width": 320, "height": 480, "fps": 30, "beats": beats}
        original = compile_episode(self.settings, spec, mixdown=True, include_manifest=True)
        file = self.root / "spec.json"
        spec["broll"] = [
            {"shot_id": self.shot_id, "at": 23, "duration": 3},
            {"source": self.footage.name, "source_start": 1.5, "at": 15, "duration": 3,
             "layout": {"kind": "stacked", "main": "bottom", "split": 0.45}},
        ]
        file.write_text(json.dumps(spec), encoding="utf-8")
        result = compile_episode(self.settings, read_episode_file(file), include_manifest=True, mixdown=True,
                                 openfilm_project=True, preview_frames=3)
        tracks = {track["name"]: track["segments"] for track in result["manifest"]["tracks"]}
        self.assertEqual(tracks["主画面"], original["manifest"]["tracks"][0]["segments"])
        self.assertEqual(Path(result["mix_path"]).read_bytes(), Path(original["mix_path"]).read_bytes())
        self.assertEqual(result["episode"]["broll"][-1]["shot_id"], self.shot_id)
        panels = tracks["补充镜头人物"]
        self.assertEqual([(p["start"], p["duration"], p["volume"]) for p in panels],
                         [(15_000_000, 1_000_000, 0), (16_000_000, 2_000_000, 0)])
        upper, full = tracks["补充镜头"]
        self.assertEqual(upper["panel_rect"], [0, 0, 320, 216])
        self.assertGreater(panels[0]["panel_rect"][1], upper["panel_rect"][3])
        self.assertEqual((full["start"], full["source_start"], full["duration"], full["volume"]),
                         (23_000_000, 8_000_000, 3_000_000, 0))
        self.assertGreater(full["clip"]["scale_x"], 1)
        self.assertEqual(full["source"], str(self.footage))
        self.assertEqual(result["duration_us"], original["duration_us"])
        self.assertTrue(Path(result["preview_path"]).is_file())
        film = (Path(result["openfilm_project_path"]) / "film.html").read_text(encoding="utf-8")
        self.assertIn("#t=8.000,11.000", film)
        self.assertIn('title="补充镜头人物"', film)
        self.assertTrue(build_manifest(self.settings, result["manifest"], dry_run=True)["success"])

    def test_invalid_source_or_program_ranges_fail_before_publication(self):
        def plan(rows):
            return plan_broll(self.settings, rows, fps=30, total_frames=900, width=320, height=480)

        base = {"shot_id": self.shot_id, "at": 23, "duration": 3}
        for changes in ({"source_start": 8.3}, {"source_start": 7.5}, {"at": 28},
                        {"duration": 0.001}, {"layout": {"kind": "stacked", "split": 1}}):
            with self.subTest(changes=changes), self.assertRaises(ProductionError):
                plan([{**base, **changes}])
        with self.assertRaisesRegex(ProductionError, "重叠"):
            plan([base, {**base, "at": 25}])

    def test_generated_captions_follow_speaker_only_inside_split(self):
        def insert(at, side):
            return {"source": str(self.footage), "at": at, "duration": 1,
                    "layout": {"kind": "stacked", "main": side, "split": 0.5}}

        windows = [insert(1, "bottom"), insert(3, "top")]
        beat = {"kind": "talk", "source": self.camera, "start": 0, "end": 5, "text": "成片字幕",
                "highlights": ["字幕"]}
        spec = {"name": "跟随口播", "style_preset": "seller_reference", "width": 320, "height": 480,
                "min_scene_seconds": 1, "beats": [beat], "broll": windows}
        normal = compile_episode(self.settings, {**spec, "broll": []}, include_manifest=True)
        result = compile_episode(self.settings, spec, include_manifest=True)
        rows = lambda r: [s for t in r["manifest"]["tracks"] if t["type"] == "text" for s in t["segments"]]
        parts = rows(result)
        self.assertEqual([p["start"] for p in parts], [0, 1_000_000, 2_000_000, 3_000_000, 4_000_000])
        self.assertEqual([parts[i]["clip"] for i in (0, 2, 4)], [rows(normal)[0]["clip"]] * 3)
        self.assertLess(parts[1]["clip"]["transform_y"], -0.75)
        self.assertAlmostEqual(parts[3]["clip"]["transform_y"], 0.2)
        self.assertTrue(all("animations" not in p and "portable_animation" not in p for p in parts[1:]))
        self.assertEqual(normal["episode"]["caption_sfx_frames"], [0])
        self.assertEqual(result["episode"]["caption_sfx_frames"], normal["episode"]["caption_sfx_frames"])
        self.assertEqual(Path(normal["srt_path"]).read_bytes(), Path(result["srt_path"]).read_bytes())
        beat["caption"] = {"y": -0.35}
        custom = rows(compile_episode(self.settings, spec, include_manifest=True))
        self.assertEqual(len(custom), 1)
        self.assertEqual(custom[0]["clip"]["transform_y"], -0.35)
        beat["caption"] = {"zone": "upper"}
        zoned = rows(compile_episode(self.settings, spec, include_manifest=True))
        self.assertEqual(len(zoned), 1)
        self.assertEqual(zoned[0]["clip"]["transform_y"], 0.36)

        recipe_path = plan_edit(self.settings, "按配方分屏", [EditClip(source=self.camera, end=5, caption="成片字幕")],
                                width=320, height=480, style_preset="seller_reference",
                                min_scene_seconds=1, broll=windows)["recipe_path"]
        recipe, _, _ = read_edit_inputs(recipe_path, None, None)
        from_recipe, _ = _recipe_captions(self.settings, recipe)
        self.assertEqual([p["clip"] for p in from_recipe], [p["clip"] for p in parts])

    def test_grouped_captions_keep_independent_slots_in_the_panel(self):
        spec = {"name": "两行字幕", "style_preset": "seller_reference", "width": 320, "height": 480,
                "min_scene_seconds": 1,
                "beats": [{"kind": "talk", "source": self.camera, "start": 0, "end": 3,
                           "caption": {"mode": "stack", "cues": [{"at": 0, "text": "第一项"}, {"at": 0.5, "text": "第二项"}]}}],
                "broll": [{"source": str(self.footage), "at": 1, "duration": 1,
                           "layout": {"kind": "interview_box", "main": "bottom"}}]}
        result = compile_episode(self.settings, spec, include_manifest=True)
        active = [s for t in result["manifest"]["tracks"] if t["type"] == "text"
                  for s in t["segments"] if s["start"] == 1_000_000]
        self.assertEqual(len(active), 2)
        self.assertNotEqual(active[0]["clip"]["transform_y"], active[1]["clip"]["transform_y"])
        for segment in active:
            self.assertLess(segment["clip"]["transform_y"], -0.40)
            self.assertLess(segment["style"]["max_line_width"], 0.7)
