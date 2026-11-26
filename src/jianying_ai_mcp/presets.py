from __future__ import annotations

import re
from enum import Enum
from typing import Literal, TypedDict
from unicodedata import normalize

import pyJianYingDraft as draft
from pyJianYingDraft.metadata import AudioSceneEffectType, SpeechToSongType, ToneEffectType


PresetCategory = Literal[
    "transition",
    "filter",
    "video_effect",
    "mix_mode",
    "font",
    "video_intro",
    "video_outro",
    "video_group",
    "text_intro",
    "text_outro",
    "text_loop",
    "scene",
    "tone",
    "speech_to_song",
    "mask",
]

_ENUMS: dict[PresetCategory, type[Enum]] = {
    "transition": draft.TransitionType,
    "filter": draft.FilterType,
    "video_effect": draft.VideoSceneEffectType,
    "mix_mode": draft.MixModeType,
    "font": draft.FontType,
    "video_intro": draft.IntroType,
    "video_outro": draft.OutroType,
    "video_group": draft.GroupAnimationType,
    "text_intro": draft.TextIntro,
    "text_outro": draft.TextOutro,
    "text_loop": draft.TextLoopAnim,
    "scene": AudioSceneEffectType,
    "tone": ToneEffectType,
    "speech_to_song": SpeechToSongType,
    "mask": draft.MaskType,
}


class PresetParameter(TypedDict):
    name: str
    default_value: float
    min_value: float
    max_value: float


class Preset(TypedDict):
    name: str
    display_name: str
    is_vip: bool | None
    resource_id: str
    effect_id: str
    default_duration_us: int | None
    parameters: list[PresetParameter]


class PresetList(TypedDict):
    success: Literal[True]
    category: PresetCategory
    source: Literal["pyJianYingDraft"]
    experimental: bool
    notice: str
    total_matches: int
    truncated: bool
    presets: list[Preset]


def _search_key(value: str) -> str:
    return re.sub(r"[\s_-]+", "", normalize("NFKC", value).casefold())


def list_presets(
    category: PresetCategory,
    keyword: str | None = None,
    is_vip: bool | None = None,
    limit: int = 30,
) -> PresetList:
    """Search installed preset metadata by display name, enum name, or ID."""
    if not 1 <= limit <= 100:
        raise ValueError("limit 必须在 1 到 100 之间")
    enum_type = _ENUMS.get(category)
    if enum_type is None:
        raise ValueError(f"未知预设分类: {category!r}")

    query = _search_key(keyword or "")
    matches: list[tuple[int, Enum]] = []
    for member in enum_type:
        meta = member.value
        if is_vip is not None and getattr(meta, "is_vip", None) is not is_vip:
            continue
        rank = 0
        if query:
            fields = (
                member.name,
                getattr(meta, "name", ""),
                getattr(meta, "title", ""),
                meta.resource_id,
                meta.effect_id,
            )
            keys = [_search_key(field) for field in fields]
            if query not in keys:
                if any(key.startswith(query) for key in keys):
                    rank = 1
                elif any(query in key for key in keys):
                    rank = 2
                else:
                    continue
        matches.append((rank, member))
    matches.sort(key=lambda match: match[0])

    presets: list[Preset] = []
    for _, member in matches[:limit]:
        meta = member.value
        presets.append({
            "name": member.name,
            "display_name": getattr(meta, "name", None) or getattr(meta, "title", member.name),
            "is_vip": getattr(meta, "is_vip", None),
            "resource_id": meta.resource_id,
            "effect_id": meta.effect_id,
            "default_duration_us": getattr(meta, "default_duration", getattr(meta, "duration", None)),
            "parameters": [
                {
                    "name": parameter.name,
                    "default_value": parameter.default_value,
                    "min_value": parameter.min_value,
                    "max_value": parameter.max_value,
                }
                for parameter in getattr(meta, "params", ())
            ],
        })

    notice = "结果来自本机 pyJianYingDraft 静态目录。剪映内的资源可用性取决于版本、账号和资源加载状态。"
    if category == "mask":
        notice += "蒙版处于实验状态，请在目标剪映版本检查显示效果。"
    return {
        "success": True,
        "category": category,
        "source": "pyJianYingDraft",
        "experimental": category == "mask",
        "notice": notice,
        "total_matches": len(matches),
        "truncated": len(matches) > limit,
        "presets": presets,
    }
