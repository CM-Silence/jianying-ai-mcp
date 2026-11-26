from __future__ import annotations

from typing import Any

from .. import EditingModule
from .common import InterviewLayout, LayoutDefinition, Rect, read_split_layout, split_parameters


ID = "stacked"
TOP_FRACTION = .5
BACKGROUND = "#15191D"


def _rectangles(width: int, height: int, split: int) -> tuple[Rect, Rect]:
    lower_y = split + max(2, 2 * round(height * .003 / 2))
    return (0, 0, width, split), (0, lower_y, width, height - lower_y)


def _resolve(raw: dict[str, Any], width: int, height: int, label: str) -> InterviewLayout:
    return read_split_layout(
        raw, width, height, label,
        default_top_fraction=TOP_FRACTION, default_background=BACKGROUND, rectangles=_rectangles,
    )


LAYOUT = LayoutDefinition(
    module=EditingModule(
        id=ID,
        summary="上下两屏各铺满一栏，同时呈现补充画面与主镜头。",
        parameters=split_parameters(ID, TOP_FRACTION, BACKGROUND),
        examples=({"shots": [{"layout": {"kind": ID, "top": {"source": "D:/素材/实拍.mp4"}}}]},),
    ),
    resolve=_resolve,
)
