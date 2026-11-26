from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import Context

from ..desktop_export import export_or_handoff
from ..desktop_runtime import diagnose_desktop_draft, probe_desktop
from .common import run_tool


async def probe_jianying_desktop(ctx: Context) -> dict[str, Any]:
    """探测独立安装的 jianying-tool 0.5.0、本机剪映版本及草稿目录。"""
    return await run_tool(
        ctx, ["检查桌面后端配置", "返回版本和可用性"], probe_desktop, threaded=True,
    )


async def diagnose_jianying_draft(draft_path: str, ctx: Context) -> dict[str, Any]:
    """只读诊断本地草稿目录中的时间线、素材路径和引用。用 ok、errors 和 warnings 判断诊断结论。"""
    return await run_tool(
        ctx,
        ["检查本地草稿", "返回诊断结果"],
        lambda settings: diagnose_desktop_draft(settings, draft_path),
        threaded=True,
    )


async def export_jianying_mp4(
    draft_name: str,
    ctx: Context,
    resolution: Literal["720P", "1080P", "2K", "4K"] = "1080P",
    framerate: float = 30,
    timeout: int = 1800,
    allow_unverified_version: bool = False,
) -> dict[str, Any]:
    """按版本调用剪映原生导出并校验 MP4。发起时需要可交互窗口；若返回 manual_export_required，请将 user_message 和原因告知用户。"""
    return await run_tool(
        ctx,
        ["预检草稿并调用剪映导出", "返回导出结果或手动导出的草稿名"],
        lambda settings: export_or_handoff(
            settings,
            draft_name,
            resolution=resolution,
            framerate=framerate,
            timeout=timeout,
            allow_unverified_version=allow_unverified_version,
        ),
        threaded=True,
    )


TOOLS = (probe_jianying_desktop, diagnose_jianying_draft, export_jianying_mp4)
