from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import Context

from ..openfilm import look_openfilm as inspect_film, render_openfilm as render_film
from ..openfilm_draft import export_openfilm_draft as export_draft
from ..openfilm_runtime import describe_runtime, install_runtime
from .common import run_tool


async def setup_openfilm_runtime(ctx: Context, install: bool = False) -> dict[str, Any]:
    """查看工具目录内内置 OpenFilm 运行时的状态。install=true 时在本机缺失就自动安装。"""
    stages = ["检查内置 OpenFilm 运行时"]
    if install:
        stages.append("安装 OpenFilm 并确认浏览器通道")
    return await run_tool(
        ctx,
        stages,
        lambda _settings: install_runtime() if install else describe_runtime(),
        threaded=True,
    )


async def look_openfilm(
    project_path: str,
    ctx: Context,
    start: float | None = None,
    end: float | None = None,
    full: bool = False,
    loudness: float | None = None,
) -> dict[str, Any]:
    """预览本地 OpenFilm 项目，返回截图和检查输出。时间以秒计。"""
    return await run_tool(
        ctx,
        ["检查 OpenFilm 项目和运行时", "返回 OpenFilm 预览和检查结果"],
        lambda settings: inspect_film(settings, project_path, start=start, end=end, full=full, loudness=loudness),
        threaded=True,
    )


async def render_openfilm(
    project_path: str,
    ctx: Context,
    start: float | None = None,
    end: float | None = None,
    fps: float | None = None,
    preview_renderer: bool = True,
) -> dict[str, Any]:
    """按工程预览逐帧渲染全部或指定区间并返回 MP4。fps 默认 30。preview_renderer=false 可选用快速素材直读。"""
    return await run_tool(
        ctx,
        ["检查 OpenFilm 项目和运行时", "返回渲染的 MP4"],
        lambda settings: render_film(settings, project_path, start=start, end=end, fps=fps, preview_renderer=preview_renderer),
        threaded=True,
    )


async def export_openfilm_draft(
    project_path: str,
    ctx: Context,
    publish: bool = False,
    media: Literal["copy", "link"] = "copy",
    fps: float = 30,
) -> dict[str, Any]:
    """把 OpenFilm 时间线导出为可编辑剪映草稿。默认保存在 MCP 工作目录，publish=true 发布到剪映草稿根目录。"""
    return await run_tool(
        ctx,
        ["检查 OpenFilm 项目和运行时", "返回 OpenFilm 导出的剪映草稿"],
        lambda settings: export_draft(settings, project_path, publish=publish, media=media, fps=fps),
        threaded=True,
    )


TOOLS = (setup_openfilm_runtime, look_openfilm, render_openfilm, export_openfilm_draft)
