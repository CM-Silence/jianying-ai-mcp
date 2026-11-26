from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from mcp.server.fastmcp import Context

from ..config import Settings, compatibility_notice


async def _stage(ctx: Context, current: int, total: int, title: str) -> None:
    await ctx.info(f"[阶段] {title}")
    await ctx.report_progress(current, total, title)


async def run_tool(
    ctx: Context,
    stages: Sequence[str],
    operation: Callable[[Settings], dict[str, Any]],
    *,
    threaded: bool = False,
) -> dict[str, Any]:
    settings = Settings.from_env()
    compatibility = compatibility_notice(settings)
    total = max(1, len(stages) - 1)
    await _stage(ctx, 0, total, stages[0])
    try:
        if threaded:
            result = await asyncio.to_thread(operation, settings)
        else:
            result = operation(settings)
    except Exception as exc:
        await ctx.error(f"[失败] {type(exc).__name__}: {exc}")
        return {
            "success": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "compatibility": compatibility,
        }
    for index, title in enumerate(stages[1:], start=1):
        await _stage(ctx, index, total, title)
    return {"success": True, **result, "compatibility": compatibility}
