from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from typing import Any

from .config import Settings
from .jianying_audio_assets import JianyingAudioAssetError, _audio_rows, _entry


JIANYING_CLI_VERSION = "0.5.0"
DESKTOP_VERSION_BASELINES = {"win32": "11.4.2.14459", "darwin": "5.5.0"}
VERSION_TIMEOUT_SECONDS = 10
PROBE_TIMEOUT_SECONDS = 20
DIAGNOSIS_TIMEOUT_SECONDS = 120
_EDITOR_MUSIC_ERROR = re.compile(r"missing audio file for ([^:\r\n]+): (<cloud://([A-Za-z0-9_-]+)>)")


class DesktopToolError(RuntimeError):
    pass


def _environment(settings: Settings) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["JIANYING_DRAFT_ROOT"] = str(settings.draft_root.resolve())
    executable = settings.jianying_exe
    if (
        not env.get("JIANYING_INSTALL_ROOT")
        and executable is not None
        and executable.is_absolute()
        and executable.name.lower() == "jianyingpro.exe"
        and re.fullmatch(r"[0-9]+(?:\.[0-9]+){2,3}", executable.parent.name)
        and executable.is_file()
    ):
        # The CLI searches */JianyingPro.exe below its Windows install root.
        env["JIANYING_INSTALL_ROOT"] = str(executable.parent.parent.resolve())
    return env


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "nt":
            # A Windows Python entry point can launch a child interpreter.
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
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _capture(
    executable: Path,
    args: list[str],
    environment: dict[str, str],
    *,
    timeout: float,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> str:
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    # Log files let a timeout return even if a descendant still has its stdout open.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                [str(executable), *args], env=environment, shell=False,
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, **options,
            )
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                _stop_process(process)
                raise DesktopToolError(f"剪映 CLI {args[0]} 超过 {timeout:g} 秒") from exc
        except OSError as exc:
            raise DesktopToolError(f"无法启动剪映 CLI: {exc}") from exc

        stdout.seek(0)
        output = stdout.read().decode("utf-8-sig", errors="replace")
        if code not in allowed_returncodes:
            stderr.seek(0)
            detail = stderr.read().decode("utf-8-sig", errors="replace").strip() or output.strip()
            raise DesktopToolError(f"剪映 CLI {args[0]} 失败（退出码 {code}）\n{detail[-2000:]}".rstrip())
        return output


