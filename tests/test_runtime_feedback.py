from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from jianying_ai_mcp.asset_batch import export_media_batch_transcript
from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.delivery import _review, deliver_edit
from jianying_ai_mcp.desktop_runtime import _stop_process
from jianying_ai_mcp.edit_recipe import EditClip, plan_edit, read_edit
from jianying_ai_mcp.export_review import review_export
from jianying_ai_mcp.video_observer import _sample_frames


ROOT = Path(__file__).resolve().parents[1]


class LocalCallerTests(unittest.TestCase):
    def test_cli_uses_both_transports_and_reports_tool_errors(self):
        build = ROOT / ".build"
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build, prefix="local-caller-") as tmp:
            env = {**os.environ, "JIANYING_MCP_WORK_ROOT": tmp, "PYTHONPATH": os.pathsep.join(sys.path)}
            cli = [sys.executable, "-B", "-m", "jianying_ai_mcp"]

            def call(*args, check=True):
                response = subprocess.run([*cli, *args], cwd=tmp, env=env, stdin=subprocess.DEVNULL,
                                          capture_output=True, encoding="utf-8", errors="replace", timeout=35)
                self.assertEqual(response.returncode == 0, check, response.stdout + response.stderr)
                return json.loads(response.stdout)

            self.assertIn("export_media_batch_transcript", call("tools")["tools"])
            args_file = Path(tmp) / "参数.json"
            args_file.write_text(json.dumps({"section": "edit"}), encoding="utf-8-sig")
            expected = call("call", "get_video_workflow", "--args-file", str(args_file))["production_guidance"]
            # A host can have different runtime paths from those pinned by the installer.
            configured = Path(tmp) / "installed-work"
            batch_id = "a" * 32
            cache = configured / "media_batches" / batch_id
            cache.mkdir(parents=True)
            (cache / "batch.json").write_text('{"sources":[]}', encoding="utf-8")
            config = Path(tmp) / "mcp-stdio.json"
            entry = {"command": sys.executable, "args": ["-B", "-m", "jianying_ai_mcp"],
                     "env": {"JIANYING_MCP_WORK_ROOT": str(configured)}}
            config.write_text(json.dumps({"mcpServers": {"jianying-ai": entry}}), encoding="utf-8")
            export = ["call", "export_media_batch_transcript", "--args", json.dumps({"batch_id": batch_id})]
            self.assertEqual(Path(call(*export, "--config", str(config))["transcript_path"]), cache / "transcripts.txt")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            process = subprocess.Popen([*cli, "serve-http", "--port", str(port), "--config", str(config)],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       env=env, cwd=tmp, start_new_session=os.name != "nt")
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    self.assertIsNone(process.poll(), "HTTP 服务启动失败")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                            break
                    except OSError:
                        time.sleep(0.1)
                url = f"http://127.0.0.1:{port}/mcp"
                actual = call("call", "get_video_workflow", "--url", url, "--args-file", str(args_file))
                self.assertEqual(actual["production_guidance"], expected)
                self.assertEqual(Path(call(*export, "--url", url)["transcript_path"]), cache / "transcripts.txt")
                error = call("call", "export_media_batch_transcript", "--url", url, "--args", '{"batch_id":"bad"}', check=False)
                self.assertFalse(error["success"])
            finally:
                # Windows venv python.exe may have a separate interpreter process.
                _stop_process(process)


