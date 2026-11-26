from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any

import pyJianYingDraft as draft
from pyJianYingDraft.time_util import srt_tstamp


SRT_TIMESTAMP = r"[0-9]{2,}:[0-5][0-9]:[0-5][0-9][,.][0-9]{3}"
SRT_TIMING = re.compile(rf"({SRT_TIMESTAMP})\s+-->\s+({SRT_TIMESTAMP})")
SRT_FORMATTING = re.compile(r"</?(?:b|i|u|s|font)(?:\s+[^>]*)?>", re.IGNORECASE)
TIME_AMOUNT = r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
TIME_OFFSET = re.compile(rf"[+-]?(?=[0-9.])(?:{TIME_AMOUNT}h)?(?:{TIME_AMOUNT}m)?(?:{TIME_AMOUNT}s)?")
LANGUAGE = re.compile(r"([a-zA-Z]{2,3})-([a-zA-Z]{2})")


class NativeCaptionError(ValueError):
    pass


def remove_unregistered_sdk_speed_ref(segment: draft.TextSegment | draft.StickerSegment) -> None:
    # Text and stickers inherit a speed reference that the SDK only registers for audio/video.
    refs = segment.extra_material_refs
    if segment.speed.global_id in refs:
        refs.remove(segment.speed.global_id)


def _strip_srt_text(text: str) -> str:
    return unescape(SRT_FORMATTING.sub("", text)).strip()


def apply_caption_highlights(
    script: draft.ScriptFile,
    segments: list[draft.TextSegment],
    highlights: Any,
    color: tuple[float, float, float],
    *,
    strip_legacy_text: bool = False,
) -> None:
    if not isinstance(highlights, list):
        raise NativeCaptionError("字幕 highlights 必须是包含 cue 和 ranges 的数组")
    cues: dict[int, tuple[str, list[tuple[int, int]]]] = {}
    for entry in highlights:
        if not isinstance(entry, dict) or entry.keys() != {"cue", "ranges"}:
            raise NativeCaptionError("字幕 highlights 的每项必须包含 cue 和 ranges")
        cue = entry["cue"]
        if type(cue) is not int or not 1 <= cue <= len(segments):
            raise NativeCaptionError(f"字幕 highlights 的 cue 必须是 1–{len(segments)} 内的整数")
        text = segments[cue - 1].text
        if strip_legacy_text:
            text = _strip_srt_text(text)
        text, ranges = cues.setdefault(cue, (text, []))
        if not isinstance(entry["ranges"], list):
            raise NativeCaptionError(f"字幕第 {cue} 段 highlights 的 ranges 必须是数组")
        for span in entry["ranges"]:
            if (not isinstance(span, list) or len(span) != 2
                    or any(type(index) is not int for index in span) or not 0 <= span[0] < span[1] <= len(text)):
                raise NativeCaptionError(f"字幕第 {cue} 段 highlights 范围须满足 0 ≤ start < end ≤ {len(text)}")
            ranges.append((span[0], span[1]))

    if not cues:
        return
    materials = {material["id"]: material for material in script.materials.texts}
    for cue, (text, ranges) in cues.items():
        if not ranges:
            continue
        segment = segments[cue - 1]
        material = materials[segment.material_id]
        content = json.loads(material["content"])
        content["text"] = segment.text = text
        content["styles"] = _caption_highlight_runs(content["styles"][0], len(text), ranges, color)
        material["content"] = json.dumps(content, ensure_ascii=False)


def apply_text_highlight_ranges(
    script: draft.ScriptFile, segment: draft.TextSegment, ranges: Any, color: tuple[float, float, float],
) -> None:
    """Colour character ranges inside one text segment built directly (not via SRT import)."""
    if not isinstance(ranges, list) or not ranges:
        return
    spans: list[tuple[int, int]] = []
    for span in ranges:
        if (not isinstance(span, (list, tuple)) or len(span) != 2
                or any(type(index) is not int for index in span) or not 0 <= span[0] < span[1] <= len(segment.text)):
            raise NativeCaptionError(f"highlight_ranges 范围须满足 0 ≤ start < end ≤ {len(segment.text)}")
        spans.append((span[0], span[1]))
    materials = {material["id"]: material for material in script.materials.texts}
    material = materials[segment.material_id]
    content = json.loads(material["content"])
    content["styles"] = _caption_highlight_runs(content["styles"][0], len(segment.text), spans, color)
    material["content"] = json.dumps(content, ensure_ascii=False)


