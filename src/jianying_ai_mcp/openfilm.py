from __future__ import annotations

import math
import os
import re
import signal
import subprocess
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from . import openfilm_runtime
from .config import Settings


OPENFILM_CLI_BASELINE = openfilm_runtime.OPENFILM_VERSION
STATUS_TIMEOUT_SECONDS = 5
LOOK_TIMEOUT_SECONDS = 300
RENDER_TIMEOUT_SECONDS = 900
PREVIEW_RENDER_TIMEOUT_SECONDS = 3600
_LOG_BYTES = 32_768
_LOOK_SUFFIXES = {".png", ".jpg", ".jpeg"}
_VIDEO_SUFFIXES = (".mp4", ".mov", ".webm")


class OpenFilmError(ValueError):
    pass


def _command(settings: Settings) -> list[str]:
    executable = settings.openfilm_exe
    if not executable or not executable.is_file():
        raise OpenFilmError("未找到 OpenFilm 可执行文件。请设置 JIANYING_OPENFILM_EXE，或配置 Node.js 与 JIANYING_OPENFILM_CLI。")
    if executable.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise OpenFilmError("OpenFilm 需要可直接执行的程序。Windows npm 安装请配置 node.exe 与 bin/openfilm.mjs。")
    command = [str(executable.resolve())]
    cli = settings.openfilm_cli
    if cli is not None:
        if cli.suffix.lower() != ".mjs" or not cli.is_file():
            raise OpenFilmError("JIANYING_OPENFILM_CLI 必须指向已安装的 OpenFilm bin/openfilm.mjs。")
        command.append(str(cli.resolve()))
    return command


def resolve_openfilm_runtime(settings: Settings) -> tuple[Path, Path]:
    command = _command(settings)
    if len(command) != 2:
        raise OpenFilmError("请配置 Node.js 和 JIANYING_OPENFILM_CLI，以使用已安装的 OpenFilm 模块。")
    executable, cli = map(Path, command)
    return executable, cli


def resolve_openfilm_project(project_path: str) -> Path:
    requested = Path(project_path).expanduser()
    if not requested.is_absolute() or requested.drive.startswith("\\\\"):
        raise OpenFilmError("OpenFilm 项目必须使用本机绝对目录路径。")
    project = requested.resolve()
    if project.drive.startswith("\\\\"):
        raise OpenFilmError("OpenFilm 项目必须使用本机绝对目录路径。")
    film = project / "film.html"
    if not project.is_dir() or not film.is_file() or film.resolve().parent != project:
        raise OpenFilmError(f"OpenFilm 项目目录中没有本地 film.html: {project}")
    return project


def _cache_dir(project: Path, action: str) -> Path:
    area = project / ".film"
    cache = area / "cache"
    folder = cache / action
    for part in (area, cache, folder):
        if part.resolve() != part or (part.exists() and not part.is_dir()):
            raise OpenFilmError(f"OpenFilm 缓存目录不能是链接或普通文件: {part}")
    if folder.exists() and any(p.is_symlink() or p.is_junction() for p in folder.iterdir()):
        raise OpenFilmError(f"OpenFilm 缓存目录中存在文件链接: {folder}")
    return folder


def _decimal_arg(value: float) -> str:
    return format(Decimal(str(value)), "f") if value else "0"


def _time_args(start: float | None, end: float | None, *, allow_point: bool) -> list[str]:
    if start is None and end is None:
        return []
    if start is None or not math.isfinite(start) or start < 0:
        raise OpenFilmError("start 必须是有限且非负的秒数。")
    if end is None and allow_point:
        return [_decimal_arg(start)]
    if end is None or not math.isfinite(end) or end <= start:
        raise OpenFilmError("end 必须是有限秒数且大于 start。")
    return [f"{_decimal_arg(start)}-{_decimal_arg(end)}"]


def _loudness_args(loudness: float | None) -> list[str]:
    if loudness is None:
        return []
    if not math.isfinite(loudness) or not -70 <= loudness <= -5:
        raise OpenFilmError("loudness 必须处于 -70 到 -5 LUFS。")
    return [f"--loudness={_decimal_arg(loudness)}"]


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "nt":
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
        process.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _read_log(file: BinaryIO) -> str:
    size = file.seek(0, os.SEEK_END)
    file.seek(0)
    if size <= 2 * _LOG_BYTES:
        return file.read().decode("utf-8", errors="replace").strip()
    start = file.read(_LOG_BYTES)
    file.seek(-_LOG_BYTES, os.SEEK_END)
    return (start + b"\n...\n" + file.read()).decode("utf-8", errors="replace").strip()


