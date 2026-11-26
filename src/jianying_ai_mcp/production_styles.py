from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal


StylePreset = Literal["editorial", "documentary", "broadcast", "clean", "seller_reference"]
GraphicShape = Literal["rule", "dot", "block", "line"]
AnimationMotion = Literal["none", "slide_in", "photo_reveal", "count_up", "fade_in", "slide_right", "wipe_in"]
EDITORIAL_COLOR = "#172C24"
SELLER_HIGHLIGHT = (1.0, 245 / 255, 137 / 255)


def _background(color: str, alpha: float, radius: float) -> dict[str, Any]:
    return {"color": color, "alpha": alpha, "style": 1, "round_radius": radius}


@dataclass(frozen=True)
class ProductionStyle:
    label: str
    use: str
    palette: dict[str, str]
    graphic_shape: GraphicShape
    serif: bool
    animation_motion: AnimationMotion
    font: str
    caption_style: dict[str, Any]
    caption_position: float
    caption_decoration: dict[str, Any]
    caption_entrance: dict[str, Any] | None
    chapter_style: dict[str, Any]
    chapter_clip: dict[str, Any]
    chapter_background: dict[str, Any]
    chapter_seconds: float
    chapter_intro: str
    chapter_entrance: str
    chapter_intro_us: int
    transition: str
    portable_transition: str
    transition_color: str
    transition_half_seconds: float
    reference_dialogue: bool = False


