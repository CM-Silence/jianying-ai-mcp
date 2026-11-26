from __future__ import annotations

import json
import math
import re
import shutil
import sys
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Any

import pyJianYingDraft as draft
from pyJianYingDraft.animation import SegmentAnimations, Text_animation
from pyJianYingDraft.metadata import AudioSceneEffectType, SpeechToSongType, ToneEffectType

from .config import Settings, compatibility_notice, detected_jianying_version
from .draft_crypto import DraftCryptoError, load_draft_json
from .media_info import MediaProbeError, _probe_streams
from .editing_modules.captions.highlights import COLOR as HIGHLIGHT_COLOR
from .native_audio import NativeAudioMaterial, audio_material_from_item
from .broll_library import resolve_broll_shot
from .native_stock import NativeStockMaterial, write_stock_metadata
from .native_captions import (
    NativeCaptionError,
    add_native_caption_track,
    apply_caption_highlights,
    apply_text_highlight_ranges,
    draft_materials_by_id,
    remove_unregistered_sdk_speed_ref,
    sync_native_caption_text,
    validate_generated_material_refs,
    write_native_caption_metadata,
)


INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SUPPORTED_TRACKS = {"video", "audio", "text", "sticker", "effect", "filter"}
TIME_AMOUNT = r"(?:\d+(?:\.\d*)?|\.\d+)"
TIME_OFFSET = re.compile(rf"(?=.)(?:{TIME_AMOUNT}h)?(?:{TIME_AMOUNT}m)?(?:{TIME_AMOUNT}s)?")


class ManifestError(ValueError):
    pass


def _safe_name(value: str, label: str = "名称") -> str:
    value = value.strip()
    if not value or value in {".", ".."} or INVALID_NAME.search(value):
        raise ManifestError(f"{label}包含空值或 Windows 路径非法字符: {value!r}")
    return value


def _child(root: Path, name: str) -> Path:
    path = (root / _safe_name(name)).resolve()
    if path.parent != root.resolve():
        raise ManifestError(f"路径越界: {path}")
    return path


def _enum(enum_type: Any, name: str, label: str) -> Any:
    members = getattr(enum_type, "__members__", {})
    if name in members:
        return members[name]
    for member in members.values():
        meta = getattr(member, "value", None)
        if getattr(meta, "name", None) == name:
            return member
    sample = list(members)[:12]
    raise ManifestError(f"未知{label}: {name!r}；示例值: {sample}")


def _timerange(item: dict[str, Any], prefix: str = "") -> Any:
    start = item.get(f"{prefix}start", "0s")
    duration = item.get(f"{prefix}duration")
    if duration is None:
        raise ManifestError(f"片段缺少 {prefix}duration")
    return draft.trange(start, duration)


def _source_timerange(item: dict[str, Any]) -> Any | None:
    if "source_duration" not in item:
        return None
    return draft.trange(item.get("source_start", "0s"), item["source_duration"])


def _precise_media_duration(settings: Settings, source: Path, stream_type: str) -> int | None:
    if settings.ffprobe_exe is None:
        return None
    try:
        streams = _probe_streams(settings.ffprobe_exe, source, local_only=True).get("streams", [])
        stream = next(stream for stream in streams if stream.get("codec_type") == stream_type
                      and not (stream.get("disposition") or {}).get("attached_pic"))
        if stream.get("duration_ts") is not None and stream.get("time_base"):
            return round(int(stream["duration_ts"]) * Fraction(stream["time_base"]) * 1_000_000)
        if stream.get("duration") is not None:
            return round(Fraction(stream["duration"]) * 1_000_000)
    except (MediaProbeError, OSError, ValueError, TypeError, ZeroDivisionError, StopIteration):
        pass
    return None


def _fit_source_timerange(
    settings: Settings, source: Any, material: draft.VideoMaterial | draft.AudioMaterial, stream_type: str,
) -> Any:
    overflow = source.end - material.duration
    if 0 < overflow < 1_000 and source.start < material.duration:
        # MediaInfo can report a millisecond-quantized length for frame-accurate files.
        local_path = Path(material.path) if material.path and not material.path.startswith(("<cloud://", "cloud://")) else None
        precise = _precise_media_duration(settings, local_path, stream_type) if local_path else None
        if precise is not None and precise >= source.end:
            material.duration = precise
        elif precise is None or precise == source.end - 1:
            # Keep the trim within the known source, including a one-microsecond rounding difference.
            if precise is not None:
                material.duration = precise
            return draft.Timerange(source.start, material.duration - source.start)
    if source.end > material.duration:
        raise ManifestError(f"素材 {Path(material.path).name} 的源区间结束于 {source.end} 微秒，超出素材时长 {material.duration} 微秒")
    return source


def _media_timeranges(
    settings: Settings,
    item: dict[str, Any],
    material: draft.VideoMaterial | draft.AudioMaterial,
    stream_type: str,
) -> tuple[Any, Any, float | None]:
    target = _timerange(item)
    if target.start < 0 or target.duration <= 0:
        raise ManifestError("媒体片段的 start 必须非负且 duration 必须为正数")
    speed = item.get("speed")
    if speed is not None and (type(speed) not in (int, float) or not math.isfinite(speed) or speed <= 0):
        raise ManifestError("媒体片段 speed 必须为有限正数")

    source = _source_timerange(item)
    if source is None:
        duration = round(target.duration * (speed if speed is not None else 1.0))
        source = draft.trange(item.get("source_start", 0), duration)
    elif speed is not None:
        # Preserve the SDK's explicit source duration + speed behavior.
        target = draft.Timerange(target.start, round(source.duration / speed))

    if source.start < 0 or source.duration <= 0 or target.duration <= 0:
        raise ManifestError("媒体片段的源起点必须非负，源和目标时长必须为正数")
    fitted_source = _fit_source_timerange(settings, source, material, stream_type)
    if fitted_source.duration != source.duration:
        speed = None
    # The SDK derives speed from these spans when both are supplied. This preserves the exact target duration.
    return target, fitted_source, speed


