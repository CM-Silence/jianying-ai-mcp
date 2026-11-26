from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pyJianYingDraft as draft
from mcp.server.fastmcp import FastMCP

from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.draft_ops import ManifestError, build_manifest, repair_caption_references
from jianying_ai_mcp.mcp_tools.drafts import _load_manifest, build_draft


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "test_assets"


class DraftGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        build = ROOT / ".build"
        build.mkdir(exist_ok=True)
        test_root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=build)))
        self.settings = Settings(test_root / "drafts", test_root / "work", None)

    def test_frame_exact_source_trim_preserves_timeline_and_rejects_real_overflow(self) -> None:
        original_material = draft.VideoMaterial

        def rounded_material(path: str) -> draft.VideoMaterial:
            material = original_material(path)
            material.duration = 3_333_000
            return material

        row = {"source": str(ASSETS / "compat_test_video.mp4"), "duration": 3_333_333}
        manifest = {
            "draft": {"name": "frame-exact"},
            "tracks": [{"type": "video", "name": "video", "segments": [row]}],
        }
        with (
            patch("jianying_ai_mcp.draft_ops.draft.VideoMaterial", side_effect=rounded_material),
            patch("jianying_ai_mcp.draft_ops._precise_media_duration", return_value=3_333_333),
        ):
            result = build_manifest(self.settings, manifest)
            row["duration"] = 3_333_500
            with self.assertRaisesRegex(ManifestError, "超出素材时长"):
                build_manifest(self.settings, manifest, dry_run=True)

        content = json.loads((Path(result["draft_path"]) / "draft_content.json").read_text(encoding="utf-8"))
        segment = content["tracks"][0]["segments"][0]
        self.assertEqual(content["materials"]["videos"][0]["duration"], 3_333_333)
        self.assertEqual(segment["source_timerange"], {"start": 0, "duration": 3_333_333})
        self.assertEqual(segment["target_timerange"], segment["source_timerange"])
        self.assertEqual(segment["speed"], 1.0)

    def test_generated_text_keeps_only_registered_effect_refs(self) -> None:
        manifest = {
            "draft": {"name": "text-refs"},
            "tracks": [
                {"type": "text", "name": "title", "segments": [{
                    "text": "标题",
                    "duration": "4s",
                    "animations": [{"category": "text_intro", "name": next(iter(draft.TextIntro)).name}],
                }]},
                {"type": "sticker", "name": "sticker", "segments": [{"resource_id": "6166716045446943233", "duration": "2s"}]},
            ],
            "srt_imports": [{"source": str(ASSETS / "captions.srt"), "clip": {"transform_y": -0.8}}],
        }
        result = build_manifest(self.settings, manifest)
        content = json.loads((Path(result["draft_path"]) / "draft_content.json").read_text(encoding="utf-8"))
        tracks = {track["name"]: track for track in content["tracks"]}
        animation_id = content["materials"]["material_animations"][0]["id"]
        self.assertEqual(tracks["title"]["segments"][0]["extra_material_refs"], [animation_id])
        self.assertEqual(tracks["sticker"]["segments"][0]["extra_material_refs"], [])
        self.assertTrue(all(not segment["extra_material_refs"] for segment in tracks["字幕"]["segments"]))

    def test_manifest_path_validates_one_local_json_object(self) -> None:
        path = self.settings.work_root.parent / "manifest.json"
        path.write_text('{"tracks": []}', encoding="utf-8-sig")
        self.assertEqual(_load_manifest(None, str(path.resolve())), {"tracks": []})
        self.assertEqual(_load_manifest({"tracks": []}, None), {"tracks": []})
        for manifest, filename in (({}, str(path.resolve())), (None, None), (None, path.name)):
            with self.assertRaises(ManifestError):
                _load_manifest(manifest, filename)
        path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ManifestError, "必须是对象"):
            _load_manifest(None, str(path.resolve()))

        mcp = FastMCP("test-draft-manifest-path")
        mcp.add_tool(build_draft)
        tool = mcp._tool_manager.get_tool("build_draft")
        self.assertEqual(set(tool.parameters["properties"]), {
            "manifest", "manifest_path", "dry_run", "allow_experimental_effects",
        })
        self.assertNotIn("manifest", tool.parameters.get("required", []))

    def test_repair_copies_only_explicit_missing_text_refs(self) -> None:
        path = self.settings.work_root.parent / "legacy.json"
        content = {
            "materials": {"texts": [{"id": "caption"}], "material_animations": [{"id": "fade"}]},
            "tracks": [
                {"type": "text", "segments": [{"material_id": "caption", "extra_material_refs": ["legacy-speed", "fade", "unrelated"]}]},
                {"type": "video", "segments": [{"material_id": "footage", "extra_material_refs": ["legacy-speed"]}]},
            ],
        }
        original = json.dumps(content).encode("utf-8")
        path.write_bytes(original)
        for ref in ("fade", "absent"):
            with self.assertRaises(ManifestError):
                repair_caption_references(self.settings, str(path), [ref], dry_run=False)
        preview = repair_caption_references(self.settings, str(path), ["legacy-speed"])
        self.assertEqual(preview["patches"], [{"track_index": 0, "segment_index": 0, "removed_ids": ["legacy-speed"]}])
        self.assertFalse(self.settings.work_root.exists())
        result = repair_caption_references(self.settings, str(path), ["legacy-speed"], dry_run=False)
        fixed = json.loads(Path(result["draft_content_path"]).read_text(encoding="utf-8"))
        self.assertEqual(fixed["tracks"][0]["segments"][0]["extra_material_refs"], ["fade", "unrelated"])
        self.assertEqual(fixed["tracks"][1], content["tracks"][1])
        self.assertEqual(path.read_bytes(), original)

    def test_generated_identity_uses_final_name_path_and_unique_id(self):
        from jianying_ai_mcp.draft_ops import _set_published_identity, _prepare_macos_timeline
        with tempfile.TemporaryDirectory(dir=self.settings.draft_root.parent) as tmp:
            root = Path(tmp)
            ids = set()
            for name in ("第一次", "第二次"):
                target = root / name
                target.mkdir()
                (target / "draft_meta_info.json").write_text("{}", encoding="utf-8")
                (target / "draft_content.json").write_text('{"platform":{},"new_version":"110.0.0"}', encoding="utf-8")
                _prepare_macos_timeline(target, "5.5.0")
                self.assertEqual((target / "draft_content.json").read_bytes(), (target / "draft_info.json").read_bytes())
                content = json.loads((target / "draft_info.json").read_text(encoding="utf-8"))
                self.assertEqual((content["platform"]["os"], content["new_version"]), ("mac", "103.0.0"))
                _set_published_identity(target, target, 5400000)
                meta = json.loads((target / "draft_meta_info.json").read_text(encoding="utf-8"))
                self.assertEqual((meta["draft_name"], meta["draft_fold_path"], meta["tm_duration"]),
                                 (name, target.as_posix(), 5400000))
                ids.add(meta["draft_id"])
            self.assertEqual(len(ids), 2)