STYLES: dict[StylePreset, ProductionStyle] = {
    "editorial": ProductionStyle(
        label="编辑感", use="知识口播、访谈和分段观点", serif=False, graphic_shape="rule", animation_motion="slide_in",
        palette={"background": "#F4F2E8", "panel": "#E8E7DC", "foreground": "#192825", "muted": "#455B53", "accent": "#E2F185", "accent_text": "#1D3025"},
        font="HarmonyOS_Sans_SC_Bold", caption_style={"color": [0.98, 0.98, 0.91], "max_line_width": 0.78}, caption_position=-0.62,
        caption_decoration={
            "background": _background(EDITORIAL_COLOR, 0.9, 0.25),
            "border": {"color": [0.03, 0.07, 0.04], "alpha": 0.72, "width": 10},
        },
        caption_entrance=None, chapter_style={"color": [0.9, 0.96, 0.64]}, chapter_clip={"transform_y": 0.68},
        chapter_background=_background(EDITORIAL_COLOR, 0.92, 0.23), chapter_seconds=2.4,
        chapter_intro="向上滑动", chapter_entrance="rise", chapter_intro_us=270_000,
        transition="闪黑", portable_transition="dip", transition_color=EDITORIAL_COLOR, transition_half_seconds=0.17,
    ),
    "documentary": ProductionStyle(
        label="暖调纪实", use="人物故事、历史讲解和纪实访谈", serif=True, graphic_shape="dot", animation_motion="fade_in",
        palette={"background": "#F0E5D2", "panel": "#E4D3B8", "foreground": "#34291E", "muted": "#69543F", "accent": "#BB733F", "accent_text": "#21170E"},
        font="思源中宋", caption_style={"color": [1, 0.93, 0.81], "size": 5.7, "bold": False, "max_line_width": 0.82}, caption_position=-0.68,
        caption_decoration={
            "background": _background("#34271D", 0.86, 0.08),
            "border": {"color": [0.18, 0.12, 0.08], "alpha": 0.85, "width": 12},
            "animations": [{"category": "text_intro", "name": "渐显", "duration": 180_000}],
        },
        caption_entrance={"kind": "fade", "duration_us": 180_000}, chapter_style={"color": [1, 0.9, 0.7], "bold": False, "size": 8.2},
        chapter_clip={"transform_y": 0.57}, chapter_background=_background("#34271D", 0.86, 0.08), chapter_seconds=2.8,
        chapter_intro="渐显", chapter_entrance="fade", chapter_intro_us=350_000,
        transition="叠化", portable_transition="dip", transition_color="#34271D", transition_half_seconds=0.23,
    ),
    "broadcast": ProductionStyle(
        label="新闻包装", use="资讯快报、数据盘点和解说", serif=False, graphic_shape="block", animation_motion="slide_right",
        palette={"background": "#112740", "panel": "#1C3859", "foreground": "#F0F7FF", "muted": "#BDD2E7", "accent": "#60E6E4", "accent_text": "#0B233B"},
        font="优设标题黑", caption_style={"color": [0.92, 0.98, 1], "size": 6.4, "max_line_width": 0.86}, caption_position=-0.72,
        caption_decoration={
            "background": _background("#12304F", 0.96, 0),
            "animations": [{"category": "text_intro", "name": "向右滑动", "duration": 160_000}],
        },
        caption_entrance={"kind": "slide_right", "duration_us": 160_000}, chapter_style={"color": [0.38, 0.90, 0.89], "size": 8.2, "align": 0, "max_line_width": 0.62},
        chapter_clip={"transform_y": 0.73, "transform_x": -0.24}, chapter_background=_background("#12304F", 0.97, 0), chapter_seconds=1.85,
        chapter_intro="向右滑动", chapter_entrance="slide_right", chapter_intro_us=190_000,
        transition="向右擦除", portable_transition="wipe_right", transition_color="#26CACC", transition_half_seconds=0.13,
    ),
    "clean": ProductionStyle(
        label="清爽极简", use="操作教程、产品演示和简洁信息流", serif=False, graphic_shape="line", animation_motion="wipe_in",
        palette={"background": "#F6F7FA", "panel": "#E8EDF6", "foreground": "#192544", "muted": "#465681", "accent": "#C9D8FA", "accent_text": "#192544"},
        font="仓耳舒圆体W02", caption_style={"color": [0.07, 0.12, 0.25], "bold": False, "size": 5.8, "max_line_width": 0.76}, caption_position=-0.63,
        caption_decoration={
            "background": _background("#F1F4FF", 0.94, 0.56),
            "animations": [{"category": "text_intro", "name": "向右擦除", "duration": 145_000}],
        },
        caption_entrance={"kind": "wipe_in", "duration_us": 145_000}, chapter_style={"color": [0.10, 0.15, 0.3], "size": 7.7},
        chapter_clip={"transform_y": 0.63}, chapter_background=_background("#E0E9FF", 0.96, 0.56), chapter_seconds=2.0,
        chapter_intro="向右擦除", chapter_entrance="wipe_in", chapter_intro_us=210_000,
        transition="向左擦除", portable_transition="wipe_left", transition_color="#B8CEF8", transition_half_seconds=0.12,
    ),
    "seller_reference": ProductionStyle(
        label="卖家案例口播", use="客户故事、前后对照、项目复盘", serif=True, graphic_shape="rule", animation_motion="slide_in",
        palette={"background": "#292822", "panel": "#3C3A32", "foreground": "#FFFCF1", "muted": "#D8D4C4", "accent": "#FFF589", "accent_text": "#29271F"},
        font="烟波宋", caption_style={"color": [1, 1, 1], "size": 12, "max_line_width": 0.90},
        caption_position=-0.435, caption_decoration={"shadow": {
            "color": [0, 0, 0], "alpha": 0.60, "diffuse": 2.5, "distance": 5, "angle": -45,
        }},
        caption_entrance=None, chapter_style={"color": [1, 1, 1]}, chapter_clip={"transform_y": 0.68},
        chapter_background=_background("#302E28", 0.82, 0.12), chapter_seconds=2.0,
        chapter_intro="向右缓入", chapter_entrance="slide_right", chapter_intro_us=266_667,
        transition="叠化", portable_transition="dip", transition_color="#25231E", transition_half_seconds=0.15,
        reference_dialogue=True,
    ),
}


def get_production_style(preset: StylePreset | None) -> ProductionStyle | None:
    if preset is None:
        return None
    try:
        return deepcopy(STYLES[preset])
    except (KeyError, TypeError) as exc:
        raise ValueError(f"style_preset 支持 {', '.join(STYLES)}") from exc


def list_video_style_presets() -> dict[str, Any]:
    return {"presets": [
        {
            "id": name, "label": style.label, "use": style.use,
            "graphic_shape": style.graphic_shape, "colors": dict(style.palette), "animation_motion": style.animation_motion,
            "portable_font_family": "serif" if style.serif else "sans-serif",
            "caption_font": style.font,
            "caption_intro": next(iter(style.caption_decoration.get("animations", [])), {}).get("name"),
            "portable_caption_motion": style.caption_entrance["kind"] if style.caption_entrance else None,
            "chapter_intro": style.chapter_intro, "portable_chapter_motion": style.chapter_entrance,
            "transition": style.transition, "portable_transition": style.portable_transition,
            **({"default_cut": "hard", "caption_size": style.caption_style["size"],
                "caption_y": style.caption_position, "caption_highlight": style.palette["accent"]}
               if style.reference_dialogue else {}),
        }
        for name, style in STYLES.items()
    ]}
