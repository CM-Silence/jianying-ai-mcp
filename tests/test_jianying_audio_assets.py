from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.jianying_audio_assets import JianyingAudioAssetError, inspect_jianying_audio, resolve_jianying_audio


ROOT = Path(__file__).resolve().parents[1]


class JianyingAudioAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        build = ROOT / ".build"
        build.mkdir(exist_ok=True)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=build))).resolve()
        self.draft = self.root / "剪映草稿"
        self.draft.mkdir()
        self.settings = Settings(self.root / "drafts", self.root / "work", None)
        self.materials = [
            {"id": "library-song", "type": "music", "name": "轻节奏", "music_id": "10001",
             "duration": 40_000_000, "path": "<cloud://10001>", "copyright_limit_type": "restricted"},
            {"id": "library-effect", "type": "sound", "name": "打字声", "effect_id": "20002",
             "duration": 500_000, "path": "##_draftpath_placeholder_01234567-ABCD_##/media/sfx.wav", "category_id": "cached"},
            {"id": "generic-audio", "type": "extract_music", "name": "原声", "music_id": "generic-audio",
             "duration": 42_000_000, "path": "other.wav"},
        ]
        self.local_audio = self.draft / "media" / "sfx.wav"
        self.local_audio.parent.mkdir()
        self.local_audio.write_bytes((ROOT / "test_assets" / "compat_test_music.wav").read_bytes())
        self.content_path = self.draft / "draft_content.json"
        self.write_draft()

    def write_draft(self) -> None:
        self.content_path.write_text(json.dumps({"materials": {"audios": self.materials}}, ensure_ascii=False), encoding="utf-8")

    def select(self, material_id: str) -> dict:
        return resolve_jianying_audio(self.settings, {"draft_path": str(self.draft), "material_id": material_id})

    def test_catalog_pagination_preserves_native_kinds_and_reports_local_availability(self) -> None:
        first = inspect_jianying_audio(self.settings, str(self.draft), limit=1)
        self.assertEqual(first["total"], 2)
        self.assertEqual(first["next_offset"], 1)
        self.assertEqual(first["assets"][0]["availability"], "editor_reference")
        self.assertEqual(first["assets"][0]["copyright_limit_type"], "restricted")
        second = inspect_jianying_audio(self.settings, str(self.content_path), offset=1)
        self.assertEqual(second["assets"][0]["source"], str(self.local_audio))
        self.assertEqual(second["assets"][0]["kind"], "sound_effect")
        self.assertIsNone(second["next_offset"])
        self.assertEqual(inspect_jianying_audio(self.settings, str(self.draft), keyword="打字")["total"], 1)

    def test_resolves_original_native_material_without_editing_or_fetching(self) -> None:
        original = self.content_path.read_bytes()
        song = self.select("library-song")
        self.assertIsNone(song["source"])
        self.assertEqual(song["duration_us"], 40_000_000)
        self.assertEqual(song["material"], self.materials[0])
        effect = self.select("library-effect")
        self.assertEqual(effect["source"], str(self.local_audio))
        self.assertEqual(effect["material"], self.materials[1])
        self.assertEqual(self.content_path.read_bytes(), original)
        self.assertFalse(self.settings.work_root.exists())
        with self.assertRaises(JianyingAudioAssetError):
            self.select("generic-audio")

    def test_remote_paths_and_missing_files_cannot_be_selected(self) -> None:
        self.materials[0]["path"] = "https://example.invalid/unavailable/?token=private"
        self.materials[1]["path"] = "missing.wav"
        self.write_draft()
        report = inspect_jianying_audio(self.settings, str(self.draft))
        self.assertEqual([row["availability"] for row in report["assets"]], ["unsupported_path", "missing_file"])
        self.assertNotIn("token=private", json.dumps(report))
        for material_id in ("library-song", "library-effect"):
            with self.assertRaises(JianyingAudioAssetError):
                self.select(material_id)

    def test_rejects_draft_relative_escape_and_accepts_blank_native_path(self) -> None:
        (self.root / "outside.wav").write_bytes(b"unrelated")
        self.materials[0]["path"] = "../outside.wav"
        self.materials[1]["path"] = ""
        self.write_draft()
        rows = inspect_jianying_audio(self.settings, str(self.draft))["assets"]
        self.assertEqual([row["availability"] for row in rows], ["unsupported_path", "editor_reference"])
        self.assertIsNone(self.select("library-effect")["source"])

    def test_accepts_readable_draft_info_when_content_is_absent(self) -> None:
        self.content_path.rename(self.draft / "draft_info.json")
        self.assertEqual(inspect_jianying_audio(self.settings, str(self.draft))["total"], 2)

    def test_caption_accents_use_native_sound_keep_distance_and_respect_silence(self) -> None:
        from jianying_ai_mcp.sound_design import caption_accent_tracks

        selector = {"draft_path": str(self.draft), "material_id": "library-effect"}

        def cue(frame, text):
            return {"start_frame": frame, "end_frame": frame + 20, "text": text}

        groups = [
            ([cue(0, "普通陈述"), cue(12, "关键：交付"), cue(24, "交付")], ["交付"], selector),
            ([cue(48, "交付")], ["交付"], selector),
            ([cue(102, "交付")], ["交付"], selector),
            ([cue(140, "普通陈述")], [], selector),
            ([cue(170, "交付")], ["交付"], False),
            ([cue(200, "按时交付")], ["交付"], selector),
        ]
        audio = caption_accent_tracks(self.settings, groups, fps=30, total_frames=210, occupied=[100])
        self.assertEqual([a["start_frame"] for a in audio], [12, 200])
        self.assertEqual([a["frames"] for a in audio], [12, 10])
        self.assertTrue(all(a["jianying_audio"] == selector and a["volume"] == 0.18 for a in audio))

    def test_caption_sound_reference_relocates_with_the_input_library(self) -> None:
        from dataclasses import replace
        from shutil import copyfile
        from jianying_ai_mcp.edit_recipe import EditClip, plan_edit, read_edit
        from jianying_ai_mcp.delivery import _recipe_captions

        settings = replace(Settings.from_env(), work_root=self.root / "work", draft_root=self.root / "drafts")
        assets = self.root / "sources"
        assets.mkdir()
        self.draft.rename(assets / "音效库")
        copyfile(ROOT / "test_assets" / "compat_test_image.png", assets / "image.png")
        plan = plan_edit(settings, "原生字幕音效", [
            EditClip(source="image.png", caption="准时交付", highlights=["交付"],
                     caption_sfx={"draft_path": "音效库", "material_id": "library-effect"})
        ], media_root=str(assets))
        moved = self.root / "relocated"
        assets.rename(moved)
        recipe, _ = read_edit(plan["recipe_path"], str(moved), None)
        _, sounds = _recipe_captions(settings, recipe)
        self.assertEqual(len(sounds), 1)
        self.assertEqual(Path(sounds[0]["jianying_audio"]["draft_path"]), moved / "音效库" / "draft_content.json")
