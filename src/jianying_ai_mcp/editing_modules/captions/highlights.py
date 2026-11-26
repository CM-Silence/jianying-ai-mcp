from __future__ import annotations

import re
from typing import Any

from ...production_graphics import ProductionError
from .. import EditingModule


MODULE = EditingModule(
    id="highlights",
    summary="把字幕原文中点名的词标成暖黄，可独立使用或搭配字幕主题。",
    parameters={
        "captions[].highlights": "非空词语数组，每项须出现在该条字幕原文中。重复出现的词会全部高亮。",
        "captions[]": "同一字幕条目需提供 text、start_frame 和 end_frame。",
    },
    examples=({
        "captions": [{"start_frame": 0, "end_frame": 30, "text": "先确认验收标准", "highlights": ["验收标准"]}],
    },),
)

COLOR = (1.0, .86, .30)


def highlight_ranges(terms: Any, text: str) -> list[tuple[int, int]]:
    if not isinstance(terms, list):
        raise ProductionError("字幕 highlights 必须是数组")
    ranges = []
    for term in terms:
        if not isinstance(term, str) or not term.strip():
            raise ProductionError("字幕 highlights 每项须为非空文字")
        matches = [(match.start(), match.end()) for match in re.finditer(re.escape(term), text)]
        if not matches:
            raise ProductionError(f"字幕 highlights 中的 {term!r} 不在该条字幕里")
        ranges.extend(matches)
    combined: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if combined and combined[-1][1] >= start:
            previous, previous_end = combined[-1]
            combined[-1] = previous, max(end, previous_end)
        else:
            combined.append((start, end))
    return combined