def openfilm_environment(settings: Settings) -> dict[str, str]:
    env = os.environ.copy()
    browsers = openfilm_runtime.runtime_home() / "browsers"
    if (browsers / "ready").is_file():
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    browser_channel = _browser_channel()
    if browser_channel:
        env["JIANYING_OPENFILM_BROWSER_CHANNEL"] = browser_channel
    env["OPENFILM_NO_BROWSER"] = "1"
    env.setdefault("OPENFILM_HOME", str((settings.work_root / "openfilm").resolve()))
    ffmpeg_dirs = dict.fromkeys(str(path.parent) for path in (settings.ffmpeg_exe, settings.ffprobe_exe) if path)
    if ffmpeg_dirs:
        env["PATH"] = os.pathsep.join([*ffmpeg_dirs, env.get("PATH", "")])
    return env


def _browser_channel() -> str | None:
    channel = os.environ.get("JIANYING_OPENFILM_BROWSER_CHANNEL", "").strip().lower()
    if channel and channel not in {"chrome", "msedge", "chromium"}:
        raise OpenFilmError("JIANYING_OPENFILM_BROWSER_CHANNEL 必须为 chrome、msedge 或 chromium。")
    if channel:
        return channel
    # Fall back to an installed desktop browser so a fresh install never needs
    # to download a headless Chromium build just to preview a film.
    return openfilm_runtime.browser_channel()


def _run_process(
    settings: Settings, command: list[str], *, label: str, cwd: Path | None,
    timeout: int, input_text: str | None = None,
) -> tuple[str, str]:
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    # File-backed logs keep a timed-out child's inherited output handles from blocking the caller.
    with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        if input_text is not None:
            stdin.write(input_text.encode("utf-8"))
            stdin.seek(0)
        try:
            process = subprocess.Popen(
                command, cwd=cwd, env=openfilm_environment(settings),
                stdin=stdin if input_text is not None else subprocess.DEVNULL,
                stdout=stdout, stderr=stderr, shell=False, **options,
            )
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                _stop_process(process)
                raise OpenFilmError(f"OpenFilm {label} 超过 {timeout} 秒。") from exc
        except OSError as exc:
            raise OpenFilmError(f"无法启动 OpenFilm: {exc}") from exc
        out, err = _read_log(stdout), _read_log(stderr)
    if code != 0:
        detail = "\n".join(
            log if len(log) <= 1500 else f"{log[:500]}\n…\n{log[-1000:]}"
            for log in (out, err) if log
        )
        raise OpenFilmError(f"OpenFilm {label} 失败（退出码 {code}）\n{detail}".rstrip())
    return out, err


def run_openfilm_worker(
    settings: Settings, worker: str, args: list[str], *, cwd: Path,
    timeout: int, input_text: str | None = None,
) -> tuple[str, str]:
    executable, cli = resolve_openfilm_runtime(settings)
    script = Path(__file__).with_name(worker)
    label = args[0] if worker == "openfilm_av.mjs" else "可编辑工程导出"
    return _run_process(
        settings, [str(executable), str(script), str(cli), *args],
        label=label, cwd=cwd, timeout=timeout, input_text=input_text,
    )


def _run(settings: Settings, args: list[str], *, cwd: Path | None, timeout: int) -> tuple[str, str]:
    if cwd is None:
        return _run_process(settings, [*_command(settings), *args], label=args[0], cwd=None, timeout=timeout)
    return run_openfilm_worker(settings, "openfilm_av.mjs", args, cwd=cwd, timeout=timeout)


def openfilm_status(settings: Settings) -> dict[str, Any]:
    """Check the configured local CLI without installing or upgrading it."""
    status = {
        "available": False,
        "version": None,
        "baseline": OPENFILM_CLI_BASELINE,
        "module_available": False,
        "executable": str(settings.openfilm_exe) if settings.openfilm_exe else None,
        "cli_path": str(settings.openfilm_cli) if settings.openfilm_cli else None,
    }
    try:
        status["preview_render_browser_channel"] = _browser_channel() or "playwright-headless-shell"
        output, _ = _run(settings, ["--version"], cwd=None, timeout=STATUS_TIMEOUT_SECONDS)
    except OpenFilmError as exc:
        return {**status, "message": str(exc)}
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.+-]+)?", output):
        return {**status, "message": "已配置程序未返回可识别的 OpenFilm 版本号。"}
    return {**status, "available": True, "version": output, "module_available": settings.openfilm_cli is not None}


def _file_stamp(path: Path) -> tuple[int, int, int] | None:
    if not path.is_file() or path.is_symlink():
        return None
    info = path.stat()
    return info.st_mtime_ns, info.st_ctime_ns, info.st_size


