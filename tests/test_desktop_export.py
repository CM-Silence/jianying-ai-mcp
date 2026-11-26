import runpy
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.desktop_export import _EXPORT_LOCK, export_desktop_mp4, export_or_handoff
from jianying_ai_mcp.desktop_runtime import DesktopToolError


ROOT = Path(__file__).resolve().parents[1]
MODULE = "jianying_ai_mcp.desktop_export"


class DesktopExportTests(unittest.TestCase):
    def setUp(self):
        build = ROOT / ".build"
        build.mkdir(exist_ok=True)
        temp = self.enterContext(TemporaryDirectory(dir=build, prefix="desktop-export-test-"))
        self.root = Path(temp).resolve()
        self.draft_name = "有空格 项目"
        self.draft = self.root / "draft" / self.draft_name
        self.draft.mkdir(parents=True)
        ffprobe = self.root / "ffprobe.exe"
        ffprobe.touch()
        self.settings = Settings(
            draft_root=self.draft.parent, work_root=self.root / "work",
            jianying_exe=None, ffprobe_exe=ffprobe,
        )
        self.probe = self.enterContext(patch(f"{MODULE}.probe_desktop", return_value={
            "desktop_export_ready": True, "installed_version": "11.4.2.14459",
            "desktop_version_baseline": "11.4.2.14459", "desktop_export_prerequisites_ok": True,
        }))
        self.doctor = self.enterContext(patch(f"{MODULE}.diagnose_desktop_draft", return_value={"ok": True}))
        self.cli = self.enterContext(patch(f"{MODULE}.run_jianying_cli", side_effect=self.run_export))
        self.media = {
            "format": "mov,mp4,m4a,3gp,3g2,mj2", "has_video": True,
            "width": 1080, "height": 1920, "duration": 3.0, "frame_rate": 30.0,
        }
        self.ffprobe = self.enterContext(patch(f"{MODULE}.parse_media_info", return_value={"media_info": self.media}))

    def run_export(self, settings, args, *, timeout):
        self.assertIs(settings, self.settings)
        self.assertEqual(args[0], "export")
        self.assertNotIn("--no-wait", args)
        self.assertEqual(args[args.index("--timeout") + 1], str(timeout))
        title = args[args.index("--title") + 1]
        destination = Path(args[args.index("--output-dir") + 1])
        self.assertEqual(destination.parent, settings.work_root / "desktop-exports")
        self.assertTrue(title.startswith("render-"))
        self.assertEqual(list(destination.iterdir()), [])
        output = destination / f"{title}.mp4"
        output.write_bytes(b"new video")
        return {"output": str(output), "exported": True}

    def test_exports_verified_portrait_mp4_to_unique_job_directory(self):
        first = export_desktop_mp4(self.settings, self.draft_name, timeout=60)
        second = export_desktop_mp4(self.settings, self.draft_name, timeout=60)
        self.assertTrue(first["success"])
        self.assertEqual(first["installed_version"], "11.4.2.14459")
        self.assertTrue(first["desktop_version_baseline_matched"])
        self.assertEqual(first["media_info"], self.media)
        self.assertNotEqual(first["output_path"], second["output_path"])
        self.assertEqual(Path(first["output_path"]).read_bytes(), b"new video")
        self.doctor.assert_called_with(self.settings, str(self.draft))
        self.ffprobe.assert_called_with(self.settings, second["output_path"])
        args = self.cli.call_args.args[1]
        self.assertEqual(args[1:3], ["--draft", self.draft_name])
        self.assertEqual(args[7:15], ["--resolution", "1080P", "--format", "MP4", "--framerate", "30", "--timeout", "60"])

    def test_accepts_rational_ntsc_and_passes_exact_desktop_label(self):
        self.media["frame_rate"] = 30000 / 1001
        export_desktop_mp4(self.settings, self.draft_name, framerate=29.97)
        args = self.cli.call_args.args[1]
        self.assertEqual(args[args.index("--framerate") + 1], "29.97")

    def test_rejects_wrong_reported_path_even_when_job_file_exists(self):
        unrelated = self.root / "unrelated.mp4"
        unrelated.write_bytes(b"old video")

        def wrong_path(settings, args, *, timeout):
            self.run_export(settings, args, timeout=timeout)
            return {"exported": True, "output": str(unrelated)}

        self.cli.side_effect = wrong_path
        with self.assertRaisesRegex(DesktopToolError, "预期的 MP4"):
            export_desktop_mp4(self.settings, self.draft_name)
        self.ffprobe.assert_not_called()
        self.assertEqual(unrelated.read_bytes(), b"old video")

    def test_rejects_wrong_fps_or_resolution_but_keeps_artifact(self):
        for attribute, value, message in (("frame_rate", 29.97, "帧率"), ("width", 720, "预期短边")):
            with self.subTest(attribute=attribute):
                original = self.media[attribute]
                self.media[attribute] = value
                with self.assertRaisesRegex(DesktopToolError, message) as caught:
                    export_desktop_mp4(self.settings, self.draft_name)
                output = Path(self.ffprobe.call_args.args[1])
                self.assertTrue(output.is_file())
                self.assertIn(str(output), str(caught.exception))
                self.media[attribute] = original

    def test_refuses_if_preflight_or_doctor_fails(self):
        self.probe.return_value = {"desktop_export_ready": False, "desktop_export_reason": "CLI draft root differs"}
        with self.assertRaisesRegex(DesktopToolError, "CLI draft root differs"):
            export_desktop_mp4(self.settings, self.draft_name)
        self.doctor.assert_not_called()
        self.probe.return_value = {"desktop_export_ready": True}
        self.doctor.return_value = {"ok": False, "errors": ["missing video", "bad ranges", "duplicate ID", "fourth"]}
        with self.assertRaisesRegex(DesktopToolError, "完整性检查未通过") as caught:
            export_desktop_mp4(self.settings, self.draft_name)
        self.assertIn("missing video", str(caught.exception))
        self.assertIn("duplicate ID", str(caught.exception))
        self.assertNotIn("fourth", str(caught.exception))
        self.cli.assert_not_called()

    def test_keeps_cli_error_and_job_directory(self):
        self.cli.side_effect = DesktopToolError("no usable Jianying window")
        with self.assertRaisesRegex(DesktopToolError, "no usable Jianying window") as caught:
            export_desktop_mp4(self.settings, self.draft_name)
        args = self.cli.call_args.args[1]
        self.assertIn(args[args.index("--output-dir") + 1], str(caught.exception))

    def test_covered_export_button_hands_user_the_existing_draft(self):
        self.cli.side_effect = DesktopToolError("export failed: button is covered by hwnd=123; refusing to click")
        result = export_or_handoff(self.settings, self.draft_name)
        self.assertEqual(result["status"], "manual_export_required")
        self.assertEqual(result["draft_path"], str(self.draft))
        self.assertIn(self.draft_name, result["user_message"])
        self.assertIn("其他窗口挡住", result["error"])
        self.assertNotIn("output_path", result)
        self.cli.assert_called_once()

    def test_other_detected_version_requires_explicit_opt_in(self):
        self.probe.return_value = {
            "desktop_export_ready": False, "desktop_export_prerequisites_ok": True,
            "installed_version": "11.8.0.14778", "desktop_version_baseline": "11.4.2.14459",
            "desktop_export_reason": "剪映版本不匹配",
        }
        with self.assertRaisesRegex(DesktopToolError, "版本不匹配"):
            export_desktop_mp4(self.settings, self.draft_name)
        self.doctor.assert_not_called()
        self.cli.assert_not_called()

        result = export_desktop_mp4(self.settings, self.draft_name, allow_unverified_version=True)
        self.assertEqual(result["installed_version"], "11.8.0.14778")
        self.assertEqual(result["desktop_version_baseline"], "11.4.2.14459")
        self.assertFalse(result["desktop_version_baseline_matched"])
        self.assertIn("11.8.0.14778", result["version_notice"])

    def test_version_override_requires_detected_app_and_aligned_existing_root(self):
        for installed_version, prerequisites_ok in ((None, False), (None, True), ("11.8.0.14778", False)):
            with self.subTest(installed_version=installed_version, prerequisites_ok=prerequisites_ok):
                self.probe.return_value = {
                    "desktop_export_ready": False, "desktop_export_prerequisites_ok": prerequisites_ok,
                    "installed_version": installed_version, "desktop_version_baseline": "11.4.2.14459",
                }
                with self.assertRaisesRegex(DesktopToolError, "前置检查未通过"):
                    export_desktop_mp4(self.settings, self.draft_name, allow_unverified_version=True)
        with self.assertRaisesRegex(DesktopToolError, "草稿目录不存在"):
            export_desktop_mp4(
                replace(self.settings, draft_root=self.root / "missing"),
                self.draft_name, allow_unverified_version=True,
            )
        self.doctor.assert_not_called()
        self.cli.assert_not_called()

    def test_refuses_paths_invalid_settings_and_missing_ffprobe(self):
        with self.assertRaisesRegex(DesktopToolError, "完整草稿目录名"):
            export_desktop_mp4(self.settings, str(self.draft))
        with self.assertRaisesRegex(DesktopToolError, "完全匹配"):
            export_desktop_mp4(self.settings, "有空格")
        for kwargs in ({"resolution": "480P"}, {"framerate": float("nan")}, {"timeout": 0}, {"timeout": 3601}):
            with self.subTest(kwargs=kwargs), self.assertRaises(DesktopToolError):
                export_desktop_mp4(self.settings, self.draft_name, **kwargs)
        with self.assertRaisesRegex(DesktopToolError, "ffprobe"):
            export_desktop_mp4(replace(self.settings, ffprobe_exe=None), self.draft_name)
        self.probe.assert_not_called()

    def test_rejects_concurrent_work_in_same_process(self):
        with _EXPORT_LOCK:
            with self.assertRaisesRegex(DesktopToolError, "正在运行"):
                export_desktop_mp4(self.settings, self.draft_name)
        self.cli.assert_not_called()
        self.assertTrue(export_desktop_mp4(self.settings, self.draft_name)["success"])

    def test_legacy_windows_dispatches_to_pinned_uia_worker(self):
        self.probe.return_value.update(platform="win32", installed_version="6.8.0", desktop_export_adapter="windows_uia")
        settings = replace(self.settings, jianying_tool_exe=self.root / "desktop/Scripts/jianying.exe")

        def run(interpreter, arguments, environment, *, timeout):
            self.assertEqual(interpreter, settings.jianying_tool_exe.with_name("python.exe"))
            self.assertEqual(Path(arguments[0]).name, "desktop_legacy.py")
            self.assertEqual([arguments[1], *arguments[3:]], [self.draft_name, "1080P", "30", "18"])
            Path(arguments[2]).write_bytes(b"new video")

        with patch(f"{MODULE}._capture", side_effect=run):
            result = export_desktop_mp4(settings, self.draft_name, timeout=18)
        self.assertTrue(result["success"])
        self.assertEqual(result["adapter"], "windows_uia")
        self.cli.assert_not_called()

    def test_legacy_worker_only_opens_target_from_home(self):
        sdk = Mock()
        controller = sdk.JianyingController.return_value
        worker = ROOT / "src/jianying_ai_mcp/desktop_legacy.py"
        args = [str(worker), self.draft_name, str(self.root / "native.mp4"), "1080P", "30", "18"]
        with patch.dict(sys.modules, {"pyJianYingDraft.jianying_controller": sdk}), patch.object(sys, "argv", args):
            controller.app_status = "edit"
            with self.assertRaisesRegex(RuntimeError, "首页"):
                runpy.run_path(str(worker), run_name="__main__")
            controller.export_draft.assert_not_called()
            controller.app.SetTopmost.assert_called_with(False)
            controller.app_status = "home"
            runpy.run_path(str(worker), run_name="__main__")
            controller.export_draft.assert_called_once_with(
                self.draft_name, args[2], resolution=sdk.ExportResolution.return_value,
                framerate=sdk.ExportFramerate.return_value, timeout=18.0,
            )
