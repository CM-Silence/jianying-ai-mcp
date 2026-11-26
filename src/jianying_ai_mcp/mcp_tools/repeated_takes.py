from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context

from ..repeated_takes import find_repeated_takes as compare_takes
from .common import run_tool


async def find_repeated_takes(
    batch_id: str, ctx: Context, source_ids: list[str] | None = None,
    max_groups: int = 8, max_members: int = 4, min_similarity: float = 0.9,
) -> dict[str, Any]:
    """从批量分析缓存找文字相近的口播候选，返回时间码、短摘录、相似分和转写质量。无需重新分析媒体，不自动挑选。"""
    return await run_tool(
        ctx, ["读取批次对白", "比对相近口播"],
        lambda settings: compare_takes(
            settings, batch_id, source_ids=source_ids, max_groups=max_groups,
            max_members=max_members, min_similarity=min_similarity,
        ), threaded=True,
    )


TOOLS = (find_repeated_takes,)
