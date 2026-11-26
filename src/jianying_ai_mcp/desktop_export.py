from __future__ import annotations

import math
import os
import re
from contextlib import contextmanager
from pathlib import Path
from tempfile import gettempdir, mkdtemp
from threading import Lock
from typing import Any, Literal
from uuid import uuid4

from .config import Settings
from .desktop_runtime import (DesktopToolError, _capture, _environment, diagnose_desktop_draft,
                              export_adapter, probe_desktop, run_jianying_cli)
from .media_info import parse_media_info


DesktopExportResolution = Literal["720P", "1080P", "2K", "4K"]
_SHORT_EDGES = {"720P": 720, "1080P": 1080, "2K": 1440, "4K": 2160}
_FPS_LABELS = {24: "24", 25: "25", 29.97: "29.97", 30: "30", 50: "50", 59.94: "59.94", 60: "60", 120: "120"}
_FPS_TOLERANCE = 0.012
_EXPORT_LOCK = Lock()


@contextmanager
def _desktop_session_lock():
    if not _EXPORT_LOCK.acquire(blocking=False):
        raise DesktopToolError("已有一个剪映桌面导出任务正在运行。")
    try:
        # Independent MCP clients and the HTTP service share one interactive desktop.
        with (Path(gettempdir()) / "jianying-ai-mcp-export.lock").open("a+b") as gate:
            try:
                if os.name == "nt":
                    import msvcrt
                    gate.seek(0)
                    msvcrt.locking(gate.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise DesktopToolError("另一个 MCP 会话正在使用剪映导出窗口。") from exc
            yield
    finally:
        _EXPORT_LOCK.release()


def _local_draft(settings: Settings, name: str) -> Path:
    if not name.strip() or name in {".", ".."} or re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        raise DesktopToolError("draft_name 必须是剪映中的完整草稿目录名。")
    root = settings.draft_root.resolve()
    if not root.is_dir():
        raise DesktopToolError(f"剪映草稿目录不存在：{root}")
    path = next((entry for entry in root.iterdir() if entry.name == name), None)
    if path is None or not path.is_dir() or path.is_symlink() or path.resolve() != path:
        raise DesktopToolError(f"在剪映草稿目录中找不到名称完全匹配的本地草稿：{name}")
    return path


def _prepare_output(settings: Settings) -> tuple[str, Path]:
    destination = settings.work_root.resolve() / "desktop-exports"
    if destination.resolve() != destination:
        raise DesktopToolError(f"剪映导出目录不能是文件系统链接：{destination}")
    destination.mkdir(parents=True, exist_ok=True)
    job = Path(mkdtemp(prefix="job-", dir=destination)).resolve()
    title = f"render-{uuid4().hex}"
    return title, job / f"{title}.mp4"


def _verify_video(settings: Settings, output: Path, *, short_edge: int, framerate: float) -> dict[str, Any]:
    try:
        info = parse_media_info(settings, str(output))["media_info"]
    except (OSError, ValueError, KeyError) as exc:
        raise DesktopToolError(f"导出的 MP4 无法通过 ffprobe 检查，文件保留于 {output}。{exc}") from exc

    width, height = info.get("width"), info.get("height")
    duration, fps = info.get("duration"), info.get("frame_rate")
    format_names = (info.get("format") or "").split(",")
    if info.get("has_video") is not True or "mp4" not in format_names or not duration or not math.isfinite(duration) or duration <= 0:
        raise DesktopToolError(f"导出文件不是可验证的有效 MP4 视频，文件保留于 {output}。")
    if not width or not height or min(width, height) != short_edge:
        raise DesktopToolError(f"导出尺寸为 {width}×{height}，预期短边为 {short_edge} 像素。文件保留于 {output}。")
    if fps is None or not math.isclose(fps, framerate, rel_tol=0, abs_tol=_FPS_TOLERANCE):
        raise DesktopToolError(f"导出文件帧率为 {fps} fps，预期 {framerate} fps。文件保留于 {output}。")
    return info


def manual_export(draft: Path, reason: str) -> dict[str, Any]:
    detail = reason.strip().splitlines()[-1] if reason.strip() else "剪映未完成自动导出"
    if "no usable jianying window" in reason.lower() or "no window titled" in reason.lower():
        detail = "未找到可操作的剪映窗口"
    elif "is covered by hwnd=" in reason.lower():
        detail = "剪映的导出按钮被其他窗口遮挡"
    return {"success": False, "status": "manual_export_required", "draft_ready": True,
            "draft_name": draft.name, "draft_path": str(draft), "error": reason, "reason": detail,
            "user_message": f"需要手动导出（{detail}）。请在剪映首页打开草稿「{draft.name}」，点击右上角「导出」。草稿位置为 {draft}。"}


def export_or_handoff(settings: Settings, draft_name: str, **options) -> dict[str, Any]:
    draft = _local_draft(settings, draft_name)
    try:
        return export_desktop_mp4(settings, draft_name, **options)
    except (OSError, RuntimeError, ValueError) as exc:
        return manual_export(draft, str(exc))


def export_desktop_mp4(
    settings: Settings,
    draft_name: str,
    *,
    resolution: DesktopExportResolution = "1080P",
    framerate: float = 30,
    timeout: int = 1800,
    allow_unverified_version: bool = False,
) -> dict[str, Any]:
    """Export a local draft through Jianying Desktop and verify the resulting MP4."""
    if resolution not in _SHORT_EDGES:
        raise DesktopToolError("resolution 必须是 720P、1080P、2K 或 4K。")
    if framerate not in _FPS_LABELS:
        raise DesktopToolError("framerate 必须是 24、25、29.97、30、50、59.94、60 或 120。")
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise DesktopToolError("timeout 必须为 1 到 3600 的整数秒。")
    if settings.ffprobe_exe is None or not settings.ffprobe_exe.is_file():
        raise DesktopToolError("桌面导出需要 ffprobe 验证成片。请设置 JIANYING_FFPROBE 或将 ffprobe 加入 PATH。")
    with _desktop_session_lock():
        draft = _local_draft(settings, draft_name)
        status = probe_desktop(settings)
        installed_version = status.get("installed_version")
        baseline = status.get("desktop_version_baseline")
        baseline_matched = bool(installed_version and installed_version == baseline)
        ready = status.get("desktop_export_ready") is True
        adapter = status.get("desktop_export_adapter") or export_adapter(status.get("platform"), installed_version)
        use_unverified_version = (
            allow_unverified_version is True
            and status.get("desktop_export_prerequisites_ok") is True
            and isinstance(installed_version, str) and bool(installed_version.strip())
            and isinstance(baseline, str) and bool(baseline.strip())
            and installed_version != baseline
        )
        if not ready and not use_unverified_version:
            reason = status.get("desktop_export_reason") or f"检测到的剪映版本为 {installed_version or '未检测到'}。"
            raise DesktopToolError(f"桌面导出前置检查未通过。{reason}")
        diagnosis = diagnose_desktop_draft(settings, str(draft))
        if diagnosis.get("ok") is not True:
            errors = diagnosis.get("errors") or []
            detail = "\n".join(str(error)[:300] for error in errors[:3])
            raise DesktopToolError(f"剪映草稿完整性检查未通过，导出已停止。草稿路径 {draft}\n{detail}".rstrip())

        title, output = _prepare_output(settings)
        draft_arg = [f"--draft={draft_name}"] if draft_name.startswith("-") else ["--draft", draft_name]
        args = [
            "export", *draft_arg, "--title", title, "--output-dir", str(output.parent),
            "--resolution", resolution, "--format", "MP4", "--framerate", _FPS_LABELS[framerate],
            "--timeout", str(timeout),
        ]
        try:
            if adapter == "windows_uia":
                if framerate not in (24, 25, 30, 50, 60):
                    raise DesktopToolError(f"旧版 UIA 导出支持 24、25、30、50、60 fps，当前为 {framerate:g}")
                interpreter = settings.jianying_tool_exe.with_name("python.exe")
                _capture(interpreter, [str(Path(__file__).with_name("desktop_legacy.py")), draft_name, str(output),
                                       resolution, str(int(framerate)), str(timeout)], _environment(settings), timeout=timeout)
                result = {"exported": True, "output": str(output)}
            else:
                result = run_jianying_cli(settings, args, timeout=timeout)
        except DesktopToolError as exc:
            message = f"剪映桌面导出失败。任务目录 {output.parent}\n{exc}"
            if "is covered by hwnd=" in str(exc).lower():
                message += ("\n其他窗口挡住了剪映的导出按钮。需要剪映原生导出时，将剪映窗口置于最前再重试。"
                            f"也可以在剪映首页打开「{draft_name}」手动导出。")
            raise DesktopToolError(message) from exc
        reported = result.get("output")
        if (result.get("exported") is not True or not isinstance(reported, str) or Path(reported) != output
                or output.is_symlink() or output.resolve() != output or not output.is_file() or output.stat().st_size <= 0):
            raise DesktopToolError(f"剪映未返回本次任务预期的 MP4：{output}")
        media_info = _verify_video(settings, output, short_edge=_SHORT_EDGES[resolution], framerate=framerate)
        response = {
            "success": True,
            "draft_name": draft_name,
            "draft_path": str(draft),
            "output_path": str(output),
            "resolution": resolution,
            "framerate": framerate,
            "media_info": media_info,
            "installed_version": installed_version,
            "desktop_version_baseline": baseline,
            "desktop_version_baseline_matched": baseline_matched,
            "export_backend": "jianying", "adapter": adapter or "native_dialog",
        }
        if not baseline_matched:
            response["version_notice"] = f"本次产物已通过文件校验。所用剪映版本 {installed_version} 超出桌面导出基线 {baseline}。"
        if diagnosis.get("editor_audio_references"):
            response["editor_audio_references"] = diagnosis["editor_audio_references"]
            response["preflight_warnings"] = diagnosis["warnings"]
        return response