def _caption_highlight_runs(
    base: dict[str, Any], length: int, ranges: list[tuple[int, int]], color: tuple[float, float, float],
) -> list[dict[str, Any]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))

    spans: list[tuple[int, int, bool]] = []
    cursor = 0
    for start, end in merged:
        if cursor < start:
            spans.append((cursor, start, False))
        spans.append((start, end, True))
        cursor = end
    if cursor < length:
        spans.append((cursor, length, False))

    # The SDK emits one glyph style for the entire cue. Keep all its fields in each run.
    runs = []
    for start, end, highlighted in spans:
        run = deepcopy(base)
        run["range"] = [start, end]
        if highlighted:
            run["fill"]["content"]["solid"]["color"] = list(color)
        runs.append(run)
    return runs


def draft_materials_by_id(content: dict[str, Any]) -> dict[str, dict[str, Any]]:
    materials = content.get("materials") or {}
    return {material["id"]: material for pool in materials.values() if isinstance(pool, list)
            for material in pool if isinstance(material, dict) and material.get("id")}


def validate_generated_material_refs(content: dict[str, Any]) -> None:
    by_id = draft_materials_by_id(content)
    tasks = (content.get("config") or {}).get("subtitle_taskinfo") or []
    task_ids = {task["id"] for task in tasks if isinstance(task, dict) and task.get("id")}

    for track in content["tracks"]:
        for index, segment in enumerate(track.get("segments", [])):
            label = f"轨道 {track.get('name') or track.get('type')} 第 {index + 1} 段"
            material_id = segment.get("material_id")
            if material_id not in by_id:
                raise NativeCaptionError(f"{label} 引用的素材不存在: {material_id}")
            for ref in segment.get("extra_material_refs") or []:
                if ref not in by_id:
                    raise NativeCaptionError(f"{label} 引用的附加素材不存在: {ref}")
            material = by_id[material_id]
            native = material.get("type") == "subtitle" and (
                track.get("flag") == 1 or "recognize_task_id" in material
            )
            if native and material.get("recognize_task_id") not in task_ids:
                raise NativeCaptionError(f"{label} 引用的原生字幕任务不存在: {material.get('recognize_task_id')}")


@dataclass(frozen=True)
class NativeCaptionTrack:
    track_id: str
    language: str


@dataclass(frozen=True)
class _CaptionCue:
    start_us: int
    end_us: int
    text: str


def _offset_us(value: int | float | str) -> int:
    if isinstance(value, str):
        value = value.strip().lower()
        if not TIME_OFFSET.fullmatch(value):
            raise NativeCaptionError("原生字幕 time_offset 必须是微秒数或带 h、m、s 单位的时间字符串")
    elif type(value) not in (int, float):
        raise NativeCaptionError("原生字幕 time_offset 必须是微秒数或带 h、m、s 单位的时间字符串")
    try:
        return draft.tim(value)
    except (ValueError, OverflowError) as exc:
        raise NativeCaptionError("原生字幕 time_offset 无法转换为整数微秒") from exc


def _read_cues(source: Path, *, offset_us: int, duration_us: int) -> list[_CaptionCue]:
    try:
        content = source.read_text(encoding="utf-8-sig").strip()
    except UnicodeDecodeError as exc:
        raise NativeCaptionError("原生字幕 SRT 必须使用 UTF-8 编码") from exc
    if not content:
        raise NativeCaptionError("原生字幕 SRT 不含字幕")

    cues: list[_CaptionCue] = []
    for index, block in enumerate(re.split(r"\n[ \t]*\n", content), start=1):
        lines = block.strip().splitlines()
        if not lines:
            continue
        if len(lines) < 3 or not lines[0].strip().isdigit():
            raise NativeCaptionError(f"SRT 第 {index} 段需要编号、时间戳和正文")
        timing = SRT_TIMING.fullmatch(lines[1].strip())
        if timing is None:
            raise NativeCaptionError(f"SRT 第 {index} 段时间戳无效")
        start, end = (srt_tstamp(stamp.replace(".", ",")) + offset_us for stamp in timing.groups())
        if end <= start:
            raise NativeCaptionError(f"SRT 第 {index} 段结束时间必须晚于开始时间")
        if start < 0 or end > duration_us:
            raise NativeCaptionError(f"SRT 第 {index} 段应用偏移后的时间超出草稿范围 0–{duration_us} 微秒")
        if cues and start < cues[-1].end_us:
            raise NativeCaptionError(f"SRT 第 {index} 段与上一段重叠或时间顺序有误")
        text = _strip_srt_text("\n".join(lines[2:]))
        if not text:
            raise NativeCaptionError(f"SRT 第 {index} 段没有正文")
        cues.append(_CaptionCue(start, end, text))
    return cues