def _reported_files(project: Path, output: str) -> list[Path]:
    files = []
    for line in output.splitlines():
        token = line.removeprefix("→ ").strip()
        if line.startswith("→ ") and token:
            try:
                path = project / token
                if not path.is_symlink():
                    files.append(path.resolve())
            except (OSError, RuntimeError, ValueError):
                continue
    return list(dict.fromkeys(files))


def _full_render(project: Path) -> Path:
    folder = _cache_dir(project, "render")
    choices = [*(project / (project.name + suffix) for suffix in _VIDEO_SUFFIXES), *folder.glob("jianying-full-*.mp4")]
    existing = [(stamp[0], p) for p in choices if (stamp := _file_stamp(p)) and stamp[2] > 0]
    if not existing:
        raise OpenFilmError("OpenFilm 全片检查需要先渲染完整视频。")
    return max(existing, key=lambda item: item[0])[1]


def look_openfilm(
    settings: Settings,
    project_path: str,
    *,
    start: float | None = None,
    end: float | None = None,
    full: bool = False,
    loudness: float | None = None,
) -> dict[str, Any]:
    """Run OpenFilm's inspection on a local film directory and return new images."""
    times = _time_args(start, end, allow_point=not full)
    if loudness is not None and (full or (start is not None and end is None)):
        raise OpenFilmError("loudness 只用于 OpenFilm 项目的全片或区间预览。")
    loudness_option = _loudness_args(loudness)
    project = resolve_openfilm_project(project_path)
    folder = _cache_dir(project, "look")
    video = _full_render(project) if full else None
    args = ["look", *([str(video), "--full"] if video else []), *times, *loudness_option]
    before = {path: _file_stamp(path) for path in folder.iterdir() if path.suffix.lower() in _LOOK_SUFFIXES} if folder.exists() else {}
    stdout, stderr = _run(settings, args, cwd=project, timeout=LOOK_TIMEOUT_SECONDS)
    new_files = [
        str(path) for path in _reported_files(project, stdout)
        if path.parent == folder and path.suffix.lower() in _LOOK_SUFFIXES
        and (stamp := _file_stamp(path)) and stamp[2] > 0 and stamp != before.get(path)
    ]
    if not new_files:
        raise OpenFilmError("OpenFilm 未报告本次新写入的检查图片。")
    result = {
        "success": True,
        "project_path": str(project),
        "artifact_paths": new_files,
        "stdout": stdout,
        "stderr": stderr,
    }
    if video is not None:
        result["inspected_video_path"] = str(video)
        if (project / "film.html").stat().st_mtime_ns > video.stat().st_mtime_ns:
            result["warning"] = "film.html 晚于这份渲染视频，检查结果对应的是先前的视频。"
    return result


def render_openfilm(
    settings: Settings,
    project_path: str,
    *,
    start: float | None = None,
    end: float | None = None,
    fps: float | None = None,
    preview_renderer: bool = False,
) -> dict[str, Any]:
    """Render a whole local film or a time range into a separate MP4 file."""
    times = _time_args(start, end, allow_point=False)
    if fps is not None and (type(fps) not in (int, float) or not math.isfinite(fps) or not 1 <= fps <= 120):
        raise OpenFilmError("fps 必须是 1 到 120 之间的数字。")
    project = resolve_openfilm_project(project_path)
    folder = _cache_dir(project, "render")
    label = "range" if times else "full"
    output = folder / f"jianying-{label}-{uuid4().hex}.mp4"
    staged = folder / f".{output.name}"
    if output.exists() or staged.exists():
        raise OpenFilmError("OpenFilm 渲染目标已存在，请重试生成新的输出路径。")
    rate = [f"--fps={_decimal_arg(fps)}"] if fps is not None else []
    args = ["render", *times, *rate, *(["--preview-renderer"] if preview_renderer else []), "--out", str(staged)]
    try:
        timeout = PREVIEW_RENDER_TIMEOUT_SECONDS if preview_renderer else RENDER_TIMEOUT_SECONDS
        stdout, stderr = _run(settings, args, cwd=project, timeout=timeout)
        stamp = _file_stamp(staged)
        if staged not in _reported_files(project, stdout) or not stamp or stamp[2] == 0:
            raise OpenFilmError("OpenFilm 未报告受控输出位置中的新渲染文件。")
        staged.rename(output)
    except BaseException:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    stdout = "\n".join(
        f"→ {output.relative_to(project)}" if _reported_files(project, line) == [staged] else line
        for line in stdout.splitlines()
    )
    return {
        "success": True,
        "project_path": str(project),
        "output_path": str(output),
        "renderer": "preview" if preview_renderer else "standard",
        "artifact_paths": [str(output)],
        "stdout": stdout,
        "stderr": stderr,
    }
