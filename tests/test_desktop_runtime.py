from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.desktop_runtime import (
    DesktopToolError,
    _environment,
    diagnose_desktop_draft,
    probe_desktop,
    run_jianying_cli,
)


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = "jianying_ai_mcp.desktop_runtime"


class DesktopRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        build = ROOT / ".build"
        build.mkdir(exist_ok=True)
        workspace = tempfile.TemporaryDirectory(dir=build, prefix="desktop-runtime-test-")
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.drafts = self.root / "剪映草稿"
        self.drafts.mkdir()
        self.executable = self.root / "jianying.exe"
        self.executable.touch()
        self.settings = Settings(self.drafts, self.root / "work", None, jianying_tool_exe=self.executable)

    @staticmethod
    def _process(*, output: str = "", error: str = "", code: int = 0):
        def open_process(*args, stdout, stderr, **kwargs):
            stdout.write(output.encode("utf-8"))
            stderr.write(error.encode("utf-8"))
            return Mock(wait=Mock(return_value=code))

        return open_process

    def test_cli_enforces_version_and_reads_final_json_with_shared_environment(self) -> None:
        steps = iter([
            self._process(output="0.5.0\n"),
            self._process(output='启动日志\n{"intermediate": true}\n{\n  "ok": true, "text": "草稿完成"\n}\n'),
        ])
        with (
            patch.dict(os.environ, {
                "JIANYING_DRAFT_ROOT": "other-drafts",
                "JIANYING_INSTALL_ROOT": "configured-app-folder",
                "JY_INSTALL_DIR": "configured-codec-folder",
                "PYTHONPATH": "other-environment",
            }),
            patch(f"{RUNTIME}.subprocess.Popen", side_effect=lambda *args, **kwargs: next(steps)(*args, **kwargs)) as popen,
        ):
            report = run_jianying_cli(self.settings, ["doctor", "draft with spaces & arguments", "--json"], timeout=30)
            self.assertEqual(os.environ["JIANYING_DRAFT_ROOT"], "other-drafts")
        self.assertEqual(report, {"ok": True, "text": "草稿完成"})
        first, second = popen.call_args_list
        self.assertEqual(first.args[0], [str(self.executable), "--version"])
        self.assertEqual(second.args[0], [str(self.executable), "doctor", "draft with spaces & arguments", "--json"])
        self.assertFalse(second.kwargs["shell"])
        self.assertEqual(second.kwargs["env"]["JIANYING_DRAFT_ROOT"], str(self.drafts))
        self.assertEqual(second.kwargs["env"]["JIANYING_INSTALL_ROOT"], "configured-app-folder")
        self.assertEqual(second.kwargs["env"]["JY_INSTALL_DIR"], "configured-codec-folder")
        self.assertEqual(second.kwargs["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertNotIn("PYTHONPATH", second.kwargs["env"])

    def test_incompatible_cli_version_stops_before_command(self) -> None:
        with patch(f"{RUNTIME}.subprocess.Popen", side_effect=self._process(output="0.5.1\n")) as popen:
            with self.assertRaisesRegex(DesktopToolError, r"0\.5\.0.*0\.5\.1"):
                run_jianying_cli(self.settings, ["probe", "--json"], timeout=10)
        self.assertEqual(popen.call_count, 1)

    def test_windows_install_root_is_inferred_only_for_an_existing_versioned_jianying(self) -> None:
        application = self.root / "JianyingPro" / "11.8.0.14778" / "JianyingPro.exe"
        application.parent.mkdir(parents=True)
        application.touch()
        settings = replace(self.settings, jianying_exe=application)
        with patch.dict(os.environ, {"JIANYING_INSTALL_ROOT": "", "JY_INSTALL_DIR": "custom-codec"}):
            env = _environment(settings)
            self.assertEqual(env["JIANYING_INSTALL_ROOT"], str(application.parent.parent))
            self.assertEqual(env["JY_INSTALL_DIR"], "custom-codec")
            unversioned = self.root / "JianyingPro.exe"
            unversioned.touch()
            missing = application.parent.with_name("11.8.0.14779") / "JianyingPro.exe"
            for path in (unversioned, missing, self.executable):
                with self.subTest(path=path):
                    self.assertEqual(_environment(replace(settings, jianying_exe=path)).get("JIANYING_INSTALL_ROOT"), "")
        with patch.dict(os.environ, {"JIANYING_INSTALL_ROOT": "explicit-root"}):
            self.assertEqual(_environment(settings)["JIANYING_INSTALL_ROOT"], "explicit-root")

    def test_cli_failure_keeps_stderr_and_missing_json_is_rejected(self) -> None:
        for command_result, message in [
            (self._process(error="错误：草稿无法读取", code=2), "退出码 2.*\n错误：草稿无法读取"),
            (self._process(output="[]\n"), "stdout 末尾返回 JSON 对象"),
            (self._process(output='{"ok": true}\n后续输出\n'), "stdout 末尾返回 JSON 对象"),
        ]:
            with self.subTest(message=message):
                steps = iter([self._process(output="0.5.0\n"), command_result])
                with (
                    patch(f"{RUNTIME}.subprocess.Popen", side_effect=lambda *args, **kwargs: next(steps)(*args, **kwargs)),
                    self.assertRaisesRegex(DesktopToolError, message),
                ):
                    run_jianying_cli(self.settings, ["probe", "--json"], timeout=10)

    def test_cli_timeout_stops_the_spawned_process(self) -> None:
        slow = Mock(wait=Mock(side_effect=subprocess.TimeoutExpired("jianying", 2.5)))
        version_process = self._process(output="0.5.0\n")
        with (
            patch(f"{RUNTIME}.subprocess.Popen", side_effect=lambda command, **kwargs: version_process(command, **kwargs) if command[-1] == "--version" else slow),
            patch(f"{RUNTIME}._stop_process") as stop,
            self.assertRaisesRegex(DesktopToolError, "probe 超过 2.5 秒"),
        ):
            run_jianying_cli(self.settings, ["probe", "--json"], timeout=2.5)
        slow.wait.assert_called_once_with(timeout=2.5)
        stop.assert_called_once_with(slow)

    def test_probe_keeps_cli_availability_codec_and_export_readiness_separate(self) -> None:
        base = {"draft_root": str(self.drafts), "draft_root_exists": True, "codec_dir": None, "codec_error": "codec unavailable"}
        for fields, prerequisites, ready in [
            ({"platform": "win32", "installed_version": "11.4.2.14459"}, True, True),
            ({"platform": "darwin", "installed_version": "5.5.0"}, True, True),
            ({"platform": "win32", "installed_version": "11.8.0.14778"}, True, True),
            ({"platform": "win32", "installed_version": "6.0.0"}, True, True),
            ({"platform": "win32", "installed_version": "20.0.1"}, True, False),
            ({"platform": "win32", "installed_version": None}, False, False),
            ({"platform": "win32", "installed_version": "  "}, False, False),
            ({"platform": "linux", "installed_version": "5.5.0"}, False, False),
            ({"platform": "darwin", "installed_version": "5.5.0", "draft_root_exists": False}, False, False),
            ({"platform": "win32", "installed_version": "11.4.2.14459", "draft_root": str(self.root)}, False, False),
        ]:
            with self.subTest(fields=fields), patch(f"{RUNTIME}.run_jianying_cli", return_value={**base, **fields}):
                status = probe_desktop(self.settings)
                self.assertTrue(status["configured"])
                self.assertTrue(status["available"])
                self.assertEqual(status["cli_version"], "0.5.0")
                self.assertFalse(status["codec_available"])
                self.assertEqual(status["codec_error"], "codec unavailable")
                self.assertEqual(status["desktop_export_prerequisites_ok"], prerequisites)
                self.assertEqual(status["desktop_export_ready"], ready)
                self.assertFalse(status["desktop_export_ui_checked"])
                self.assertEqual(status["desktop_export_reason"] is None, ready)

    def test_windows_product_version_uses_paired_numeric_codec_build(self) -> None:
        for reported, folder, installed, ready in [
            ("11.4.2.f0a65bde0d6", "11.4.2.14459", "11.4.2.14459", True),
            ("11.8.0.d901ced05ea", "11.8.0.14778", "11.8.0.14778", True),
            ("11.8.0.d901ced05ea", "11.4.2.14459", "11.8.0.d901ced05ea", False),
            ("11.4.2.14458", "11.4.2.14459", "11.4.2.14458", True),
            ("11.4.2.f0a65bde0d6", "11.4.2.old", "11.4.2.f0a65bde0d6", False),
        ]:
            report = {
                "platform": "win32",
                "installed_version": reported,
                "codec_dir": rf"D:\application\JianyingPro\{folder}",
                "draft_root": str(self.drafts),
                "draft_root_exists": True,
            }
            with self.subTest(reported=reported, folder=folder), patch(f"{RUNTIME}.run_jianying_cli", return_value=report):
                status = probe_desktop(self.settings)
                self.assertEqual(status["reported_installed_version"], reported)
                self.assertEqual(status["installed_version"], installed)
                self.assertTrue(status["desktop_export_prerequisites_ok"])
                self.assertEqual(status["desktop_export_ready"], ready)

    def test_probe_with_no_executable_reports_unavailable(self) -> None:
        status = probe_desktop(replace(self.settings, jianying_tool_exe=None))
        self.assertFalse(status["configured"])
        self.assertFalse(status["available"])
        self.assertFalse(status["desktop_export_prerequisites_ok"])
        self.assertFalse(status["desktop_export_ready"])
        self.assertIn("JIANYING_TOOL_EXE", status["desktop_export_reason"])

    def test_probe_errors_clear_export_prerequisites(self) -> None:
        with patch(f"{RUNTIME}.run_jianying_cli", side_effect=DesktopToolError("probe failed")):
            status = probe_desktop(self.settings)
        self.assertTrue(status["configured"])
        self.assertFalse(status["available"])
        self.assertFalse(status["desktop_export_prerequisites_ok"])
        self.assertFalse(status["desktop_export_ready"])
        self.assertEqual(status["desktop_export_reason"], "probe failed")

    def test_doctor_returns_exit_one_findings_and_checks_absolute_directory(self) -> None:
        draft_path = self.drafts / "原生草稿"
        draft_path.mkdir()
        findings = {"ok": False, "errors": ["找不到时间线"], "warnings": []}
        steps = iter([self._process(output="0.5.0\n"), self._process(output=json.dumps(findings, ensure_ascii=False), code=1)])
        with patch(f"{RUNTIME}.subprocess.Popen", side_effect=lambda *args, **kwargs: next(steps)(*args, **kwargs)) as popen:
            self.assertEqual(diagnose_desktop_draft(self.settings, str(draft_path)), {**findings, "draft_path": str(draft_path)})
            for path in ("relative-draft", str(self.drafts / "missing"), str(self.executable)):
                with self.subTest(path=path), self.assertRaisesRegex(DesktopToolError, "草稿目录的绝对路径"):
                    diagnose_desktop_draft(self.settings, path)
        self.assertEqual(popen.call_args_list[1].args[0], [str(self.executable), "doctor", str(draft_path), "--json"])
        self.assertEqual(popen.call_count, 2)

    def test_doctor_reclassifies_only_verified_editor_music_path(self) -> None:
        draft_path = self.drafts / "原生音乐"
        draft_path.mkdir()
        material = {
            "id": "draft-audio-id", "type": "music", "music_id": "library-id", "duration": 2_000_000,
            "path": "<cloud://library-id>",
        }
        timeline = draft_path / "draft_content.json"
        original = json.dumps({"materials": {"audios": [material]}}).encode("utf-8")
        timeline.write_bytes(original)
        valid = "missing audio file for draft-audio-id: <cloud://library-id>"
        other = [
            "missing audio file for someone-else: <cloud://library-id>",
            "missing audio file for draft-audio-id: <cloud://wrong-id>",
            "missing audio file for another: audio.wav",
            "missing video file for draft-audio-id: <cloud://library-id>",
            "track 0 segment 0 has non-positive timing",
        ]
        for remaining in ([], other):
            with self.subTest(remaining=remaining), patch(f"{RUNTIME}.run_jianying_cli", return_value={
                "ok": False, "readable": True, "errors": [valid, *remaining], "warnings": ["original warning"],
            }):
                report = diagnose_desktop_draft(self.settings, str(draft_path))
                self.assertEqual(report["ok"], not remaining)
                self.assertEqual(report["errors"], remaining)
                self.assertEqual(report["warnings"][0], "original warning")
                self.assertIn("播放尚未验证", report["warnings"][1])
                self.assertEqual(report["editor_audio_references"], [{
                    "material_id": "draft-audio-id", "library_id": "library-id", "path": "<cloud://library-id>",
                }])
        self.assertEqual(timeline.read_bytes(), original)

    def test_doctor_keeps_cloud_style_errors_when_material_identity_unverified(self) -> None:
        draft_path = self.drafts / "未验证"
        draft_path.mkdir()
        timeline = draft_path / "draft_content.json"
        valid = {
            "id": "draft-audio-id", "type": "music", "music_id": "library-id", "duration": 2_000_000,
            "path": "<cloud://library-id>",
        }
        find = {"ok": False, "readable": True, "errors": ["missing audio file for draft-audio-id: <cloud://library-id>"],
                "warnings": []}
        for materials in ([{**valid, "music_id": "wrong-id"}], [{**valid, "type": "sound"}], [valid, valid]):
            timeline.write_text(json.dumps({"materials": {"audios": materials}}), encoding="utf-8")
            with patch(f"{RUNTIME}.run_jianying_cli", return_value=find):
                self.assertEqual(diagnose_desktop_draft(self.settings, str(draft_path)), {**find, "draft_path": str(draft_path)})
        timeline.write_text("unreadable", encoding="utf-8")
        with patch(f"{RUNTIME}.run_jianying_cli", return_value=find):
            self.assertEqual(diagnose_desktop_draft(self.settings, str(draft_path)), {**find, "draft_path": str(draft_path)})