def _final_json(output: str, command: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for candidate in re.finditer(r"(?m)^[ \t]*(\{)", output):
        try:
            value, end = decoder.raw_decode(output, candidate.start(1))
        except json.JSONDecodeError:
            continue
        if not output[end:].strip():
            return value
    raise DesktopToolError(f"剪映 CLI {command} 没有在 stdout 末尾返回 JSON 对象")


def run_jianying_cli(
    settings: Settings,
    args: list[str],
    *,
    timeout: float,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> dict[str, Any]:
    executable = settings.jianying_tool_exe
    if executable is None:
        raise DesktopToolError("未找到剪映 CLI，请设置 JIANYING_TOOL_EXE 指向独立环境中的 jianying")
    if not executable.is_absolute() or not executable.is_file() or executable.suffix.lower() in {".cmd", ".bat"}:
        raise DesktopToolError(f"JIANYING_TOOL_EXE 必须指向可直接运行的本地 jianying 文件: {executable}")
    if not args or not math.isfinite(timeout) or timeout <= 0:
        raise DesktopToolError("剪映 CLI 需要命令和有限的正数超时")

    environment = _environment(settings)
    version = _capture(executable, ["--version"], environment, timeout=min(timeout, VERSION_TIMEOUT_SECONDS)).strip()
    if version != JIANYING_CLI_VERSION:
        raise DesktopToolError(f"需要 jianying-tool {JIANYING_CLI_VERSION}，当前版本输出为 {version[:100]!r}")
    output = _capture(executable, args, environment, timeout=timeout, allowed_returncodes=allowed_returncodes)
    return _final_json(output, args[0])


def _desktop_version(platform: Any, reported: Any, codec_dir: Any) -> str | None:
    if not isinstance(reported, str) or not reported.strip():
        return None
    version = reported.strip()
    if platform != "win32" or not isinstance(codec_dir, str):
        return version

    # The Windows ProductVersion can show a commit hash while the codec folder names the build.
    build = PureWindowsPath(codec_dir).name
    release, _, suffix = version.rpartition(".")
    if (
        re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", build)
        and release == build.rpartition(".")[0]
        and suffix
        and not suffix.isdecimal()
    ):
        return build
    return version


def export_adapter(platform: str | None, version: str | None) -> str | None:
    if not version or not re.fullmatch(r"\d+(?:\.\d+){1,3}", version):
        return None
    major = int(version.split(".")[0])
    if platform == "win32":
        if 1 <= major <= 6:
            return "windows_uia"
        if 7 <= major <= 11:
            return "windows_visual"
    if platform == "darwin" and major == 5:
        return "macos_accessibility"
    return None


def probe_desktop(settings: Settings) -> dict[str, Any]:
    """Probe the local CLI and version baseline without interacting with the editor."""
    status: dict[str, Any] = {
        "configured": settings.jianying_tool_exe is not None,
        "available": False,
        "cli_version": None,
        "expected_cli_version": JIANYING_CLI_VERSION,
        "platform": None,
        "install_root": None,
        "installed_version": None,
        "reported_installed_version": None,
        "desktop_version_baseline": None,
        "draft_root": None,
        "draft_root_aligned": False,
        "draft_root_exists": False,
        "codec_dir": None,
        "codec_error": None,
        "codec_available": False,
        "desktop_export_prerequisites_ok": False,
        "desktop_export_ready": False,
        "desktop_export_ui_checked": False,
        "desktop_export_adapter": None,
        "desktop_version_baseline_matched": False,
    }
    try:
        report = run_jianying_cli(settings, ["probe", "--json"], timeout=PROBE_TIMEOUT_SECONDS)
    except DesktopToolError as exc:
        return {**status, "desktop_export_reason": str(exc)}

    platform = report.get("platform")
    reported_root = report.get("draft_root")
    root = Path(reported_root) if isinstance(reported_root, str) and reported_root else None
    aligned = root is not None and root.is_absolute() and root.resolve() == settings.draft_root.resolve()
    exists = report.get("draft_root_exists") is True and root is not None and root.is_dir()
    expected = DESKTOP_VERSION_BASELINES.get(platform) if isinstance(platform, str) else None
    reported_version = report.get("installed_version")
    codec = report.get("codec_dir")
    codec_error = report.get("codec_error")
    installed = _desktop_version(platform, reported_version, codec)
    adapter = export_adapter(platform, installed)
    prerequisites_ok = expected is not None and installed is not None and aligned and exists
    if expected is None:
        reason = "剪映桌面导出支持 Windows 和 macOS"
    elif installed is None:
        reason = "剪映 CLI 未检测到已安装的剪映版本"
    elif not aligned:
        reason = "剪映 CLI 返回的草稿根目录与当前 MCP 配置不一致"
    elif not exists:
        reason = "剪映草稿根目录不存在"
    elif adapter is None:
        reason = f"剪映桌面导出的已知版本基线为 {expected}，当前探测为 {installed}"
    else:
        reason = None
    return {
        **status,
        "available": True,
        "cli_version": JIANYING_CLI_VERSION,
        "platform": platform,
        "install_root": report.get("install_root"),
        "installed_version": installed,
        "reported_installed_version": reported_version,
        "desktop_version_baseline": expected,
        "draft_root": reported_root,
        "draft_root_aligned": aligned,
        "draft_root_exists": exists,
        "codec_dir": codec,
        "codec_error": codec_error,
        "codec_available": bool(codec and not codec_error),
        "desktop_export_prerequisites_ok": prerequisites_ok,
        "desktop_export_ready": reason is None,
        "desktop_export_adapter": adapter,
        "desktop_version_baseline_matched": bool(installed and installed == expected),
        "desktop_export_ui_access": "启动需要可交互窗口，确认后后台等待文件" if adapter else None,
        "desktop_export_reason": reason,
    }


def _classify_editor_music(settings: Settings, path: Path, report: dict[str, Any]) -> dict[str, Any]:
    errors, warnings = report.get("errors"), report.get("warnings")
    if report["ok"] or report.get("readable") is not True or not isinstance(errors, list) or not isinstance(warnings, list):
        return report
    candidates = {
        error: match.groups() for error in errors
        if isinstance(error, str) and (match := _EDITOR_MUSIC_ERROR.fullmatch(error))
    }
    if not candidates:
        return report

    # Read the same timeline that jianying-tool 0.5.0 selects on this platform.
    names = ("draft_info.json", "draft_content.json") if sys.platform == "darwin" else ("draft_content.json", "draft_info.json")
    try:
        timeline = next((path / name for name in names if (path / name).is_file()), None)
        if timeline is None:
            return report
        file, _, materials = _audio_rows(settings, str(timeline))
    except (JianyingAudioAssetError, OSError):
        return report

    reclassified = {}
    for error, (material_id, marker, library_id) in candidates.items():
        matches = [material for material in materials if material.get("id") == material_id]
        if len(matches) != 1 or matches[0].get("path") != marker:
            continue
        entry = _entry(matches[0], file.parent)
        if entry and entry["kind"] == "music" and entry["availability"] == "editor_reference" and entry["library_id"] == library_id:
            reclassified[error] = {"material_id": material_id, "library_id": library_id, "path": marker}
    if not reclassified:
        return report

    remaining = [error for error in errors if not isinstance(error, str) or error not in reclassified]
    references = list(reclassified.values())
    return {
        **report, "ok": not remaining, "errors": remaining,
        "warnings": [*warnings, f"{len(references)} 项剪映原生音乐引用需由编辑器解析；素材可用性和播放尚未验证。"],
        "editor_audio_references": references,
    }


def diagnose_desktop_draft(settings: Settings, draft_path: str) -> dict[str, Any]:
    path = Path(draft_path).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise DesktopToolError("draft_path 必须是本机已存在的草稿目录的绝对路径")
    path = path.resolve()
    report = run_jianying_cli(
        settings, ["doctor", str(path), "--json"], timeout=DIAGNOSIS_TIMEOUT_SECONDS, allowed_returncodes=(0, 1),
    )
    if type(report.get("ok")) is not bool:
        raise DesktopToolError("剪映 CLI doctor 返回的 JSON 缺少有效的诊断状态")
    return {**_classify_editor_music(settings, path, report), "draft_path": str(path)}
