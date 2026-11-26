import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.openfilm import (
    LOOK_TIMEOUT_SECONDS,
    RENDER_TIMEOUT_SECONDS,
    OpenFilmError,
    look_openfilm,
    openfilm_environment,
    openfilm_status,
    render_openfilm,
    run_openfilm_worker,
)


ROOT = Path(__file__).resolve().parents[1]


class OpenFilmTests(unittest.TestCase):
    def setUp(self):
        build = ROOT / ".build"
        build.mkdir(exist_ok=True)
        temp = self.enterContext(TemporaryDirectory(dir=build, prefix="openfilm-test-"))
        self.root = Path(temp).resolve()
        self.project = self.root / "Film With Spaces"
        self.project.mkdir()
        (self.project / "film.html").write_text("<section></section>", encoding="utf-8")
        self.executable = self.root / "node.exe"
        self.executable.touch()
        self.cli = self.root / "openfilm.mjs"
        self.cli.touch()
        self.settings = Settings(
            draft_root=self.root / "drafts", work_root=self.root / "work", jianying_exe=None,
            openfilm_exe=self.executable, openfilm_cli=self.cli,
        )

    def write_artifact(self, relative, content=b"media data"):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_look_reports_new_images_only(self):
        old = self.write_artifact(".film/cache/look/old.png")
        outside = self.write_artifact("assets/user.png")

        def run(settings, args, *, cwd, timeout):
            self.assertEqual(args, ["look", "0-0.0001", "--loudness=-14"])
            self.assertEqual(cwd, self.project)
            self.assertEqual(timeout, LOOK_TIMEOUT_SECONDS)
            self.write_artifact(".film/cache/look/图 像.png")
            return (
                "film 1280×720\n→ .film/cache/look/图 像.png\n"
                f"→ {old}\n→ {outside}\n→ .film/cache/look/missing.png",
                "drawing frames",
            )

        with patch("jianying_ai_mcp.openfilm._run", side_effect=run):
            result = look_openfilm(self.settings, str(self.project), start=0, end=1e-4, loudness=-14)
        self.assertEqual(result["artifact_paths"], [str(self.project / ".film/cache/look/图 像.png")])
        self.assertEqual(result["stderr"], "drawing frames")

    def test_look_does_not_pass_old_unwritten_image_as_success(self):
        old = self.write_artifact(".film/cache/look/old.png")
        with patch("jianying_ai_mcp.openfilm._run", return_value=(f"→ {old}", "")):
            with self.assertRaisesRegex(OpenFilmError, "本次新写入"):
                look_openfilm(self.settings, str(self.project))

    def test_full_look_checks_full_render_and_warns_for_old_film(self):
        full = self.write_artifact(".film/cache/render/jianying-full-001.mp4")
        os.utime(full, (1, 1))
        self.write_artifact(".film/cache/render/jianying-range-002.mp4")

        def run(settings, args, *, cwd, timeout):
            self.assertEqual(args, ["look", str(full), "--full", "1-2"])
            self.write_artifact(".film/cache/look/preview.jpg")
            return "→ .film/cache/look/preview.jpg", ""

        with patch("jianying_ai_mcp.openfilm._run", side_effect=run):
            result = look_openfilm(self.settings, str(self.project), start=1, end=2, full=True)
        self.assertEqual(result["inspected_video_path"], str(full))
        self.assertIn("先前", result["warning"])

    def test_full_look_requires_an_existing_full_video(self):
        self.write_artifact(".film/cache/render/jianying-range-002.mp4")
        with self.assertRaisesRegex(OpenFilmError, "渲染完整视频"):
            look_openfilm(self.settings, str(self.project), full=True)

    def test_render_has_distinct_managed_outputs_and_keeps_user_video(self):
        original = self.write_artifact(self.project.name + ".mp4", b"original")
        calls = []

        def run(settings, args, *, cwd, timeout):
            self.assertEqual(timeout, RENDER_TIMEOUT_SECONDS)
            self.assertEqual(cwd, self.project)
            calls.append(args[:-1])
            output = Path(args[-1])
            self.write_artifact(output.relative_to(self.project))
            return f"→ {output.relative_to(self.project)}\n2 frames at 30 fps", ""

        with patch("jianying_ai_mcp.openfilm._run", side_effect=run):
            full = render_openfilm(self.settings, str(self.project))
            part = render_openfilm(self.settings, str(self.project), start=1, end=3, fps=24)
        self.assertEqual(calls, [["render", "--out"], ["render", "1-3", "--fps=24", "--out"]])
        self.assertEqual(full["artifact_paths"], [full["output_path"]])
        self.assertTrue(Path(full["output_path"]).name.startswith("jianying-full-"))
        self.assertTrue(Path(part["output_path"]).name.startswith("jianying-range-"))
        self.assertEqual(original.read_bytes(), b"original")
        self.assertIn(str(Path(full["output_path"]).relative_to(self.project)), full["stdout"])

    def test_failed_render_does_not_publish_partial_mp4(self):
        def run(settings, args, *, cwd, timeout):
            staged = Path(args[-1]).relative_to(self.project)
            self.write_artifact(staged, b"partial")
            raise OpenFilmError("encode failed")

        with patch("jianying_ai_mcp.openfilm._run", side_effect=run):
            with self.assertRaisesRegex(OpenFilmError, "encode failed"):
                render_openfilm(self.settings, str(self.project))
        self.assertEqual(list((self.project / ".film/cache/render").glob("*.mp4")), [])

    def test_render_rejects_artifact_elsewhere(self):
        old = self.write_artifact(self.project.name + ".mp4")
        with patch("jianying_ai_mcp.openfilm._run", return_value=(f"→ {old}", "")):
            with self.assertRaisesRegex(OpenFilmError, "受控输出"):
                render_openfilm(self.settings, str(self.project))

    def test_validates_path_time_and_loudness_before_running(self):
        with patch("jianying_ai_mcp.openfilm._run") as run:
            for path in ("relative/project", "https://example.com/project", str(self.root)):
                with self.subTest(path=path), self.assertRaises(OpenFilmError):
                    look_openfilm(self.settings, path)
            for kwargs in ({"start": float("nan")}, {"start": -1}, {"end": 5}, {"start": 2, "end": 1},
                           {"start": 0, "end": float("inf")}, {"loudness": -71}, {"full": True, "loudness": -14}):
                with self.subTest(kwargs=kwargs), self.assertRaises(OpenFilmError):
                    look_openfilm(self.settings, str(self.project), **kwargs)
            with self.assertRaises(OpenFilmError):
                render_openfilm(self.settings, str(self.project), start=1)
        run.assert_not_called()

    def test_version_runs_configured_cli_with_no_shell(self):
        def popen(command, **kwargs):
            self.assertEqual(command, [str(self.executable), str(self.cli), "--version"])
            self.assertFalse(kwargs["shell"])
            kwargs["stdout"].write(b"0.4.1\n")
            return Mock(wait=Mock(return_value=0))

        with patch("jianying_ai_mcp.openfilm.subprocess.Popen", side_effect=popen):
            status = openfilm_status(self.settings)
        self.assertTrue(status["available"])
        self.assertTrue(status["module_available"])
        self.assertEqual(status["version"], "0.4.1")

    def test_environment_uses_work_root_unless_home_is_explicit(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(openfilm_environment(self.settings)["OPENFILM_HOME"], str(self.root / "work/openfilm"))
        specified = str(self.root / "existing-home")
        with patch.dict(os.environ, {"OPENFILM_HOME": specified}):
            self.assertEqual(openfilm_environment(self.settings)["OPENFILM_HOME"], specified)

    def test_environment_accepts_local_browser_channels(self):
        with patch.dict(os.environ, {"JIANYING_OPENFILM_BROWSER_CHANNEL": " Chrome "}):
            self.assertEqual(openfilm_environment(self.settings)["JIANYING_OPENFILM_BROWSER_CHANNEL"], "chrome")
        with patch.dict(os.environ, {"JIANYING_OPENFILM_BROWSER_CHANNEL": "invalid"}):
            with self.assertRaisesRegex(OpenFilmError, "JIANYING_OPENFILM_BROWSER_CHANNEL"):
                openfilm_environment(self.settings)

    def test_worker_passes_utf8_payload_via_stdin(self):
        def popen(command, **kwargs):
            self.assertEqual(command[2:], [str(self.cli)])
            self.assertEqual(kwargs["stdin"].read(), '{"标题":"测试"}'.encode("utf-8"))
            self.assertFalse(kwargs["shell"])
            kwargs["stdout"].write(b'{"success":true}\n')
            return Mock(wait=Mock(return_value=0))

        with patch("jianying_ai_mcp.openfilm.subprocess.Popen", side_effect=popen):
            stdout, stderr = run_openfilm_worker(
                self.settings, "openfilm_draft.mjs", [], cwd=self.project,
                timeout=30, input_text='{"标题":"测试"}',
            )
        self.assertEqual(stdout, '{"success":true}')
        self.assertEqual(stderr, "")

    def test_render_timeout_reports_limit_and_targets_only_started_process(self):
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("openfilm", RENDER_TIMEOUT_SECONDS)
        with patch("jianying_ai_mcp.openfilm.subprocess.Popen", return_value=process) as popen:
            with patch("jianying_ai_mcp.openfilm._stop_process") as stop:
                with self.assertRaisesRegex(OpenFilmError, str(RENDER_TIMEOUT_SECONDS)):
                    render_openfilm(self.settings, str(self.project))
        stop.assert_called_once_with(process)
        command = popen.call_args.args[0]
        self.assertEqual(command[0], str(self.executable))
        self.assertEqual(Path(command[1]).name, "openfilm_av.mjs")
        self.assertEqual(command[2:5], [str(self.cli), "render", "--out"])