class MediaFeedbackTests(unittest.TestCase):
    def setUp(self):
        build = ROOT / ".build"
        build.mkdir(exist_ok=True)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=build, prefix="media-feedback-")))
        self.settings = replace(Settings.from_env(), work_root=self.root, draft_root=self.root / "drafts")

    def test_cached_batch_exports_all_cues_and_marks_unavailable_sources(self):
        batch = "f" * 32
        directory = self.root / "media_batches" / batch
        directory.mkdir(parents=True)
        report = self.root / "analysis.json"
        text = "经过复听的完整对白" * 60
        report.write_text(json.dumps({"transcript": [{"start_ms": 1234, "end_ms": 5005, "text": text, "quality": "review"}]}))
        rows = [{"source_id": f"m{i:04d}", "media_path": f"素材{i}.mp4", "analysis_report_path": str(report)} for i in range(1, 38)]
        rows[-1]["analysis_report_path"] = str(self.root / "missing.json")
        (directory / "batch.json").write_text(json.dumps({"sources": rows}))
        result = export_media_batch_transcript(self.settings, batch)
        content = Path(result["transcript_path"]).read_text(encoding="utf-8")
        self.assertEqual((result["source_count"], result["transcript_cue_count"], result["unavailable_source_ids"]), (37, 36, ["m0037"]))
        self.assertEqual(content.count(text), 36)
        self.assertIn("00:00:01.234 --> 00:00:05.005 [待复听]", content)
        self.assertLess(content.index("m0001"), content.index("m0037"))
        self.assertIn("转写缓存不可读", content)

    def video(self, name, color, *, duration=1):
        if not (self.settings.ffmpeg_exe and self.settings.ffprobe_exe):
            self.skipTest("需要 FFmpeg 和 ffprobe")
        path = self.root / f"{name}.mp4"
        subprocess.run([str(self.settings.ffmpeg_exe), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        f"color=c={color}:s=320x320:r=12:d={duration}", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                       stdin=subprocess.DEVNULL, check=True)
        return str(path)

    def test_only_selected_jpegs_are_written_and_no_candidates_are_unlinked(self):
        path = self.video("静止", "navy", duration=4)
        folder = self.root / "frames"
        folder.mkdir()
        with patch.object(Path, "unlink", side_effect=AssertionError("代表帧筛选触发文件删除")), \
                patch("jianying_ai_mcp.video_observer.subprocess.run", wraps=subprocess.run) as run:
            frames = _sample_frames(self.settings.ffmpeg_exe, Path(path), folder, "efficient", 128, 0, 4, 4)
        self.assertTrue(run.call_args_list)
        self.assertTrue(all(call.kwargs["stdin"] == subprocess.DEVNULL for call in run.call_args_list))
        self.assertEqual(len(frames), 1)
        self.assertEqual([Path(f["path"]) for f in frames], list(folder.rglob("*.jpg")))
        with Image.open(frames[0]["path"]) as image:
            self.assertEqual(image.size, (128, 128))

    def test_native_green_screen_requires_matching_authored_picture(self):
        green = self.video("green", "lime")
        scan = review_export(self.settings, green, max_frames=0)
        self.assertEqual(scan["green_detection"]["suspect_frames"], 12)
        self.assertEqual(scan["green_segments"], [{"start_ms": 0, "end_ms": 1000}])
        for color, expected in (("navy", False), ("lime", True)):
            with self.subTest(color=color):
                preview = self.root / "intended.png"
                Image.new("RGB", (320, 320), color).save(preview)
                plan = plan_edit(self.settings, "验收", [EditClip(source=str(preview), hold_seconds=1, caption="")],
                                 width=320, height=320, fps=12)
                recipe, _ = read_edit(plan["recipe_path"], None, None)
                with patch("jianying_ai_mcp.delivery.look_openfilm", return_value={"artifact_paths": [str(preview)]}) as look:
                    review = _review(self.settings, green, recipe, lambda: "authored-preview")
                self.assertEqual(review["checks"]["green_frames"], expected)
                self.assertEqual(review["green_review"]["suspect_frames"], 12)
                look.assert_called_once()

    def test_unreadable_native_file_hands_off_and_partial_scan_fails(self):
        video = self.video("blue", "navy")
        plan = plan_edit(self.settings, "质检", [EditClip(source=video, caption="")], width=720, height=720, fps=25)
        with patch("jianying_ai_mcp.delivery.export_or_handoff", return_value={
            "success": True, "output_path": str(self.root / "missing-native.mp4")
        }) as native:
            failed = deliver_edit(self.settings, plan["recipe_path"])
        native.assert_called_once()
        self.assertEqual(failed["status"], "manual_export_required")
        self.assertIn(failed["draft_name"], failed["user_message"])
        self.assertTrue(Path(failed["draft_path"], "draft_content.json").is_file())
        self.assertNotIn("mp4_path", failed)
        recipe, _ = read_edit(plan["recipe_path"], None, None)
        partial = review_export(self.settings, video, max_frames=0)
        partial["green_detection"]["frames_checked"] -= 1
        with patch("jianying_ai_mcp.delivery.review_export", return_value=partial):
            review = _review(self.settings, video, recipe, None)
        self.assertFalse(review["checks"]["green_scan_complete"])
