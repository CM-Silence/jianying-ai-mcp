from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from jianying_ai_mcp.broll_library import index_broll_shots, resolve_broll_shot, search_broll_shots
from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.episode_broll import plan_broll
from jianying_ai_mcp.jianying_stock import list_stock_videos
from jianying_ai_mcp.video_observer import analyze_video, save_observations


ROOT = Path(__file__).resolve().parents[1]


class BrollLibraryTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".build"
        scratch.mkdir(exist_ok=True)
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=scratch)))
        self.settings = replace(Settings.from_env(), work_root=root / "work", jianying_stock_root=root / "stock",
                                draft_root=root / "drafts")
        if not self.settings.ffprobe_exe:
            self.skipTest("需要 ffprobe")
        self.settings.jianying_stock_root.mkdir()
        self.source = self.settings.jianying_stock_root / "warehouse.mp4"
        shutil.copyfile(ROOT / "test_assets/compat_test_video.mp4", self.source)
        self.shot = {"source": str(self.source), "start_ms": 100, "end_ms": 1900,
                     "description": "传送带上的包裹被扫码", "tags": ["仓库", "Scanner"],
                     "use_case": "订单进入自动分拣", "source_url": "https://example.org/shot", "license": "CC0"}

    def test_cross_project_search_and_idempotent_update(self):
        other = {**self.shot, "start_ms": 2000, "end_ms": 2900, "description": "机房里的服务器",
                 "use_case": "处理订单", "tags": []}
        first, second = index_broll_shots(self.settings, [self.shot, other])["shot_ids"]
        settings = replace(self.settings, draft_root=self.source.parent / "another-project")
        matches = search_broll_shots(settings, "订单进入自动分拣")["matches"]
        self.assertEqual([m["shot_id"] for m in matches], [first, second])
        self.assertEqual(resolve_broll_shot(settings, first)["source_url"], self.shot["source_url"])
        search = search_broll_shots(settings, "订单", min_duration_ms=1000)
        self.assertEqual([m["shot_id"] for m in search["matches"]], [first])
        self.assertEqual(search_broll_shots(settings, "Scanner")["matches"][0]["shot_id"], first)
        updated = {**self.shot, "description": "新画面描述", "tags": ["扫码枪"], "use_case": "交付"}
        self.assertEqual(index_broll_shots(settings, [updated])["shot_ids"], [first])
        self.assertFalse(search_broll_shots(settings, "Scanner")["matches"])
        self.assertEqual(search_broll_shots(settings, "扫码枪")["matches"][0]["description"], "新画面描述")

    def test_discovery_pool_isolation_and_reference_rejection(self):
        other = self.source.parent.parent / "camera.mp4"
        shutil.copyfile(self.source, other)
        draft = self.source.parent / "参考稿"
        draft.mkdir()
        (draft / "draft_content.json").write_text("{}")
        draft_video = draft / "现场.mp4"
        shutil.copyfile(other, draft_video)
        catalog = list_stock_videos(self.settings)
        self.assertIn(str(self.source), [r["source"] for r in catalog["videos"]])
        self.assertNotIn(str(draft_video), [r["source"] for r in catalog["videos"]])
        with self.assertRaisesRegex(ValueError, "剪映素材"):
            index_broll_shots(self.settings, [{**self.shot, "source": str(other)}])
        for pool in ("personal", "jianying"):
            with self.assertRaisesRegex(ValueError, "草稿"):
                index_broll_shots(self.settings, [{**self.shot, "source": str(draft_video)}], source_pool=pool)
        index_broll_shots(self.settings, [{**self.shot, "source": str(other)}], source_pool="personal")
        self.assertFalse(search_broll_shots(self.settings, "扫码")["matches"])
        self.assertEqual(len(search_broll_shots(self.settings, "扫码", source_pool="personal")["matches"]), 1)
        index_broll_shots(self.settings, [{**self.shot, "asset_title": "物流中心"}])
        match, = search_broll_shots(self.settings, "物流中心")["matches"]
        self.assertEqual((match["source_pool"], match["origin"]), ("jianying", "designated_stock_folder"))

    def test_changed_missing_and_batch_validation(self):
        with self.assertRaisesRegex(ValueError, "源区间"):
            index_broll_shots(self.settings, [self.shot, {**self.shot, "end_ms": 10**8}])
        self.assertFalse(search_broll_shots(self.settings, "扫码")["matches"])
        first, = index_broll_shots(self.settings, [self.shot])["shot_ids"]
        stat = self.source.stat()
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        with self.assertRaisesRegex(ValueError, "变动"):
            resolve_broll_shot(self.settings, first)
        self.assertEqual(search_broll_shots(self.settings, "扫码")["unavailable_skipped"], 1)
        refreshed, = index_broll_shots(self.settings, [self.shot])["shot_ids"]
        self.assertNotEqual(first, refreshed)
        self.source.unlink()
        with self.assertRaises(FileNotFoundError):
            resolve_broll_shot(self.settings, refreshed)
        self.assertFalse(search_broll_shots(self.settings, "扫码")["matches"])

    def test_editor_preview_is_searchable_and_separate_from_local_video(self):
        data = self.source.parent.parent / "User Data"
        settings = replace(self.settings, draft_root=data / "Projects" / "com.lveditor.draft")
        cache = data / "Cache" / "onlineMaterial"
        cache.mkdir(parents=True)
        native = cache / "native.mp4"
        native.write_bytes(b"unreadable video cache fixture")
        self.assertIn(str(native), [r["source"] for r in list_stock_videos(settings)["needs_editor_preview"]])
        row = {**self.shot, "source": str(native)}
        with self.assertRaises(ValueError):
            index_broll_shots(settings, [row])
        shot_id, = index_broll_shots(settings, [{**row, "editor_verified_ms": 7000}])["shot_ids"]
        match, = search_broll_shots(settings, "扫码", availability="editor_only")["matches"]
        self.assertEqual((match["shot_id"], match["origin"]), (shot_id, "online_material_cache"))
        self.assertFalse(search_broll_shots(settings, "扫码", availability="local_video")["matches"])
        with self.assertRaisesRegex(ValueError, "编辑器播放"):
            plan_broll(settings, [{"shot_id": shot_id, "at": 0, "duration": 1}],
                       width=320, height=480, fps=30, total_frames=30)

    def test_import_candidates_uses_only_in_range_frame_evidence(self):
        if not self.settings.ffmpeg_exe:
            self.skipTest("需要 ffmpeg")
        report = analyze_video(self.settings, str(self.source), transcription="none", max_frames=2)
        inside = report["frames"][0]
        end = min(inside["time_ms"] + 1000, report["metadata"]["duration_ms"])
        observations = [
            {"time_ms": inside["time_ms"], "description": "订单包裹", "tags": ["仓储"]},
            {"time_ms": end, "description": "另一个镜头"},
        ]
        candidate = {"start_ms": inside["time_ms"], "end_ms": end, "reason": "说明物流"}
        save_observations(self.settings, report["analysis_id"], observations, [candidate])
        result = index_broll_shots(self.settings, analysis_id=report["analysis_id"])
        data = resolve_broll_shot(self.settings, result["shot_ids"][0])
        self.assertEqual(data["description"], "订单包裹")
        self.assertEqual(data["tags"], ["仓储"])
        self.assertEqual(data["use_case"], "说明物流")
        self.assertEqual(data["preview_frame"]["path"], inside["path"])
        self.assertEqual(search_broll_shots(self.settings, "另一个镜头")["matches"], [])
        with self.source.open("ab") as f:
            f.write(b"updated")
        with self.assertRaisesRegex(ValueError, "变动"):
            index_broll_shots(self.settings, analysis_id=report["analysis_id"])


if __name__ == "__main__":
    unittest.main()
