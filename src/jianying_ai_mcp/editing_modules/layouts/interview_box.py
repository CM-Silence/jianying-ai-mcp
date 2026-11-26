from __future__ import annotations

from typing import Any

from .. import EditingModule
from .common import InterviewLayout, LayoutDefinition, Rect, read_split_layout, split_parameters


ID = "interview_box"
TOP_FRACTION = .43
BACKGROUND = "#E9DFCC"
FRAME_COLOR = "#F4CE59"


def _rectangles(width: int, height: int, split: int) -> tuple[Rect, Rect]:
    lower_y = split + max(2, 2 * round(height * .077 / 2))
    lower_width = 2 * round(width * .74 / 2)
    bottom_padding = 2 * round(height * .062 / 2)
    return (0, 0, width, split), (
        (width - lower_width) // 2, lower_y, lower_width, height - lower_y - bottom_padding,
    )


def _resolve(raw: dict[str, Any], width: int, height: int, label: str) -> InterviewLayout:
    return read_split_layout(
        raw, width, height, label,
        default_top_fraction=TOP_FRACTION, default_background=BACKGROUND,
        rectangles=_rectangles, bottom_frame_color=FRAME_COLOR,
    )


LAYOUT = LayoutDefinition(
    module=EditingModule(
        id=ID,
        summary="补充画面铺满上栏，受访者留在下栏的居中描边框里。",
        parameters=split_parameters(ID, TOP_FRACTION, BACKGROUND),
        examples=({"shots": [{"layout": {"kind": ID, "top": {"source": "D:/素材/细节.jpg"}}}]},),
    ),
    resolve=_resolve,
)
