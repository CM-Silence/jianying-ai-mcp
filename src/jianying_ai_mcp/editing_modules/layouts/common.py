from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ...production_graphics import ProductionError
from .. import EditingModule


Rect = tuple[int, int, int, int]
Focus = tuple[float, float]
LAYOUT_PATH = "shots[].layout"
TOP_FOCUS = (.5, .5)
BOTTOM_FOCUS = (.5, .32)
MIN_TOP_FRACTION = .30
MAX_TOP_FRACTION = .65


@dataclass(frozen=True)
class InterviewLayout:
    kind: str
    top_source: dict[str, Any]
    top: Rect
    bottom: Rect
    top_focus: Focus
    bottom_focus: Focus
    background: str
    bottom_frame_color: str | None = None
    # Which panel the shot's own footage (and its audio) occupies. ``top.source``
    # always names the secondary picture, which takes the other panel.
    main: str = "bottom"
    # How the secondary picture fills its panel: "cover" crops to fill, "contain"
    # shows the whole frame on the layout background.
    top_fit: str = "cover"

    @property
    def main_rect(self) -> Rect:
        return self.top if self.main == "top" else self.bottom

    @property
    def secondary_rect(self) -> Rect:
        return self.bottom if self.main == "top" else self.top


@dataclass(frozen=True)
class LayoutDefinition:
    module: EditingModule
    resolve: Callable[[dict[str, Any], int, int, str], InterviewLayout | None]


def split_parameters(kind: str, top_fraction: float, background: str) -> dict[str, str]:
    return {
        f"{LAYOUT_PATH}.kind": f'设为 "{kind}"。分屏画布宽高须为偶数。',
        f"{LAYOUT_PATH}.top.source": "必填。副画面（静音图片、视频、动效卡或生成视频）的本地绝对路径。主镜头沿用 source 和音量。",
        f"{LAYOUT_PATH}.top.fit": "选填。副画面填充方式：cover 裁切铺满（默认，适合实拍），contain 完整显示。竖版图卡放进横向栏位请用与栏位同尺寸的素材或 contain。",
        f"{LAYOUT_PATH}.main": '选填。主镜头所在栏位，"bottom"（默认）或 "top"；副画面占另一栏。',
        f"{LAYOUT_PATH}.top.source_start_frame": "选填。上方视频的源起始帧，默认 0。",
        f"{LAYOUT_PATH}.top.source_frames": "选填。上方视频使用的源帧数，默认为当前镜头帧数。",
        f"{LAYOUT_PATH}.top.focus": f"选填。上方横纵裁切重心，每项取 0–1，默认 {list(TOP_FOCUS)}。",
        f"{LAYOUT_PATH}.bottom_focus": f"选填。下方横纵裁切重心，每项取 0–1，默认 {list(BOTTOM_FOCUS)}。",
        f"{LAYOUT_PATH}.top_fraction": f"选填。上方面板高度占比，取 {MIN_TOP_FRACTION:.2f}–{MAX_TOP_FRACTION:.2f}，默认 {top_fraction}。",
        f"{LAYOUT_PATH}.background": f"选填。底板颜色，格式 #RRGGBB，默认 {background}。",
        "shots[].clip.alpha": "选填。分屏镜头的 clip 仅支持 alpha。",
    }


def _focus(value: Any, label: str) -> Focus:
    if not isinstance(value, (tuple, list)) or len(value) != 2 or any(
        type(coordinate) not in (int, float) or not 0 <= coordinate <= 1 for coordinate in value
    ):
        raise ProductionError(f"{label} 必须是两个 0–1 的裁切重心坐标")
    return value[0], value[1]


def read_split_layout(
    raw: dict[str, Any], width: int, height: int, label: str, *,
    default_top_fraction: float, default_background: str,
    rectangles: Callable[[int, int, int], tuple[Rect, Rect]], bottom_frame_color: str | None = None,
) -> InterviewLayout:
    if set(raw) - {"kind", "top", "top_fraction", "bottom_focus", "background", "main"}:
        raise ProductionError(f"{label} 需要 kind，可指定 top、top_fraction、bottom_focus、background、main")
    main = raw.get("main", "bottom")
    if main not in ("bottom", "top"):
        raise ProductionError(f"{label}.main 须为 bottom 或 top")
    if width % 2 or height % 2:
        raise ProductionError(f"{label} 分屏画幅的宽高须为偶数")
    source = raw.get("top")
    if not isinstance(source, dict) or "source" not in source or set(source) - {
        "source", "source_start_frame", "source_frames", "focus", "fit",
    }:
        raise ProductionError(f"{label}.top 需要本地 source，可指定源帧区间、focus 及 fit")
    fit = source.get("fit", "cover")
    if fit not in ("cover", "contain"):
        raise ProductionError(f"{label}.top.fit 须为 cover 或 contain")
    fraction = raw.get("top_fraction", default_top_fraction)
    if type(fraction) not in (int, float) or not MIN_TOP_FRACTION <= fraction <= MAX_TOP_FRACTION:
        raise ProductionError(f"{label}.top_fraction 须为 {MIN_TOP_FRACTION:.2f}–{MAX_TOP_FRACTION:.2f}")
    background = raw.get("background", default_background)
    if not isinstance(background, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", background):
        raise ProductionError(f"{label}.background 须为 #RRGGBB")

    split = 2 * round(height * fraction / 2)
    top, bottom = rectangles(width, height, split)
    return InterviewLayout(
        raw["kind"], source, top, bottom,
        _focus(source.get("focus", TOP_FOCUS), f"{label}.top.focus"),
        _focus(raw.get("bottom_focus", BOTTOM_FOCUS), f"{label}.bottom_focus"), background,
        bottom_frame_color, main, fit,
    )
