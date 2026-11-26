from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context

from ..capabilities import capability_report
from ..config import Settings
from ..draft_crypto import crypto_status
from ..narration import narration_status
from ..managed_fonts import FONT_REVISION, font_path
from ..openfilm import openfilm_status
from ..openfilm_runtime import runtime_home
from ..video_local_asr import configured_model, local_asr_status
from ..video_observer import observer_status
from .common import run_tool


def _capabilities(settings: Settings) -> dict[str, Any]:
    from pathlib import Path
    model = Path(value) if (value := configured_model()) else None
    browser = runtime_home() / "browsers"
    fonts = [font_path(False), font_path(True)]
    return {
        "capabilities": capability_report(settings),
        "paths": {
            "draft_root": str(settings.draft_root),
            "work_root": str(settings.work_root),
            "jianying_exe": str(settings.jianying_exe) if settings.jianying_exe else None,
        },
        "video_observer": observer_status(settings),
        "narration": narration_status(),
        "encrypted_draft_reader": crypto_status(settings),
        "openfilm": openfilm_status(settings),
        "installation": {
            "stdio_config": str(runtime_home() / "mcp-stdio.json") if (runtime_home() / "mcp-stdio.json").is_file() else None,
            "ffmpeg": str(settings.ffmpeg_exe) if settings.ffmpeg_exe else None,
            "ffprobe": str(settings.ffprobe_exe) if settings.ffprobe_exe else None,
            "yt_dlp": str(settings.yt_dlp_exe) if settings.yt_dlp_exe else None,
            "local_asr": {**local_asr_status(), "weights_present": bool(model and all(
                (model / name).is_file() for name in ("model.bin", "tokenizer.json", "config.json")))},
            "managed_browser": str(browser) if (browser / "ready").is_file() else None,
            "font_revision": FONT_REVISION if all(path.is_file() for path in fonts) else None,
        },
        "jianying_desktop": {
            "configured": settings.jianying_tool_exe is not None,
            "executable": str(settings.jianying_tool_exe) if settings.jianying_tool_exe else None,
            "probe_tool": "probe_jianying_desktop",
        },
        "recommended_modes": ["creative", "strict", "reference"],
    }


async def get_jianying_capabilities(ctx: Context) -> dict[str, Any]:
    """检测本机剪映、媒体工具和可选 OpenFilm 运行时。"""
    return await run_tool(ctx, ["读取本机能力", "返回能力与版本"], _capabilities, threaded=True)


TOOLS = (get_jianying_capabilities,)
