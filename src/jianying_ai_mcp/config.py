from __future__ import annotations

import os
import platform
import plistlib
import re
import shutil
import sys
import sysconfig
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path

from . import openfilm_runtime


TESTED_JIANYING_VERSION = "10.0.5.13816"
TESTED_DRAFT_LIBRARY_VERSION = "0.3.0"
MCP_CALL_TIMEOUT_SECONDS = 7200


@dataclass(frozen=True)
class Settings:
    draft_root: Path
    work_root: Path
    jianying_exe: Path | None
    ffmpeg_exe: Path | None = None
    ffprobe_exe: Path | None = None
    yt_dlp_exe: Path | None = None
    draft_crypto_exe: Path | None = None
    openfilm_exe: Path | None = None
    openfilm_cli: Path | None = None
    jianying_tool_exe: Path | None = None
    jianying_stock_root: Path | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        draft_root = Path(os.environ.get("JIANYING_DRAFT_ROOT") or default_draft_root()).expanduser().resolve()
        work_root = Path(
            os.environ.get("JIANYING_MCP_WORK_ROOT", str(Path.cwd() / ".jianying-mcp"))
        ).expanduser().resolve()
        raw_exe = os.environ.get("JIANYING_EXE")
        exe = Path(raw_exe).expanduser().resolve() if raw_exe else _find_jianying_exe()
        raw_openfilm_cli = os.environ.get("JIANYING_OPENFILM_CLI")
        if raw_openfilm_cli:
            openfilm_cli = Path(raw_openfilm_cli).expanduser().resolve()
            openfilm_exe = openfilm_runtime.find_node()
        else:
            builtin = openfilm_runtime.discover()
            openfilm_exe, openfilm_cli = builtin if builtin else (
                _find_media_tool("openfilm", "JIANYING_OPENFILM_EXE"), None,
            )
        if os.environ.get("JIANYING_OPENFILM_EXE"):
            openfilm_exe = _find_media_tool("node" if openfilm_cli else "openfilm", "JIANYING_OPENFILM_EXE")
        return cls(
            draft_root=draft_root,
            work_root=work_root,
            jianying_exe=exe,
            ffmpeg_exe=_find_media_tool("ffmpeg", "JIANYING_FFMPEG"),
            ffprobe_exe=_find_media_tool("ffprobe", "JIANYING_FFPROBE"),
            yt_dlp_exe=_find_media_tool("yt-dlp", "JIANYING_YT_DLP"),
            draft_crypto_exe=_find_draft_crypto(),
            openfilm_exe=openfilm_exe,
            openfilm_cli=openfilm_cli,
            jianying_tool_exe=_find_media_tool("jianying", "JIANYING_TOOL_EXE"),
            jianying_stock_root=Path(os.environ["JIANYING_STOCK_ROOT"]).expanduser().resolve()
            if os.environ.get("JIANYING_STOCK_ROOT") else None,
        )


def default_draft_root() -> Path:
    if sys.platform == "darwin":
        visible = Path.home() / "Movies" / "JianyingPro"
        sandboxed = Path.home() / "Library/Containers/com.lemon.lvpro/Data/Movies/JianyingPro"
        base = visible if visible.exists() or not sandboxed.exists() else sandboxed
    else:
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "JianyingPro"
    return base / "User Data/Projects/com.lveditor.draft"


def _find_jianying_exe() -> Path | None:
    if sys.platform == "darwin":
        names = ("VideoFusion-macOS.app", "JianyingPro.app", "剪映专业版.app")
        configured = os.environ.get("JIANYING_INSTALL_ROOT")
        apps = [Path(configured)] if configured else [root / name for root in (Path("/Applications"), Path.home() / "Applications") for name in names]
        return next((app for app in apps if (app / "Contents/Info.plist").is_file()), None)
    roots = [Path(os.environ.get("LOCALAPPDATA", "")) / "JianyingPro"]
    roots.extend(Path(os.environ[key]) for key in ("JIANYING_INSTALL_ROOT", "JY_INSTALL_DIR") if os.environ.get(key))
    if os.name == "nt":
        roots.extend(_registered_editor_roots())
    matches: list[Path] = []
    for root in dict.fromkeys(roots):
        if not root.is_absolute() or not root.is_dir():
            continue
        for pattern in ("*/JianyingPro.exe", "Apps/*/JianyingPro.exe"):
            matches.extend(root.glob(pattern))
        direct = root / "JianyingPro.exe"
        if direct.is_file():
            matches.append(direct)
    if not matches:
        return None
    return sorted(matches, key=lambda path: _version_tuple(path.parent.name), reverse=True)[0].resolve()