def _clip(data: dict[str, Any] | None) -> Any | None:
    if not data:
        return None
    allowed = {
        "alpha",
        "flip_horizontal",
        "flip_vertical",
        "rotation",
        "scale_x",
        "scale_y",
        "transform_x",
        "transform_y",
    }
    unknown = set(data) - allowed
    if unknown:
        raise ManifestError(f"未知 clip 参数: {sorted(unknown)}")
    return draft.ClipSettings(**data)


def _rgb(value: Any, label: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ManifestError(f"{label}必须是三个 0~1 数值")
    values = tuple(float(v) for v in value)
    if any(not 0 <= v <= 1 for v in values):
        raise ManifestError(f"{label}必须是三个 0~1 数值")
    return values


def _text_style(data: dict[str, Any] | None) -> Any:
    data = dict(data or {})
    if "color" in data:
        data["color"] = _rgb(data["color"], "文本颜色")
    return draft.TextStyle(**data)


def _border(data: dict[str, Any] | None) -> Any | None:
    if not data:
        return None
    data = dict(data)
    if "color" in data:
        data["color"] = _rgb(data["color"], "描边颜色")
    return draft.TextBorder(**data)


def _shadow(data: dict[str, Any] | None) -> Any | None:
    if not data:
        return None
    data = dict(data)
    if "color" in data:
        data["color"] = _rgb(data["color"], "阴影颜色")
    return draft.TextShadow(**data)


def _require_experimental_effect(allowed: bool, feature: str) -> None:
    if not allowed:
        raise ManifestError(f"{feature} 需要设置 allow_experimental_effects=true")


def _effect_object(value: Any, label: str, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{label} 必须是对象")
    unknown = value.keys() - fields
    if unknown:
        raise ManifestError(f"未知 {label} 参数: {sorted(unknown)}")
    return value


def _effect_number(
    value: Any,
    label: str,
    *,
    minimum: float = -math.inf,
    maximum: float = math.inf,
) -> float:
    if type(value) not in (int, float):
        raise ManifestError(f"{label} 必须是有限数值")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ManifestError(f"{label} 必须是有限数值") from exc
    if not math.isfinite(number):
        raise ManifestError(f"{label} 必须是有限数值")
    if not minimum <= number <= maximum:
        raise ManifestError(f"{label} 超出允许范围")
    return number


def _add_mask(segment: Any, value: Any) -> None:
    ranges = {
        "center_x": (-math.inf, math.inf),
        "center_y": (-math.inf, math.inf),
        "rotation": (-math.inf, math.inf),
        "size": (0, 1),
        "feather": (0, 100),
        "rect_width": (0, 1),
        "round_corner": (0, 100),
    }
    mask = _effect_object(value, "mask", set(ranges) | {"name", "invert"})
    name = mask.get("name")
    if not isinstance(name, str) or not name:
        raise ManifestError("mask.name 必须是蒙版名称")
    mask_type = _enum(draft.MaskType, name, "蒙版")
    parameters = {
        field: _effect_number(mask[field], f"mask.{field}", minimum=low, maximum=high)
        for field, (low, high) in ranges.items()
        if field in mask
    }
    if parameters.get("size") == 0 or parameters.get("rect_width") == 0:
        raise ManifestError("mask.size 和 mask.rect_width 必须大于 0")
    if mask_type != draft.MaskType.矩形 and {"rect_width", "round_corner"}.intersection(parameters):
        raise ManifestError("mask.rect_width 和 mask.round_corner 只适用于矩形蒙版")
    invert = mask.get("invert", False)
    if not isinstance(invert, bool):
        raise ManifestError("mask.invert 必须是布尔值")
    segment.add_mask(mask_type, invert=invert, **parameters)


def _add_background_filling(segment: Any, value: Any) -> None:
    background = _effect_object(value, "background_filling", {"type", "blur", "color"})
    fill_type = background.get("type")
    if fill_type not in ("blur", "color"):
        raise ManifestError("background_filling.type 必须是 blur 或 color")
    blur = _effect_number(background.get("blur", 0.0625), "background_filling.blur", minimum=0, maximum=1)
    color = background.get("color", "#00000000" if fill_type == "blur" else None)
    if not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{8}", color):
        raise ManifestError("background_filling.color 必须是 #RRGGBBAA")
    segment.add_background_filling(fill_type, blur=blur, color=color)


def _volume_keyframe(
    keyframe: dict[str, Any],
    duration: int,
    used_times: set[int],
) -> tuple[int, float]:
    time = keyframe.get("time")
    if type(time) is int:
        offset = time
    elif isinstance(time, str) and TIME_OFFSET.fullmatch(time.strip().lower()):
        try:
            offset = draft.tim(time)
        except (ValueError, OverflowError) as exc:
            raise ManifestError("音量关键帧 time 无法转换为整数微秒") from exc
    else:
        raise ManifestError("音量关键帧 time 必须是整数微秒或带单位的时间字符串")
    if not 0 <= offset <= duration:
        raise ManifestError("音量关键帧 time 必须在片段起点和结束之间")
    if offset in used_times:
        raise ManifestError("同一片段的音量关键帧 time 不能重复")
    volume = _effect_number(keyframe.get("value"), "音量关键帧 value", minimum=0)
    used_times.add(offset)
    return offset, volume


def _add_visual_features(
    segment: Any,
    item: dict[str, Any],
    *,
    text: bool = False,
    allow_experimental_effects: bool = False,
) -> None:
    transition = item.get("transition")
    if transition:
        segment.add_transition(
            _enum(draft.TransitionType, transition["name"], "转场"),
            duration=transition.get("duration"),
        )

    filter_spec = item.get("filter")
    if filter_spec:
        segment.add_filter(
            _enum(draft.FilterType, filter_spec["name"], "滤镜"),
            intensity=float(filter_spec.get("intensity", 100)),
        )

    for effect in item.get("effects", []):
        segment.add_effect(
            _enum(draft.VideoSceneEffectType, effect["name"], "画面特效"),
            params=effect.get("params"),
        )

    if item.get("mix_mode"):
        segment.set_mix_mode(_enum(draft.MixModeType, item["mix_mode"], "混合模式"))

    if item.get("chroma"):
        chroma = item["chroma"]
        color = chroma["color"]
        if not re.fullmatch(r"#[0-9A-Fa-f]{8}", color):
            raise ManifestError("色度抠图颜色必须是 #RRGGBBAA")
        segment.add_chroma(
            color,
            intensity=float(chroma.get("intensity", 100)),
            edge_smooth=float(chroma.get("edge_smooth", 0)),
            spill=float(chroma.get("spill", 0)),
        )

    for animation in item.get("animations", []):
        category = animation["category"]
        enum_type = {
            "text_intro": draft.TextIntro,
            "text_outro": draft.TextOutro,
            "text_loop": draft.TextLoopAnim,
            "video_intro": draft.IntroType,
            "video_outro": draft.OutroType,
            "video_group": draft.GroupAnimationType,
        }.get(category)
        if enum_type is None or (text and not category.startswith("text_")) or (not text and category.startswith("text_")):
            raise ManifestError(f"动画分类与片段类型不匹配: {category}")
        segment.add_animation(
            _enum(enum_type, animation["name"], "动画"),
            duration=animation.get("duration"),
        )

    volume_times: set[int] = set()
    for keyframe in item.get("keyframes", []):
        prop = _enum(draft.KeyframeProperty, keyframe["property"], "关键帧属性")
        if prop == draft.KeyframeProperty.volume:
            if text:
                raise ManifestError("音量关键帧只适用于音频或视频片段")
            _require_experimental_effect(allow_experimental_effects, "音量关键帧")
            offset, volume = _volume_keyframe(keyframe, segment.duration, volume_times)
            segment.add_keyframe(prop, offset, volume)
        else:
            segment.add_keyframe(prop, keyframe["time"], float(keyframe["value"]))


def _video_segment(
    settings: Settings,
    item: dict[str, Any],
    *,
    allow_experimental_effects: bool,
    is_bottom_video_track: bool,
    stock_receipts: dict,
) -> Any:
    source = Path(item["source"]).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    stock = None
    if "stock_shot_id" in item:
        stock = resolve_broll_shot(settings, item["stock_shot_id"], with_receipt=True)
        if "receipt" not in stock or source != Path(stock["source"]):
            raise ManifestError("官方素材绑定已失效，请从素材库重新选择镜头")
        material = NativeStockMaterial(stock["receipt"], source, item.get("stock_crop", [0, 0, 1, 1]))
    else:
        material = draft.VideoMaterial(str(source))
    target_range, source_range, speed = _media_timeranges(settings, item, material, "video")
    if stock and (source_range.start < stock["start_ms"] * 1000 or source_range.end > stock["end_ms"] * 1000 + 1):
        raise ManifestError("官方素材源区间超出了入库选段")
    segment = draft.VideoSegment(
        material,
        target_range,
        source_timerange=source_range,
        volume=float(item.get("volume", 1.0)),
        change_pitch=bool(item.get("change_pitch", False)),
        clip_settings=_clip(item.get("clip")),
    )
    if speed is not None:
        segment.speed.speed = speed
    if stock:
        stock_receipts[segment.segment_id] = stock["receipt"]
    if item.get("mask") is not None:
        _require_experimental_effect(allow_experimental_effects, "mask")
        _add_mask(segment, item["mask"])
    if item.get("background_filling") is not None:
        _require_experimental_effect(allow_experimental_effects, "background_filling")
        if not is_bottom_video_track:
            raise ManifestError("background_filling 只适用于最底层视频轨道")
        _add_background_filling(segment, item["background_filling"])
    _add_visual_features(segment, item, allow_experimental_effects=allow_experimental_effects)
    return segment


def _audio_segment(
    settings: Settings, item: dict[str, Any], *, allow_experimental_effects: bool,
    native_materials: dict[tuple[str, str], NativeAudioMaterial],
) -> Any:
    keyframes = item.get("keyframes")
    if keyframes:
        _require_experimental_effect(allow_experimental_effects, "音量关键帧")
        if not isinstance(keyframes, list):
            raise ManifestError("音频 keyframes 必须是数组")
    try:
        material = audio_material_from_item(settings, item, native_materials)
    except ValueError as exc:
        raise ManifestError(str(exc)) from exc
    target_range, source_range, speed = _media_timeranges(settings, item, material, "audio")
    segment = draft.AudioSegment(
        material,
        target_range,
        source_timerange=source_range,
        volume=float(item.get("volume", 1.0)),
        change_pitch=bool(item.get("change_pitch", False)),
    )
    if speed is not None:
        segment.speed.speed = speed
    fade = item.get("fade")
    if fade:
        segment.add_fade(fade.get("in", "0s"), fade.get("out", "0s"))
    volume_times: set[int] = set()
    for keyframe in keyframes or []:
        if not isinstance(keyframe, dict) or keyframe.get("property") != "volume":
            raise ManifestError("音频 keyframes 只支持 property=volume")
        offset, volume = _volume_keyframe(keyframe, segment.duration, volume_times)
        segment.add_keyframe(offset, volume)
    enum_types = {
        "scene": AudioSceneEffectType,
        "tone": ToneEffectType,
        "speech_to_song": SpeechToSongType,
    }
    used: set[str] = set()
    for effect in item.get("effects", []):
        category = effect["category"]
        if category in used:
            raise ManifestError(f"每个音频片段只能使用一个 {category} 效果")
        enum_type = enum_types.get(category)
        if enum_type is None:
            raise ManifestError(f"未知音频效果分类: {category}")
        segment.add_effect(_enum(enum_type, effect["name"], "音频效果"), effect.get("params"))
        used.add(category)
    return segment


def _text_segment(item: dict[str, Any]) -> Any:
    font = _enum(draft.FontType, item["font"], "字体") if item.get("font") else None
    background = draft.TextBackground(**item["background"]) if item.get("background") else None
    segment = draft.TextSegment(
        str(item["text"]),
        _timerange(item),
        font=font,
        style=_text_style(item.get("style")),
        clip_settings=_clip(item.get("clip")),
        border=_border(item.get("border")),
        background=background,
        shadow=_shadow(item.get("shadow")),
    )
    remove_unregistered_sdk_speed_ref(segment)
    _add_visual_features(segment, item, text=True)
    if item.get("bubble"):
        segment.add_bubble(item["bubble"]["effect_id"], item["bubble"]["resource_id"])
    if item.get("flower_text_resource_id"):
        segment.add_effect(item["flower_text_resource_id"])
    return segment


_CAPTION_DECORATION_FIELDS = {"font", "bubble", "flower_text_resource_id", "animations", "border", "background", "shadow"}


def _caption_decoration(srt: dict[str, Any]) -> tuple[dict[str, Any], dict[str, tuple[Any, int]]]:
    supplied = srt.get("decoration")
    if supplied is None:
        return {}, {}
    decoration = _effect_object(supplied, "字幕 decoration", _CAPTION_DECORATION_FIELDS)
    bubble = decoration.get("bubble")
    if bubble is not None:
        bubble = _effect_object(bubble, "字幕气泡", {"effect_id", "resource_id"})
        for field in ("effect_id", "resource_id"):
            value = bubble.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ManifestError(f"字幕气泡必须使用参考草稿提供的 {field}")
    flower = decoration.get("flower_text_resource_id")
    if flower is not None and (not isinstance(flower, str) or not flower.strip()):
        raise ManifestError("字幕 flower_text_resource_id 必须使用参考草稿提供的资源 ID")
    font = decoration.get("font")
    if font is not None and (not isinstance(font, str) or not font.strip()):
        raise ManifestError("字幕 font 必须是内置字体预设名称")

    values = decoration.get("animations", [])
    if not isinstance(values, list):
        raise ManifestError("字幕 animations 必须是数组")
    categories = {"text_intro": draft.TextIntro, "text_outro": draft.TextOutro, "text_loop": draft.TextLoopAnim}
    plan = {}
    for value in values:
        value = _effect_object(value, "字幕动画", {"category", "name", "duration"})
        category, name = value.get("category"), value.get("name")
        if not isinstance(category, str) or category not in categories or not isinstance(name, str) or not name:
            raise ManifestError("字幕动画须指定 text_intro、text_outro 或 text_loop 分类及动画名称")
        if category in plan:
            raise ManifestError(f"每种字幕动画只能指定一次: {category}")
        animation = _enum(categories[category], name, "字幕动画")
        duration = value.get("duration")
        if category == "text_loop" and duration is not None:
            raise ManifestError("字幕循环动画自动填充剩余时间，不接受 duration")
        try:
            duration_us = draft.tim(duration) if duration is not None else animation.value.duration
        except (TypeError, ValueError, OverflowError) as exc:
            raise ManifestError("字幕动画 duration 必须是正微秒数或带单位的时间字符串") from exc
        if duration_us <= 0:
            raise ManifestError("字幕动画 duration 必须大于 0")
        plan[category] = (animation, duration_us)
    return {key: value for key, value in decoration.items() if key != "animations"}, plan


def _caption_animations(segment: draft.TextSegment, plan: dict[str, tuple[Any, int]]) -> None:
    if not plan:
        return
    duration = segment.target_timerange.duration
    _, intro = plan.get("text_intro", (None, 0))
    _, outro = plan.get("text_outro", (None, 0))
    if intro + outro > duration:
        # Short cues share their own time between both transitions. The SRT range stays exact.
        intro = round(Fraction(intro * duration, intro + outro))
        outro = duration - intro
    animation = SegmentAnimations()
    intervals = (
        ("text_intro", 0, intro),
        ("text_outro", duration - outro, outro),
        ("text_loop", intro, duration - intro - outro),
    )
    for category, start, length in intervals:
        if category in plan and length > 0:
            animation.add_animation(Text_animation(plan[category][0], start, length))
    if animation.animations:
        segment.animations_instance = animation
        segment.extra_material_refs.append(animation.animation_id)


def _srt_highlights(
    script: draft.ScriptFile,
    segments: list[draft.TextSegment],
    spec: dict[str, Any],
    *,
    strip_legacy_text: bool = False,
) -> None:
    if "highlights" not in spec:
        return
    color = spec.get("highlight_color", [1, 0.86, 0.30])
    if (not isinstance(color, (list, tuple)) or len(color) != 3
            or any(type(value) not in (int, float) or not 0 <= value <= 1 for value in color)):
        raise ManifestError("字幕 highlight_color 必须是三个 0~1 数值")
    try:
        apply_caption_highlights(
            script, segments, spec["highlights"], _rgb(color, "字幕 highlight_color"),
            strip_legacy_text=strip_legacy_text,
        )
    except NativeCaptionError as exc:
        raise ManifestError(str(exc)) from exc


def _caption_reference(
    parts: dict[str, Any], style: dict[str, Any] | None, clip: dict[str, Any], *, native: bool = False,
) -> draft.TextSegment:
    border = {"color": [0, 0, 0], "width": 40} if native else None
    return _text_segment({"text": "", "duration": 1, "style": style, "clip": clip, "border": border, **parts})


def _sticker_segment(item: dict[str, Any]) -> Any:
    segment = draft.StickerSegment(
        str(item["resource_id"]),
        _timerange(item),
        clip_settings=_clip(item.get("clip")),
    )
    remove_unregistered_sdk_speed_ref(segment)
    for keyframe in item.get("keyframes", []):
        prop = _enum(draft.KeyframeProperty, keyframe["property"], "贴纸关键帧属性")
        if prop == draft.KeyframeProperty.volume:
            raise ManifestError("音量关键帧只适用于音频或视频片段")
        segment.add_keyframe(prop, keyframe["time"], float(keyframe["value"]))
    return segment


def _validate_content(path: Path, *, generated: bool = False) -> dict[str, Any]:
    content = json.loads((path / "draft_content.json").read_text(encoding="utf-8"))
    tracks = content.get("tracks")
    if not isinstance(tracks, list):
        raise ManifestError("生成的 draft_content.json 缺少 tracks")
    if generated:
        try:
            validate_generated_material_refs(content)
        except NativeCaptionError as exc:
            raise ManifestError(str(exc)) from exc
    return {
        "duration_us": content.get("duration", 0),
        "tracks": len(tracks),
        "segments": sum(len(track.get("segments", [])) for track in tracks),
        "track_types": [track.get("type") for track in tracks],
    }


def repair_caption_references(
    settings: Settings,
    draft_path: str,
    reference_ids: list[str],
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Remove explicitly named missing refs from text clips in a standalone plaintext JSON copy."""
    source = Path(draft_path).expanduser()
    if not source.is_absolute() or source.drive.startswith("\\\\"):
        raise ManifestError("draft_path 必须是本地绝对路径")
    if source.is_dir():
        source /= "draft_content.json"
    source = source.resolve()
    if not source.is_file() or source.drive.startswith("\\\\") or source.suffix.lower() != ".json":
        raise ManifestError("draft_path 必须指向本地明文 JSON 草稿")
    if not reference_ids or any(not isinstance(ref, str) or not ref.strip() for ref in reference_ids):
        raise ManifestError("reference_ids 必须明确列出至少一个非空引用 ID")
    wanted = set(reference_ids)
    try:
        content = json.loads(source.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError("只支持明文 JSON 草稿，原始草稿未修改") from exc
    if not isinstance(content, dict) or not isinstance(content.get("tracks"), list) or not isinstance(content.get("materials"), dict):
        raise ManifestError("草稿缺少 tracks 或 materials")
    registered = wanted & draft_materials_by_id(content).keys()
    if registered:
        raise ManifestError(f"以下素材引用有效，不能移除: {', '.join(sorted(registered))}")
    text_ids = {material.get("id") for material in content["materials"].get("texts", []) if isinstance(material, dict)}
    patches = []
    found: set[str] = set()
    for track_index, track in enumerate(content["tracks"]):
        if track.get("type") != "text":
            continue
        for segment_index, segment in enumerate(track.get("segments", [])):
            refs = segment.get("extra_material_refs")
            if not isinstance(refs, list) or segment.get("material_id") not in text_ids:
                continue
            removed = [ref for ref in refs if isinstance(ref, str) and ref in wanted]
            if removed:
                found.update(removed)
                patches.append({"track_index": track_index, "segment_index": segment_index, "removed_ids": removed})
                segment["extra_material_refs"] = [ref for ref in refs if not isinstance(ref, str) or ref not in wanted]
    if wanted - found:
        raise ManifestError(f"以下 ID 不在可核实的文本片段引用中: {', '.join(sorted(wanted - found))}")
    output = None
    if not dry_run:
        folder = settings.work_root / "caption-repairs" / uuid.uuid4().hex
        folder.mkdir(parents=True)
        output = folder / "draft_content.json"
        output.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "success": True, "dry_run": dry_run, "source_path": str(source), "patches": patches,
        "removed_count": sum(len(patch["removed_ids"]) for patch in patches),
        "draft_content_path": str(output) if output else None,
    }


def _publish(staged: Path, target: Path) -> None:
    if target.exists():
        if (target / ".locked").exists():
            raise ManifestError(f"目标草稿正被剪映占用，拒绝覆盖: {target}")
        raise FileExistsError(f"目标草稿已存在，首版不自动覆盖: {target}")
    # Own the directory before copying so a racing publisher cannot remove another draft.
    target.mkdir()
    try:
        shutil.copytree(staged, target, dirs_exist_ok=True)
    except Exception:
        if target.exists() and not (target / ".locked").exists():
            shutil.rmtree(target)
        raise


def _set_published_identity(staged: Path, target: Path, duration_us: int) -> None:
    meta_path = staged / "draft_meta_info.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        from pyJianYingDraft import assets
        meta = json.loads(Path(assets.get_asset_path("DRAFT_META_TEMPLATE")).read_text(encoding="utf-8"))
    meta.update(draft_id=str(uuid.uuid4()).upper(), draft_name=target.name, draft_fold_path=target.as_posix(),
                draft_root_path=str(target.parent), tm_duration=duration_us)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _prepare_macos_timeline(staged: Path, version: str | None) -> None:
    path = staged / "draft_content.json"
    content = json.loads(path.read_text(encoding="utf-8"))
    for field in ("platform", "last_modified_platform"):
        marker = content.setdefault(field, {})
        marker.update(os="mac", app_id=3704, app_source="lv")
        if version:
            marker["app_version"] = version
    if version == "5.5.0":
        content.update(new_version="103.0.0", color_space=-1)
    path.write_text(json.dumps(content, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    shutil.copyfile(path, staged / "draft_info.json")


def build_manifest(
    settings: Settings,
    manifest: dict[str, Any],
    *,
    dry_run: bool = False,
    allow_experimental_effects: bool = False,
) -> dict[str, Any]:
    settings.draft_root.mkdir(parents=True, exist_ok=True)
    settings.work_root.mkdir(parents=True, exist_ok=True)
    draft_spec = manifest.get("draft") or {}
    name = _safe_name(str(draft_spec.get("name", "")), "草稿名称")
    width = int(draft_spec.get("width", 1920))
    height = int(draft_spec.get("height", 1080))
    fps = int(draft_spec.get("fps", 30))
    if width <= 0 or height <= 0 or not 1 <= fps <= 120:
        raise ManifestError("分辨率或帧率无效")
    tracks = manifest.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        raise ManifestError("manifest.tracks 必须是非空数组")

    stage_root = _child(settings.work_root, f"stage-{uuid.uuid4().hex}")
    stage_root.mkdir(parents=True)
    staged_draft = stage_root / name
    try:
        folder = draft.DraftFolder(str(stage_root))
        script = folder.create_draft(name, width, height, fps=fps)
        seen: set[tuple[str, str]] = set()
        specs = []
        for track in tracks:
            track_type = str(track.get("type", ""))
            track_name = _safe_name(str(track.get("name", "")), "轨道名称")
            if track_type not in SUPPORTED_TRACKS:
                raise ManifestError(f"不支持的轨道类型: {track_type}")
            key = (track_type, track_name)
            if key in seen:
                raise ManifestError(f"轨道类型和名称重复: {key}")
            seen.add(key)
            specs.append(draft.TrackSpec(_enum(draft.TrackType, track_type, "轨道类型"), track_name))
        script.append_tracks(specs)

        bottom_video_track = next((track for track in tracks if track["type"] == "video"), None)
        native_audio_materials: dict[tuple[str, str], NativeAudioMaterial] = {}
        stock_receipts = {}
        highlighted: list[tuple[Any, Any, tuple[float, float, float]]] = []
        for track in tracks:
            track_type = track["type"]
            track_name = track["name"]
            for item in track.get("segments", []):
                if track_type != "video" and (item.get("mask") is not None or item.get("background_filling") is not None):
                    raise ManifestError("mask 和 background_filling 只适用于视频片段")
                if track_type != "audio" and item.get("jianying_audio") is not None:
                    raise ManifestError("jianying_audio 只适用于音频片段")
                if track_type == "video":
                    segment = _video_segment(
                        settings,
                        item,
                        allow_experimental_effects=allow_experimental_effects,
                        is_bottom_video_track=track is bottom_video_track,
                        stock_receipts=stock_receipts,
                    )
                    script.add_segment(segment, track=track_name)
                elif track_type == "audio":
                    segment = _audio_segment(settings, item, allow_experimental_effects=allow_experimental_effects,
                                             native_materials=native_audio_materials)
                    script.add_segment(segment, track=track_name)
                elif track_type == "text":
                    segment = _text_segment(item)
                    script.add_segment(segment, track=track_name)
                    if item.get("highlight_ranges"):
                        color = _rgb(item.get("highlight_color", HIGHLIGHT_COLOR), "文字 highlight_color")
                        highlighted.append((segment, item["highlight_ranges"], color))
                elif track_type == "sticker":
                    segment = _sticker_segment(item)
                    script.add_segment(segment, track=track_name)
                elif track_type == "effect":
                    script.add_effect(
                        _enum(draft.VideoSceneEffectType, item["name"], "独立画面特效"),
                        _timerange(item),
                        track_name,
                        params=item.get("params"),
                    )
                elif track_type == "filter":
                    script.add_filter(
                        _enum(draft.FilterType, item["name"], "独立滤镜"),
                        _timerange(item),
                        track_name,
                        intensity=float(item.get("intensity", 100)),
                    )

        for segment, ranges, color in highlighted:
            apply_text_highlight_ranges(script, segment, ranges, color)

        native_imports = []
        for srt in manifest.get("srt_imports", []):
            mode = srt.get("mode", "text")
            parts, animation_plan = _caption_decoration(srt)
            if mode == "native":
                native_imports.append((srt, parts, animation_plan))
                continue
            if mode != "text":
                raise ManifestError("srt_imports.mode 必须是 text 或 native")
            source = Path(srt["source"]).expanduser().resolve()
            if not source.is_file():
                raise FileNotFoundError(source)
            srt_track_name = _safe_name(srt.get("track_name", "字幕"), "字幕轨名称")
            existing = script.tracks.get(srt_track_name)
            beginning = len(existing.segments) if existing is not None else 0
            clip_settings = {"transform_y": -0.8} if not srt.get("clip") else srt["clip"]
            reference = _caption_reference(parts, srt.get("style"), clip_settings) if parts else None
            script.import_srt(
                str(source),
                track_name=srt_track_name,
                time_offset=srt.get("time_offset", 0.0),
                style_reference=reference,
                text_style=_text_style(srt.get("style")),
                clip_settings=_clip(clip_settings),
            )
            imported = script.tracks[srt_track_name].segments[beginning:]
            _srt_highlights(script, imported, srt, strip_legacy_text=True)
            for segment in imported:
                remove_unregistered_sdk_speed_ref(segment)
                _caption_animations(segment, animation_plan)
                if segment.animations_instance is not None:
                    # The SDK registered the text, bubble and flower during import.
                    # It couldn't register the animation before the cue duration was known.
                    script.materials.animations.append(segment.animations_instance)

        native_tracks = []
        for srt, parts, animation_plan in native_imports:
            style = {"size": 6, "align": 1, "auto_wrapping": True, **(srt.get("style") or {})}
            clip = {"transform_y": -0.8, **(srt.get("clip") or {})}
            track_name = _safe_name(srt.get("track_name", "原生字幕"), "字幕轨名称")
            try:
                native_tracks.append(add_native_caption_track(
                    script,
                    source=srt.get("source"),
                    track_name=track_name,
                    time_offset=srt.get("time_offset", 0),
                    language=srt.get("language"),
                    style_reference=_caption_reference(parts, style, clip, native=True),
                    decorate=lambda segment: _caption_animations(segment, animation_plan),
                ))
            except NativeCaptionError as exc:
                raise ManifestError(str(exc)) from exc
            _srt_highlights(script, script.tracks[track_name].segments, srt)
        script.save()
        if stock_receipts:
            write_stock_metadata(staged_draft / "draft_content.json", stock_receipts)
        if native_tracks:
            write_native_caption_metadata(staged_draft / "draft_content.json", native_tracks)
        if sys.platform == "darwin":
            _prepare_macos_timeline(staged_draft, detected_jianying_version(settings))
        summary = _validate_content(staged_draft, generated=True)
        target = _child(settings.draft_root, name)
        _set_published_identity(staged_draft, target, summary["duration_us"])
        if not dry_run:
            _publish(staged_draft, target)
        return {
            "success": True,
            "dry_run": dry_run,
            "draft_name": name,
            "draft_path": None if dry_run else str(target),
            "summary": summary,
            "compatibility": compatibility_notice(settings),
            "stages": ["校验编辑清单", "构建暂存草稿", "校验草稿结构", "发布到剪映草稿目录" if not dry_run else "完成试运行"],
        }
    finally:
        if stage_root.exists():
            shutil.rmtree(stage_root)


def _draft_content(settings: Settings, draft_name: str) -> tuple[Path, dict[str, Any], str]:
    path = _child(settings.draft_root, draft_name)
    content_path = path / "draft_content.json"
    if (sys.platform == "darwin" or not content_path.is_file()) and (path / "draft_info.json").is_file():
        content_path = path / "draft_info.json"
    if not content_path.is_file():
        raise FileNotFoundError(content_path)
    try:
        loaded = load_draft_json(settings, content_path, label=f"参考草稿 {draft_name}")
    except DraftCryptoError as exc:
        raise ManifestError(str(exc)) from exc
    return path, loaded.content, loaded.source_format


def _reference_edit_details(segment: dict, track_type: str, by_id: dict, text_body: dict) -> dict:
    """Keep timing and styling in the editor's native units, excluding local resource paths."""
    result = {key: segment[key] for key in ("speed", "volume", "clip") if key in segment}
    source = segment.get("source_timerange")
    if source:
        result.update(source_start_us=source.get("start", 0), source_duration_us=source.get("duration", 0))
    material = by_id.get(segment.get("material_id"), {})
    if track_type != "text":
        name = material.get("material_name") or material.get("name")
        if name:
            result["material_name"] = name

    styles = []
    for style in text_body.get("styles") or []:
        run = {key: style[key] for key in ("range", "size", "bold") if key in style}
        font = (style.get("font") or {}).get("path")
        if font:
            run["font"] = font.replace("\\", "/").rsplit("/", 1)[-1]
        fill = (style.get("fill") or {}).get("content") or {}
        if "color" in (solid := fill.get("solid") or {}):
            run["color"] = solid["color"]
        if style.get("shadows"):
            run["shadow"] = True
        styles.append(run)
    if styles:
        result["text_styles"] = styles
    animations = []
    adjustments = {}
    for ref in segment.get("extra_material_refs") or []:
        extra = by_id.get(ref, {})
        kind = extra.get("type")
        if kind == "mask":
            result["mask"] = extra.get("name") or extra.get("resource_type")
        elif kind == "realtime_denoise":
            result["denoise"] = {k: extra[k] for k in ("is_denoise", "denoise_rate") if k in extra}
        elif kind in ("brightness", "contrast", "sharpen", "temperature", "saturation", "shadow", "clear", "tone"):
            if "value" in extra:
                adjustments[kind] = extra["value"]
        for animation in extra.get("animations") or []:
            animations.append({k: animation[k] for k in ("name", "type", "start", "duration") if k in animation})
    if adjustments:
        result["color_adjustments"] = adjustments
    if animations:
        result["animations"] = animations
    if segment.get("common_keyframes"):
        result["keyframes"] = [
            {"property": group.get("property_type"), "points": [
                {"time_us": point.get("time_offset", 0), "values": point.get("values", [])}
                for point in group.get("keyframe_list") or []
            ]} for group in segment["common_keyframes"]
        ]
    return result


def analyze_reference_draft(
    settings: Settings, draft_name: str, *, include_text: bool = False, include_edit_details: bool = False,
) -> dict[str, Any]:
    draft_name = _safe_name(draft_name, "参考草稿名称")
    path, content, source_format = _draft_content(settings, draft_name)
    materials = content.get("materials", {})
    by_id = draft_materials_by_id(content) if include_edit_details else {}
    text_by_id: dict[str, dict] = {}
    for material in materials.get("texts", []):
        try:
            body = json.loads(material.get("content", "{}"))
            if isinstance(body, dict):
                text_by_id[material.get("id", "")] = body
        except (TypeError, json.JSONDecodeError):
            continue
    tracks = []
    type_counts: dict[str, int] = {}
    for index, track in enumerate(content.get("tracks", [])):
        track_type = track.get("type", "")
        type_index = type_counts.get(track_type, 0)
        type_counts[track_type] = type_index + 1
        segments = []
        for seg_index, segment in enumerate(track.get("segments", [])):
            t = segment.get("target_timerange", {})
            item: dict[str, Any] = {
                "index": seg_index,
                "start_us": t.get("start", 0),
                "duration_us": t.get("duration", 0),
                "extra_ref_count": len(segment.get("extra_material_refs", [])),
            }
            body = text_by_id.get(segment.get("material_id"))
            if include_text and body is not None:
                item["text"] = body.get("text", "")
            if include_edit_details:
                item.update(_reference_edit_details(segment, track_type, by_id, body or {}))
            segments.append(item)
        tracks.append(
            {
                "index": index,
                "type_index": type_index,
                **({"attribute": track.get("attribute", 0)} if include_edit_details else {}),
                "type": track.get("type"),
                "name": track.get("name", ""),
                "segments": segments,
            }
        )
    profile = {
        "reference_draft": draft_name,
        "source_format": source_format,
        "duration_us": content.get("duration", 0),
        "canvas": {
            "width": content.get("canvas_config", {}).get("width"),
            "height": content.get("canvas_config", {}).get("height"),
            "fps": content.get("fps"),
        },
        "tracks": tracks,
        "material_counts": {key: len(value) for key, value in materials.items() if isinstance(value, list) and value},
        "learnable_dimensions": ["轨道布局", "片段时间与节奏", "字幕样式", "动画/滤镜/特效引用", "混合与关键帧结构"],
        "limitations": ["加密草稿只读取临时副本，不修改原文件", "在线资源仍受剪映版本、账号和缓存影响"],
    }
    profile_root = settings.work_root / "reference_profiles"
    profile_root.mkdir(parents=True, exist_ok=True)
    profile_path = _child(profile_root, f"{draft_name}.json")
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "success": True,
        "profile_path": str(profile_path),
        "profile": profile,
        "compatibility": compatibility_notice(settings),
    }


def build_from_reference(
    settings: Settings,
    reference_draft: str,
    new_draft_name: str,
    replacements: list[dict[str, Any]] | None = None,
    text_replacements: list[dict[str, Any]] | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    reference_draft = _safe_name(reference_draft, "参考草稿名称")
    new_draft_name = _safe_name(new_draft_name, "新草稿名称")
    source, content, source_format = _draft_content(settings, reference_draft)
    if (source / ".locked").exists():
        raise ManifestError("参考草稿正被剪映占用；请返回首页后再复制")
    settings.work_root.mkdir(parents=True, exist_ok=True)
    stage_root = _child(settings.work_root, f"reference-{uuid.uuid4().hex}")
    stage_root.mkdir(parents=True)
    staged = stage_root / new_draft_name
    try:
        shutil.copytree(source, staged)
        staged_content = staged / "draft_content.json"
        staged_info = staged / "draft_info.json"
        sync_info = (
            source_format == "plaintext_json"
            and staged_info.is_file()
            and (sys.platform == "darwin" or not staged_content.is_file() or staged_info.read_bytes() == staged_content.read_bytes())
        )
        staged_content.write_text(
            json.dumps(content, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        meta_path = source / "draft_meta_info.json"
        if meta_path.is_file():
            try:
                loaded_meta = load_draft_json(settings, meta_path, label=f"参考草稿 {reference_draft} 元数据")
                (staged / "draft_meta_info.json").write_text(
                    json.dumps(loaded_meta.content, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
            except DraftCryptoError:
                pass
        script = draft.DraftFolder(str(stage_root)).load_template(new_draft_name)
        for spec in replacements or []:
            track_type = spec.get("track_type", "video")
            if track_type not in {"video", "audio"}:
                raise ManifestError("素材替换只支持 video 或 audio")
            track = script.get_imported_track(
                _enum(draft.TrackType, track_type, "替换轨道类型"),
                name=spec.get("track_name"),
                index=spec.get("track_index"),
            )
            source_path = Path(spec["source"]).expanduser().resolve()
            if not source_path.is_file():
                raise FileNotFoundError(source_path)
            material = draft.VideoMaterial(str(source_path)) if track_type == "video" else draft.AudioMaterial(str(source_path))
            source_range = _source_timerange(spec)
            if source_range is not None:
                if source_range.start < 0 or source_range.duration <= 0:
                    raise ManifestError("替换素材的源起点必须非负，源时长必须为正数")
                source_range = _fit_source_timerange(settings, source_range, material, track_type)
            script.replace_material_by_seg(track, int(spec["segment_index"]), material, source_range)
        replaced_native_materials: set[str] = set()
        for spec in text_replacements or []:
            track = script.get_imported_track(
                draft.TrackType.text,
                name=spec.get("track_name"),
                index=spec.get("track_index"),
            )
            segment_index = int(spec["segment_index"])
            script.replace_text(
                track,
                segment_index,
                spec["text"],
                recalc_style=bool(spec.get("recalc_style", True)),
            )
            if track.raw_data.get("flag") == 1:
                replaced_native_materials.add(track.segments[segment_index].material_id)
        script.save()
        if replaced_native_materials:
            sync_native_caption_text(staged_content, replaced_native_materials)
        if sync_info:
            shutil.copyfile(staged_content, staged_info)
        summary = _validate_content(staged)
        target = _child(settings.draft_root, new_draft_name)
        _set_published_identity(staged, target, summary["duration_us"])
        if not dry_run:
            _publish(staged, target)
        return {
            "success": True,
            "mode": "reference_blueprint_copy",
            "reference_draft": reference_draft,
            "source_format": source_format,
            "draft_name": new_draft_name,
            "draft_path": None if dry_run else str(target),
            "dry_run": dry_run,
            "summary": summary,
            "compatibility": compatibility_notice(settings),
            "stages": ["读取参考草稿", "复制剪辑蓝图", "替换素材与文本", "校验新草稿", "发布新草稿" if not dry_run else "完成试运行"],
        }
    finally:
        if stage_root.exists():
            shutil.rmtree(stage_root)


def batch_from_template(settings: Settings, template_draft: str, jobs: list[dict[str, Any]], *, dry_run: bool = False) -> dict[str, Any]:
    if not jobs:
        raise ManifestError("jobs 不能为空")
    names = [_safe_name(str(job.get("name", "")), "批量草稿名称") for job in jobs]
    if len(set(names)) != len(names):
        raise ManifestError("批量任务的草稿名称不能重复")
    results = []
    for job, name in zip(jobs, names):
        results.append(
            build_from_reference(
                settings,
                template_draft,
                name,
                replacements=job.get("replacements"),
                text_replacements=job.get("text_replacements"),
                dry_run=dry_run,
            )
        )
    return {
        "success": True,
        "mode": "template_batch",
        "template_draft": template_draft,
        "count": len(results),
        "results": results,
        "compatibility": compatibility_notice(settings),
    }
