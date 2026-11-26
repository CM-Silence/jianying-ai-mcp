from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context

from ..jianying_audio_assets import inspect_jianying_audio
from .common import run_tool


async def list_jianying_audio_assets(
    draft_path: str,
    ctx: Context,
    keyword: str | None = None,
    offset: int = 0,
    limit: int = 40,
) -> dict[str, Any]:
    """读取参考草稿里的配乐和音效名称、时长、素材 ID 与本地可用状态。"""
    return await run_tool(
        ctx,
        ["读取已选剪映草稿", "列出音乐与音效及本地可用状态"],
        lambda settings: inspect_jianying_audio(settings, draft_path, keyword=keyword, offset=offset, limit=limit),
        threaded=True,
    )


TOOLS = (list_jianying_audio_assets,)