@lru_cache(maxsize=1)
def _registered_editor_roots() -> tuple[Path, ...]:
    import winreg

    roots = []
    uninstall = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, uninstall, access=winreg.KEY_READ | view) as entries:
                    for i in range(winreg.QueryInfoKey(entries)[0]):
                        try:
                            with winreg.OpenKey(entries, winreg.EnumKey(entries, i)) as app:
                                name = winreg.QueryValueEx(app, "DisplayName")[0]
                                if name not in {"剪映专业版", "JianyingPro", "Jianying Pro"}:
                                    continue
                                for field in ("InstallLocation", "DisplayIcon"):
                                    try:
                                        value = winreg.QueryValueEx(app, field)[0]
                                    except OSError:
                                        continue
                                    if isinstance(value, str) and value.strip():
                                        path = Path(os.path.expandvars(re.sub(r",-?\d+$", "", value.strip()).strip('"')))
                                        roots.append(path.parent if field == "DisplayIcon" else path)
                        except OSError:
                            continue
            except OSError:
                continue
    return tuple(dict.fromkeys(roots))


def _find_media_tool(name: str, env_name: str) -> Path | None:
    configured = os.environ.get(env_name)
    if configured:
        path = Path(configured).expanduser().resolve()
        return path if path.is_file() else None

    if name in {"ffmpeg", "ffprobe"}:
        try:
            from ffmpeg import FFMPEG_PATH, FFPROBE_PATH
        except ImportError:
            pass
        else:
            bundled = FFMPEG_PATH if name == "ffmpeg" else FFPROBE_PATH
            if bundled and bundled.is_file():
                return bundled
    if name == "jianying":
        managed = openfilm_runtime.runtime_home() / "desktop" / ("Scripts/jianying.exe" if os.name == "nt" else "bin/jianying")
        if managed.is_file():
            return managed
    found = shutil.which(name, path=sysconfig.get_path("scripts")) or shutil.which(name)
    if found:
        return Path(found).resolve()

    return None


def _find_draft_crypto() -> Path | None:
    configured = _find_media_tool("jy-draftc", "JIANYING_DRAFT_CRYPTO")
    if configured:
        return configured
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = [
        Path.cwd() / ".jianying-mcp" / "tools" / "jy-draftc.exe",
        local_app_data / "jianying-ai-mcp" / "bin" / "jy-draftc.exe",
    ]
    if os.name == "nt" and platform.machine().lower() in {"amd64", "x86_64"}:
        candidates.append(openfilm_runtime.SPEC / "win-x64/jy-draftc.exe")
    return next((path.resolve() for path in candidates if path.is_file()), None)


def _version_tuple(value: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", value)
    return tuple(int(num) for num in nums) if nums else (0,)


def detected_jianying_version(settings: Settings) -> str | None:
    if settings.jianying_exe is None:
        return None
    if sys.platform == "darwin":
        bundle = next((p for p in (settings.jianying_exe, *settings.jianying_exe.parents) if p.suffix == ".app"), None)
        if bundle:
            try:
                with (bundle / "Contents/Info.plist").open("rb") as handle:
                    return plistlib.load(handle).get("CFBundleShortVersionString")
            except (OSError, plistlib.InvalidFileException):
                return None
    folder_version = settings.jianying_exe.parent.name
    return folder_version if re.fullmatch(r"\d+(?:\.\d+)+", folder_version) else None


def compatibility_notice(settings: Settings) -> dict[str, object]:
    detected = detected_jianying_version(settings)
    matches = detected == TESTED_JIANYING_VERSION
    library_version = version("pyJianYingDraft")
    if detected is None:
        message = f"本机剪映版本未知，草稿生成的验证基线为 {TESTED_JIANYING_VERSION}。"
    elif matches:
        message = f"当前剪映 {detected} 与草稿生成的验证基线一致。"
    else:
        message = (
            f"当前剪映 {detected}，草稿生成的验证基线为 {TESTED_JIANYING_VERSION}。"
            "动画、在线资源、滤镜和草稿格式可能随版本变化。"
        )
    if library_version != TESTED_DRAFT_LIBRARY_VERSION:
        message += f" 当前草稿库 {library_version}，已验证基线为 {TESTED_DRAFT_LIBRARY_VERSION}。"
    return {
        "detected": detected,
        "tested": TESTED_JIANYING_VERSION,
        "matches_tested_version": matches,
        "draft_library_version": library_version,
        "tested_draft_library_version": TESTED_DRAFT_LIBRARY_VERSION,
        "message": message,
    }
