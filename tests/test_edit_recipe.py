from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jianying_ai_mcp.broll_library import index_broll_shots
from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.delivery import deliver_edit
from jianying_ai_mcp.edit_recipe import EditClip, plan_edit, read_edit, read_edit_inputs
from jianying_ai_mcp.media_info import parse_media_info
from jianying_ai_mcp.media_prepare import portable_shot_source
from jianying_ai_mcp.production_graphics import ProductionError
from jianying_ai_mcp.setup_runtime import write_client_configs
from jianying_ai_mcp.storyboard import compile_episode, read_episode_file, select_take


ROOT = Path(__file__).resolve().parents[1]


class RecipeTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".build"
        scratch.mkdir(exist_ok=True)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=scratch)))
        self.settings = replace(Settings.from_env(), work_root=self.root, draft_root=self.root / "library")
        self.media = self.root / "素材"
        self.media.mkdir()
        self.picture = self.media / "card.png"
        shutil.copyfile(ROOT / "test_assets/compat_test_image.png", self.picture)

    def plan(self):
        return plan_edit(self.settings, "剪辑复刻", [EditClip(source="card.png", caption="一句话")],
                         media_root=str(self.media), width=640, height=360, fps=25)

    def test_recipe_relocates_and_fails_before_writing_on_changed_content(self):
        if not self.settings.ffprobe_exe:
            self.skipTest("需要 ffprobe")
        plan = self.plan()
        self.assertEqual(plan["total_frames"], 75)
        moved = self.root / "moved"
        self.media.rename(moved)
        recipe, shots = read_edit(plan["recipe_path"], str(moved), None)
        self.assertEqual(recipe.captions[0].text, "一句话")
        self.assertEqual(shots[0]["source"], str(moved / "card.png"))
        with (moved / "card.png").open("ab") as file:
            file.write(b"changed")
        with self.assertRaisesRegex(ProductionError, "m0001"):
            deliver_edit(self.settings, plan["recipe_path"], media_root=str(moved))
        self.assertFalse((self.root / "production/deliveries").exists())
        with self.assertRaisesRegex(ProductionError, "media_root"):
            plan_edit(self.settings, "越界", [EditClip(source="../elsewhere")], media_root=str(moved))

    def test_library_insert_replays_on_moved_media_with_original_trim(self):
        if not self.settings.ffprobe_exe or not self.settings.ffmpeg_exe:
            self.skipTest("需要 FFmpeg")
        side = self.media / "拍摄.mp4"
        shutil.copyfile(ROOT / "test_assets/compat_test_video.mp4", side)
        shot_id, = index_broll_shots(self.settings, [{
            "source": str(side), "start_ms": 101, "end_ms": 2101, "description": "现场拍摄",
        }], source_pool="personal")["shot_ids"]
        plan = plan_edit(self.settings, "两栏", [EditClip(source="card.png", caption="")],
                         media_root=str(self.media), width=360, height=640, fps=25,
                         broll=[{"shot_id": shot_id, "at": 0.4, "duration": 1.6,
                                 "layout": {"kind": "stacked", "main": "top", "split": 0.45}}])
        moved = self.root / "换机"
        self.media.rename(moved)
        recipe, _, inserts = read_edit_inputs(plan["recipe_path"], str(moved), None)
        self.assertEqual(recipe.broll[0].shot_id, shot_id)
        self.assertEqual(inserts, [{"source": str(moved / side.name), "source_start": 0.101, "at": 0.4,
                                    "duration": 1.6, "layout": {"kind": "stacked", "main": "top", "split": 0.45}}])
        delivered = deliver_edit(self.settings, plan["recipe_path"], media_root=str(moved))
        self.assertTrue(delivered["draft_ready"])
        manifest = json.loads(Path(delivered["manifest_path"]).read_text(encoding="utf-8"))
        clip, = next(t["segments"] for t in manifest["tracks"] if t["name"] == "补充镜头")
        self.assertEqual((clip["start"], clip["duration"], clip["volume"]), (400_000, 1_600_000, 0))
        with (moved / side.name).open("ab") as f:
            f.write(b"updated")
        with self.assertRaisesRegex(ProductionError, "素材内容已变化"):
            read_edit_inputs(plan["recipe_path"], str(moved), None)

    def test_review_mismatch_is_returned_as_failed_delivery_with_artifacts(self):
        if not (self.settings.ffmpeg_exe and self.settings.ffprobe_exe):
            self.skipTest("需要媒体工具")
        plan = plan_edit(self.settings, "验收草稿", [EditClip(source=str(self.picture), caption="一句话")],
                         width=1280, height=720, fps=25)
        # The render returned a real video with the wrong size, duration and rate.
        wrong = str(ROOT / "test_assets/compat_test_video.mp4")
        with patch("jianying_ai_mcp.delivery.export_or_handoff", return_value={"success": True, "output_path": wrong}):
            result = deliver_edit(self.settings, plan["recipe_path"])
        self.assertFalse(result["success"])
        self.assertEqual(result["failed_stage"], "review_export")
        self.assertFalse(result["checks"]["frames"])
        self.assertTrue(Path(result["draft_path"], "draft_content.json").is_file())
        self.assertTrue(Path(result["review_path"]).is_file())
        delivery = Path(result["delivery_dir"])
        self.assertEqual(Path(result["unverified_mp4_path"]), delivery / "video.mp4")
        self.assertEqual(Path(result["srt_path"]), delivery / "captions.srt")
        self.assertEqual(Path(result["archived_recipe_path"]).read_bytes(), Path(plan["recipe_path"]).read_bytes())
        self.assertEqual(Path(result["draft_path"]).parent, self.settings.draft_root)
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["draft"]["name"], result["draft_name"])
        self.assertIn(result["draft_name"], result["user_message"])
        self.assertEqual(result["status"], "manual_export_required")
        reference = json.loads(Path(result["review_path"]).read_text(encoding="utf-8"))
        self.assertTrue((delivery / reference["ranges"][0]["frames"][0]["file"]).is_file())
        reference["sha256"]["srt"] = "0" * 64
        other = self.root / "other-review.json"
        other.write_text(json.dumps(reference), encoding="utf-8")
        with patch("jianying_ai_mcp.delivery.export_or_handoff", return_value={"success": True, "output_path": wrong}):
            compared = deliver_edit(self.settings, plan["recipe_path"], reference_review_path=str(other))
        self.assertEqual([compared["checks"][key] for key in ("same_decoded_video", "same_decoded_audio", "same_srt")],
                         [True, True, False])
        self.assertFalse(compared["success"])

    def test_packaged_panel_fields_and_explicitly_disabled_title_survive_replay(self):
        if not self.settings.ffprobe_exe:
            self.skipTest("需要 ffprobe")
        args = json.loads((ROOT / "examples/edits/packaged-interview.json").read_text(encoding="utf-8"))
        args["clips"] = [EditClip(**row) for row in args["clips"]]
        plan = plan_edit(self.settings, **args, media_root=str(ROOT / "test_assets"))
        recipe, shots = read_edit(plan["recipe_path"], None, None)
        self.assertEqual(recipe.total_frames, 100)
        self.assertIsNone(shots[0]["chapter"])
        self.assertEqual(shots[1]["chapter"], "审片")
        self.assertEqual(recipe.cuts[0].panel.data["items"][1]["at"], 1.2)
        args["clips"][0].panel.data["items"] = []
        with self.assertRaisesRegex(ProductionError, "至少需要 2 项"):
            plan_edit(self.settings, **args, media_root=str(ROOT / "test_assets"))

    def test_batches_do_not_share_takes_and_candidate_ends_at_the_source_boundary(self):
        def batch(key, filename):
            batch_id = key * 32
            report = self.root / "video_analyses" / key / "analysis.json"
            report.parent.mkdir(parents=True)
            report.write_text(json.dumps({"local_video_path": str(self.media / filename), "metadata": {"duration_ms": 1000},
                                         "transcript": [{"start_ms": 500, "end_ms": 1000, "text": "可用原话"}]}))
            catalog = self.root / "media_batches" / batch_id / "batch.json"
            catalog.parent.mkdir(parents=True)
            catalog.write_text(json.dumps({"sources": [{"analysis_report_path": str(report)}]}))
            return batch_id
        batch("a", "a.mp4")
        selected = select_take(self.settings, "可用原话", batch_id=batch("b", "b.mp4"))
        self.assertEqual(selected["source"], str(self.media / "b.mp4"))
        self.assertEqual(selected["end"], 1)

    def test_episode_file_rebinds_nested_media_without_rewriting_text(self):
        beat = {"source": "movie.mp4", "text": "movie.mp4", "voice": {"source": "audio.wav"},
                "layout": {"with": {"media": {"source": "still.png"}}}}
        spec = self.root / "分镜.json"
        spec.write_text(json.dumps({"beats": [beat], "bgm": {"source": "music.wav"}}), encoding="utf-8")
        data = read_episode_file(spec, str(self.media))
        beat = data["beats"][0]
        self.assertEqual((beat["source"], beat["text"]), (str(self.media / "movie.mp4"), "movie.mp4"))
        self.assertEqual(beat["voice"]["source"], str(self.media / "audio.wav"))
        self.assertEqual(beat["layout"]["with"]["media"]["source"], str(self.media / "still.png"))
        self.assertEqual(data["bgm"]["source"], str(self.media / "music.wav"))

    def test_camera_rotation_and_tiff_use_managed_media_with_source_time_preserved(self):
        if not (self.settings.ffmpeg_exe and self.settings.ffprobe_exe):
            self.skipTest("需要媒体工具")
        from PIL import Image
        tif = self.media / "scan.tif"
        with Image.open(self.picture) as photo:
            photo.save(tif)
        def portable(path, first):
            return portable_shot_source(self.settings, path, parse_media_info(self.settings, str(path))["media_info"],
                                        first=first, frames=20, fps=25, width=360, height=640, source_sha256=path.stem)
        png, first = portable(tif, 0)
        self.assertEqual((Path(png).suffix, first), (".png", 0))
        rotated = self.media / "rotated.mp4"
        subprocess.run([str(self.settings.ffmpeg_exe), "-v", "error", "-display_rotation", "90", "-i",
                        str(ROOT / "test_assets/compat_test_video.mp4"), "-c", "copy", str(rotated)], check=True)
        proxy, first = portable(rotated, 50)
        info = parse_media_info(self.settings, proxy)["media_info"]
        self.assertNotEqual(Path(proxy), rotated)
        self.assertEqual((first, info["rotation_degrees"], info["display_width"], info["display_height"]), (25, 0, 360, 640))

    def test_episode_uses_source_time_for_placement_and_frame_time_for_cuts(self):
        video = str(ROOT / "test_assets/compat_test_video.mp4")
        specs = [
            {"kind": "media", "source": video, "start": 0, "end": 0.345},
            {"kind": "talk", "source": video, "start": 5, "end": 5.345, "text": "原话"},
            {"kind": "talk", "source": video, "start": 6, "end": 6.345, "text": "再见", "caption": {"zone": "upper"}},
        ]
        with patch("jianying_ai_mcp.storyboard.choose_caption_anchor", return_value=None) as anchor, \
             patch("jianying_ai_mcp.storyboard.compile_video_timeline", return_value={}) as timeline:
            result = compile_episode(self.settings, {"name": "时间轴", "fps": 10, "source_proxy": "off", "beats": specs})
        if self.settings.ffmpeg_exe and self.settings.ffprobe_exe:
            self.assertAlmostEqual(anchor.call_args.args[2], 5)
        beats = result["episode"]["beats"]
        self.assertEqual([beat["start_s"] for beat in beats], [0, 0.3, 0.6])
        self.assertNotIn("caption_zone", beats[0])
        self.assertEqual([beat["caption_zone"] for beat in beats[1:]], ["bottom", "upper"])
        self.assertEqual([c["start_frame"] for c in timeline.call_args.kwargs["captions"]], [3, 6])

    def test_noise_cue_survives_a_python_restart(self):
        code = ("import hashlib; from jianying_ai_mcp.sound_design import _render; "
                "print(hashlib.sha256(_render('swipe').tobytes()).hexdigest())")
        def digest(seed):
            return subprocess.check_output([sys.executable, "-c", code], text=True, env={**os.environ,
                                           "PYTHONHASHSEED": str(seed), "PYTHONPATH": str(ROOT / "src")}).strip()
        self.assertEqual(digest(1), digest(2))

    def test_nondefault_node_from_runtime_discovery_reaches_settings(self):
        binary, cli = self.root / "bundled-node", self.root / "openfilm.mjs"
        with patch.dict(os.environ, {"JIANYING_OPENFILM_EXE": "", "JIANYING_OPENFILM_CLI": ""}), \
             patch("jianying_ai_mcp.openfilm_runtime.discover", return_value=(binary, cli)):
            settings = Settings.from_env()
        self.assertEqual((settings.openfilm_exe, settings.openfilm_cli), (binary, cli))

    def test_generated_client_formats_preserve_paths_and_expose_direct_tools(self):
        binary, env = str(self.root / "工具 环境/python.exe"), {"JIANYING_MCP_WORK_ROOT": str(self.media)}
        paths = write_client_configs(self.root, binary, env)
        def read(key):
            return json.loads(Path(paths[key]).read_text(encoding="utf-8"))
        stdio = read("stdio_config")["mcpServers"]["jianying-ai"]
        vscode = read("vscode_config")["servers"]["jianying-ai"]
        codex = tomllib.loads(Path(paths["codex_config"]).read_text(encoding="utf-8"))["mcp_servers"]["jianying-ai"]
        for client in (stdio, vscode, codex):
            self.assertEqual((client["command"], client["args"], client["env"]), (binary, ["-m", "jianying_ai_mcp"], env))
        v1 = read("opencode_config")["mcp"]["jianying-ai"]
        v2 = read("opencode_v2_config")["mcp"]["servers"]["jianying-ai"]
        for client in (v1, v2):
            self.assertEqual((client["command"], client["environment"]), ([binary, "-m", "jianying_ai_mcp"], env))
        self.assertFalse(v2["codemode"])