def _language(value: str | None, cues: list[_CaptionCue]) -> str:
    if value is None:
        return "zh-CN" if any(re.search(r"[\u3400-\u9fff]", cue.text) for cue in cues) else "en-US"
    match = LANGUAGE.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        raise NativeCaptionError("原生字幕 language 必须是 zh-CN 或 en-US 一类语言代码")
    code, region = match.groups()
    return f"{code.lower()}-{region.upper()}"


def add_native_caption_track(
    script: draft.ScriptFile,
    *,
    source: str,
    track_name: str,
    time_offset: int | float | str,
    language: str | None,
    style_reference: draft.TextSegment,
    decorate: Callable[[draft.TextSegment], None],
) -> NativeCaptionTrack:
    if not isinstance(source, str) or not source.strip():
        raise NativeCaptionError("原生字幕 source 必须是本地 SRT 文件的绝对路径")
    path = Path(source).expanduser()
    if not path.is_absolute() or path.suffix.lower() != ".srt":
        raise NativeCaptionError("原生字幕 source 必须是本地 SRT 文件的绝对路径")
    if not path.is_file():
        raise FileNotFoundError(path)
    if track_name in script.tracks:
        raise NativeCaptionError(f"原生字幕轨名称与已有轨道重复: {track_name}")

    cues = _read_cues(path, offset_us=_offset_us(time_offset), duration_us=script.duration)
    locale = _language(language, cues)
    track = script.append_track(draft.TrackSpec(draft.TrackType.text, track_name))
    for cue in cues:
        segment = draft.TextSegment.create_from_template(
            cue.text,
            draft.Timerange(cue.start_us, cue.end_us - cue.start_us),
            style_reference,
        )
        remove_unregistered_sdk_speed_ref(segment)
        decorate(segment)
        script.add_segment(segment, track=track)
    return NativeCaptionTrack(track.track_id, locale)


def write_native_caption_metadata(path: Path, native_tracks: list[NativeCaptionTrack]) -> None:
    # Native subtitle task/track/material fields adapted from OpenFilm v0.4.1 (MIT).
    # Copyright (c) 2026 OpenFilm contributors. See THIRD_PARTY_NOTICES.md.
    # https://github.com/openfilm/openfilm/blob/v0.4.1/packages/openfilm/studio/server/jianying.mjs#L356-L393
    content = json.loads(path.read_text(encoding="utf-8"))
    tracks = {track["id"]: track for track in content["tracks"]}
    texts = {material["id"]: material for material in content["materials"]["texts"]}
    tasks = content["config"].setdefault("subtitle_taskinfo", [])
    group_time = time.time_ns() // 1_000_000
    for index, native_track in enumerate(native_tracks):
        track = tracks[native_track.track_id]
        track["flag"] = 1
        language = native_track.language
        task_id = f"{uuid.uuid4().hex[:24]}_0_0"
        group_id = f"{language}_{group_time + index}"
        tasks.append({
            "id": task_id,
            "type": 10,
            "language": language,
            "content": "",
            "remove_invalid_task_id": "",
            "ai_accurate_recognize_enable": False,
            "supplies_commit_id": "",
            "is_local_asr": False,
        })
        for segment in track["segments"]:
            material = texts[segment["material_id"]]
            material.update({
                "type": "subtitle",
                "add_type": 1,
                "recognize_task_id": task_id,
                "recognize_text": json.loads(material["content"])["text"],
                "language": language,
                "group_id": group_id,
                "words": {"start_time": [], "end_time": [], "text": []},
                "current_words": {"start_time": [], "end_time": [], "text": []},
                "subtitle_keywords": {"range": []},
            })
    path.write_text(json.dumps(content, ensure_ascii=False, indent=4), encoding="utf-8")


def sync_native_caption_text(path: Path, material_ids: set[str]) -> None:
    content = json.loads(path.read_text(encoding="utf-8"))
    updated = False
    for material in content["materials"].get("texts", []):
        if material["id"] not in material_ids or material.get("type") != "subtitle" or "recognize_text" not in material:
            continue
        text = json.loads(material["content"])["text"]
        if material["recognize_text"] != text:
            material["recognize_text"] = text
            updated = True
    if updated:
        path.write_text(json.dumps(content, ensure_ascii=False, indent=4), encoding="utf-8")
