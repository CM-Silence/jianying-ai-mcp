from __future__ import annotations

import json
import math
import shutil
import subprocess
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from fractions import Fraction
from html import escape
from pathlib import Path
from typing import Any, Literal

import pyJianYingDraft as draft

from .config import Settings
from .caption_faces import protect_captions
from .draft_ops import _safe_name
from .episode_broll import broll_preview, broll_tracks, plan_broll
from .editing_modules.callouts import get_callout_defaults
from .editing_modules.captions import CaptionTheme, get_caption_theme
from .editing_modules.captions.highlights import COLOR as HIGHLIGHT_COLOR, highlight_ranges
from .jianying_audio_assets import resolve_jianying_audio
from .media_info import parse_media_info
from .picture_pacing import check_picture_pacing
from .production_graphics import (
    ProductionError,
    absolute_file,
    create_output_folder,
    validate_canvas,
    write_contact_sheet,
)
from .production_openfilm import write_openfilm_project
from .production_layouts import (
    DEFAULT_LAYOUT_ID,
    InterviewLayout,
    draw_layout_background,
    prepare_layout_panel,
    read_interview_layout,
)
from .production_styles import EDITORIAL_COLOR, SELLER_HIGHLIGHT, ProductionStyle, StylePreset, get_production_style
from .video_observer import parse_subtitle_file


AUDIO_RATE = 48_000
US_PER_SECOND = 1_000_000
SrtMode = Literal["text", "native"]


@dataclass(frozen=True)
class _Clock:
    fps: int

    def us(self, frame: int) -> int:
        return round(Fraction(frame * US_PER_SECOND, self.fps))

    def frames_covering(self, microseconds: int) -> int:
        return math.ceil(Fraction(microseconds * self.fps, US_PER_SECOND))


@dataclass(frozen=True)
class _Material:
    path: Path | None
    duration: int
    is_photo: bool = False


def _frame(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ProductionError(f"{label} 必须为大于等于 {minimum} 的帧编号或帧数")
    return value


def _volume(value: Any, label: str, default: float = 1.0) -> float:
    value = default if value is None else value
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 2:
        raise ProductionError(f"{label} 必须是 0–2 之间的音量倍率")
    return float(value)


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProductionError(f"{label} 必须是对象")
    return value


def _array(value: Any, label: str) -> list:
    if not isinstance(value, list):
        raise ProductionError(f"{label} 必须是数组")
    return value


class _Materials:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache: dict[tuple[Path, bool], _Material] = {}
        self._catalog: dict[tuple[str, str], _Material] = {}

    def open(self, item: dict[str, Any], label: str, *, audio: bool = False) -> _Material:
        if item.get("jianying_audio") is not None:
            if not audio:
                raise ProductionError(f"{label} 的 jianying_audio 仅用于音频")
            if item.get("source") is not None:
                raise ProductionError(f"{label} 的 source 和 jianying_audio 只能提供一个")
            selector = _object(item["jianying_audio"], f"{label}.jianying_audio")
            draft_path, material_id = selector.get("draft_path"), selector.get("material_id")
            if set(selector) != {"draft_path", "material_id"} or not all(isinstance(value, str) and value for value in (draft_path, material_id)):
                raise ProductionError(f"{label}.jianying_audio 需要 draft_path 与 material_id")
            key = (draft_path, material_id)
            if key not in self._catalog:
                selected = resolve_jianying_audio(self._settings, selector)
                path = Path(selected["source"]) if selected["source"] else None
                duration = selected["duration_us"]
                if path is not None:
                    duration = min(duration, self.open({"source": str(path)}, label, audio=True).duration)
                self._catalog[key] = _Material(path, duration)
            return self._catalog[key]
        path = absolute_file(item.get("source"), f"{label}.source")
        key = (path, audio)
        if key not in self._cache:
            try:
                source = draft.AudioMaterial(str(path)) if audio else draft.VideoMaterial(str(path))
            except (ValueError, OSError, RuntimeError) as exc:
                raise ProductionError(f"{label} 不能作为剪映素材使用: {exc}") from exc
            self._cache[key] = _Material(path, int(source.duration), getattr(source, "material_type", None) == "photo")
        return self._cache[key]


def _target(clock: _Clock, first: int, last: int) -> dict[str, int]:
    return {"start": clock.us(first), "duration": clock.us(last) - clock.us(first)}


def _segment(
    clock: _Clock, item: dict[str, Any], material: _Material, first: int, last: int, label: str, *,
    default_volume: float, natural_duration: bool = False,
) -> dict[str, Any]:
    source_first = _frame(item.get("source_start_frame", 0), f"{label}.source_start_frame")
    span = _target(clock, first, last)
    start_us = clock.us(source_first)
    available = material.duration - start_us
    source_frames = item.get("source_frames")
    if material.is_photo and (source_first or source_frames is not None):
        raise ProductionError(f"{label} 静态图片不接受 source_start_frame 或 source_frames")
    if natural_duration:
        span["duration"] = min(available, span["duration"])
    requested = span["duration"] if source_frames is None else (
        clock.us(source_first + _frame(source_frames, f"{label}.source_frames", minimum=1)) - start_us
    )
    # MediaInfo duration can round file boundaries to milliseconds. Keep the timeline
    # boundary intact and trim this sub-millisecond rounding error from the source.
    if available <= 0 or requested > available + 1_000:
        raise ProductionError(f"{label} 源区间超出素材时长（剩余 {max(0, available) / US_PER_SECOND:.3f} 秒）")
    media = {"jianying_audio": item["jianying_audio"]} if item.get("jianying_audio") is not None else {"source": str(material.path)}
    return {
        **span, **media, "source_start": start_us,
        "source_duration": min(requested, available),
        "volume": _volume(item.get("volume"), f"{label}.volume", default_volume),
    }


def _video_overlays(
    clock: _Clock, raw: Any, first: int, last: int, label: str, materials: _Materials,
    video: list[dict[str, Any]], text: list[dict[str, Any]], callout_roles: Counter[str],
) -> None:
    for index, item in enumerate(_array(raw, f"{label}.overlays"), start=1):
        prefix = f"{label} 的叠加素材 {index}"
        item = _object(item, prefix)
        start = first + _frame(item.get("offset_frame", 0), f"{prefix}.offset_frame")
        stop = start + _frame(item.get("frames", last - start), f"{prefix}.frames", minimum=1)
        if stop > last:
            raise ProductionError(f"{prefix} 超出了所在镜头")
        if "text" in item and "source" in item:
            raise ProductionError(f"{prefix} 只能指定 text 或 source 中的一项")
        if "role" in item and "text" not in item:
            raise ProductionError(f"{prefix}.role 只适用于文字叠层")
        if "text" in item:
            entry = {**_target(clock, start, stop), "text": _caption_text(item["text"], prefix)}
            text_keys = (
                "font", "style", "border", "background", "shadow", "clip", "animations", "keyframes",
                "bubble", "flower_text_resource_id", "portable_animation", "portable_exit_animation",
            )
            if "role" in item:
                role = item["role"]
                entry.update(get_callout_defaults(role, entry["duration"], prefix))
                callout_roles[role] += 1
                item = dict(item)
                for key in ("style", "clip"):
                    if key in item:
                        item[key] = {**entry[key], **_object(item[key], f"{prefix}.{key}")}
            entry.update({key: item[key] for key in text_keys if key in item})
            text.append(entry)
        else:
            material = materials.open(item, prefix)
            entry = _segment(clock, item, material, start, stop, prefix, default_volume=0)
            entry.update({key: item[key] for key in ("clip", "mix_mode", "keyframes", "animations", "filter", "effects", "chroma") if key in item})
            video.append(entry)


def _caption_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2_000:
        raise ProductionError(f"{label} 必须提供非空且不超过 2000 字的文字")
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())


def _caption(clock: _Clock, item: dict[str, Any], total: int, label: str) -> tuple[int, int, str]:
    first = _frame(item.get("start_frame"), f"{label}.start_frame")
    last = _frame(item.get("end_frame"), f"{label}.end_frame", minimum=first + 1)
    if last > total:
        raise ProductionError(f"{label} 超出视频范围")
    return first, last, _caption_text(item.get("text"), f"{label}.text")


def _source_captions(
    raw_path: str, clip: dict[str, Any], first: int, last: int, cache: dict[Path, list[dict[str, Any]]],
) -> list[tuple[int, int, str, bool]]:
    path = absolute_file(raw_path, "transcript_srt_path")
    if path.suffix.casefold() not in {".srt", ".vtt"}:
        raise ProductionError("transcript_srt_path 必须是本地 SRT 或 VTT 文件")
    if path not in cache:
        rows = parse_subtitle_file(path)
        if not rows:
            raise ProductionError(f"素材字幕没有可读取的条目: {path.name}")
        cache[path] = rows
    source_from = clip["source_start"]
    source_to = source_from + clip["source_duration"]
    result = []
    for row in cache[path]:
        start = max(source_from, row["start_ms"] * 1000)
        end = min(source_to, row["end_ms"] * 1000)
        if end <= start:
            continue
        a = first + round(Fraction((start - source_from) * (last - first), clip["source_duration"]))
        b = first + round(Fraction((end - source_from) * (last - first), clip["source_duration"]))
        if b > a:
            result.append((a, b, _caption_text(row["text"], "素材字幕"), bool(clip["volume"])))
    return result


def _keep_source_captions(
    source_cues: list[tuple[int, int, str, bool]], cues: list[tuple[int, int, str]],
) -> list[tuple[int, int, str, bool]]:
    explicit = sorted(cues)
    explicit_index = 0
    last_end = 0
    retained = []
    for start, end, text, audible in sorted(source_cues):
        while explicit_index < len(explicit) and explicit[explicit_index][1] <= start:
            explicit_index += 1
        if explicit_index < len(explicit) and explicit[explicit_index][0] < end:
            continue
        start = max(start, last_end)
        if end > start:
            retained.append((start, end, text, audible))
            last_end = end
    return retained


def _audio_tracks(
    clock: _Clock, items: list[dict[str, Any]], total: int, materials: _Materials, label: str,
    captions: list[tuple[int, int, str]] | None = None,
) -> list[dict[str, Any]]:
    segments = []
    for index, item in enumerate(_array(items, label), start=1):
        prefix = f"{label} {index}"
        item = _object(item, prefix)
        material = materials.open(item, prefix, audio=True)
        first = _frame(item.get("start_frame"), f"{prefix}.start_frame")
        source_first = _frame(item.get("source_start_frame", 0), f"{prefix}.source_start_frame")
        natural = item.get("frames") is None and item.get("source_frames") is None
        frames = item.get("frames", item.get("source_frames"))
        if frames is None:
            frames = clock.frames_covering(material.duration - clock.us(source_first))
        last = first + _frame(frames, f"{prefix}.frames", minimum=1)
        if last > total and not (natural and clock.us(first) + material.duration - clock.us(source_first) <= clock.us(total)):
            raise ProductionError(f"{prefix} 超出画面结束时间，请检查 start_frame 或 frames")
        last = min(last, total)
        segment = _segment(clock, item, material, first, last, prefix, default_volume=1, natural_duration=natural)
        if item.get("text") is not None and captions is not None:
            cue = _caption(clock, {"start_frame": first, "end_frame": item.get("subtitle_end_frame", last), "text": item["text"]}, total, prefix)
            captions.append(cue)
        segments.append(segment)
    return segments


def _lanes(track_type: str, name: str, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tracks: list[dict[str, Any]] = []
    ends: list[int] = []
    for segment in sorted(segments, key=lambda segment: segment["start"]):
        mark = segment["start"]
        for index, end in enumerate(ends):
            if end <= mark:
                tracks[index]["segments"].append(segment)
                ends[index] = mark + segment["duration"]
                break
        else:
            tracks.append({"type": track_type, "name": f"{name} {len(tracks) + 1}", "segments": [segment]})
            ends.append(mark + segment["duration"])
    return tracks


def _srt_time(milliseconds: int) -> str:
    minutes, remainder = divmod(milliseconds, 60_000)
    seconds, fraction = divmod(remainder, 1_000)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{seconds:02d},{fraction:03d}"


def _write_srt(
    path: Path, clock: _Clock, cues: list[tuple[int, int, str]], total: int,
    highlights: dict[tuple[int, int, str], list[tuple[int, int]]],
) -> int:
    blocks = []
    fast = 0
    end = 0
    for index, (first, last, caption) in enumerate(sorted(cues), start=1):
        if first < end:
            raise ProductionError(f"字幕第 {index} 段重叠，请调整 captions 或旁白文本的时间")
        # A cue's end at the final frame must never extend the draft by rounding up.
        start_ms = round(Fraction(first * 1_000, clock.fps))
        end_ms = min(round(Fraction(last * 1_000, clock.fps)), clock.us(total) // 1_000)
        # Highlight positions refer to literal text, including any characters SRT
        # would otherwise parse as formatting or entities.
        payload = escape(caption, quote=False) if (first, last, caption) in highlights else caption
        blocks.append(f"{index}\n{_srt_time(start_ms)} --> {_srt_time(end_ms)}\n{payload}\n")
        is_cjk = any("\u3400" <= character <= "\u9fff" for character in caption)
        if len(caption) / ((last - first) / clock.fps) > (8 if is_cjk else 20):
            fast += 1
        end = last
    path.write_text("\n".join(blocks), encoding="utf-8")
    return fast


def _ffmpeg(settings: Settings, arguments: list[str], label: str, *, cwd: Path | None = None) -> None:
    ffmpeg = settings.ffmpeg_exe
    if ffmpeg is None or not ffmpeg.is_file():
        raise ProductionError("此步骤需要本地 FFmpeg。请设置 JIANYING_FFMPEG 或加入 PATH。")
    try:
        result = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *arguments],
            cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace", timeout=420,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProductionError(f"{label}未完成: {exc}") from exc
    if result.returncode:
        raise ProductionError(f"{label}失败: {result.stderr.strip()[-1200:]}")


def _seconds(us: int) -> str:
    return f"{us / US_PER_SECOND:.6f}"


def _samples(us: int) -> int:
    return round(Fraction(us * AUDIO_RATE, US_PER_SECOND))


def _audio_filter(segment: dict[str, Any], *, offset: bool, fade: bool = False) -> str:
    first = _samples(segment["source_start"])
    last = _samples(segment["source_start"] + segment["source_duration"])
    duration = _samples(segment["duration"])
    tempo = segment["source_duration"] / segment["duration"]
    rate = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    filters = [rate, f"atrim=start_sample={first}:end_sample={last}", "asetpts=PTS-STARTPTS"]
    # atempo supports [0.5, 2] without dropping samples. Chain filters for larger changes.
    while tempo < 0.5 or tempo > 2:
        part = 0.5 if tempo < 0.5 else 2
        filters.append(f"atempo={part}")
        tempo /= part
    if abs(tempo - 1) > 1e-6:
        filters.append(f"atempo={tempo:.12f}")
    filters.extend([f"apad=whole_len={duration}", f"atrim=end_sample={duration}"])
    if fade:
        fade_in = max(1, min(duration // 4, _samples(150_000)))
        fade_out = max(1, min(duration // 4, _samples(300_000)))
        filters.extend([f"afade=t=in:ss=0:ns={fade_in}", f"afade=t=out:ss={duration - fade_out}:ns={fade_out}"])
    if offset:
        filters.append(f"volume={segment['volume']:.8f}")
        delay = _samples(segment["start"])
        if delay:
            filters.append(f"adelay={delay}S:all=1")
    return ",".join(filters)


def _duck_filter(speech: list[dict[str, Any]], volume: float, duck: float) -> str:
    ranges: list[tuple[int, int]] = []
    for segment in sorted(speech, key=lambda clip: clip["start"]):
        if not segment["volume"]:
            continue
        first, last = segment["start"], segment["start"] + segment["duration"]
        if ranges and first <= ranges[-1][1] + 260_000:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], last))
        else:
            ranges.append((first, last))
    if not ranges or volume == duck:
        return f"volume={volume:.8f}"
    ramps = [f"clip(min((t-{_seconds(first - 150_000)})/0.15,({_seconds(last + 250_000)}-t)/0.25),0,1)" for first, last in ranges]
    # Ranges have separate speech plateaus but their ramps can intersect briefly.
    # Pair combinations keep the expression tree shallow for FFmpeg's parser.
    while len(ramps) > 1:
        ramps = [f"max({ramps[i]},{ramps[i + 1]})" if i + 1 < len(ramps) else ramps[i] for i in range(0, len(ramps), 2)]
    return f"volume='{volume:.8f}-({volume - duck:.8f})*{ramps[0]}':eval=frame"


def _audio_job(settings: Settings, folder: Path, output: Path, args: list[str], graph: str) -> None:
    command = ["-map", "[out]", "-c:a", "pcm_s16le", output.name]
    if len(graph) < 12_000:
        _ffmpeg(settings, [*args, "-filter_complex", graph, *command], "处理制作时间线音频", cwd=folder)
    else:
        script = output.with_suffix(".filter.txt")
        try:
            script.write_text(graph, encoding="ascii")
            _ffmpeg(settings, [*args, "-/filter_complex", script.name, *command], "处理制作时间线音频", cwd=folder)
        finally:
            script.unlink(missing_ok=True)


def _music_parameters(item: dict[str, Any]) -> tuple[float, float, bool]:
    volume = _volume(item.get("volume"), "bgm.volume", 0.24)
    duck = _volume(item.get("duck_volume"), "bgm.duck_volume", volume * 0.40)
    if duck > volume:
        raise ProductionError("bgm.duck_volume 须小于或等于 bgm.volume")
    loop = item.get("loop", True)
    if type(loop) is not bool:
        raise ProductionError("bgm.loop 必须是布尔值")
    return volume, duck, loop


def _native_background_music(
    item: dict[str, Any], material: _Material, total_us: int, speech: list[dict[str, Any]],
    volume: float, duck: float, loop: bool,
) -> list[dict[str, Any]]:
    # Keep each stretch pointed at the same library resource. Source time advances across duck boundaries.
    actual = total_us if loop else min(material.duration, total_us)
    loops = range(material.duration, actual, material.duration)
    voice_ranges: list[tuple[int, int]] = []
    if volume != duck:
        for clip in sorted(speech, key=lambda row: row["start"]):
            a = max(0, clip["start"])
            b = min(actual, clip["start"] + clip["duration"])
            if not clip["volume"] or a >= b:
                continue
            if voice_ranges and a <= voice_ranges[-1][1]:
                voice_ranges[-1] = (voice_ranges[-1][0], max(b, voice_ranges[-1][1]))
            else:
                voice_ranges.append((a, b))
    boundaries = sorted({0, actual, *loops, *(edge for interval in voice_ranges for edge in interval)})
    segments = []
    cue_index = 0
    for a, b in zip(boundaries, boundaries[1:]):
        while cue_index < len(voice_ranges) and voice_ranges[cue_index][1] <= a:
            cue_index += 1
        covered = cue_index < len(voice_ranges) and voice_ranges[cue_index][0] <= a < voice_ranges[cue_index][1]
        segments.append({
            "jianying_audio": item["jianying_audio"], "start": a, "duration": b - a,
            "source_start": a % material.duration, "source_duration": b - a,
            "volume": duck if covered else volume,
        })
    return segments


def _background_music(
    settings: Settings, folder: Path, item: dict[str, Any], total_us: int,
    speech: list[dict[str, Any]], materials: _Materials,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    item = _object(item, "bgm")
    material = materials.open(item, "bgm", audio=True)
    if material.duration < 10_000:
        raise ProductionError("配乐源文件须至少有 10 毫秒有效音频")
    volume, duck, loop = _music_parameters(item)
    config = {"loop": loop, "volume": volume, "duck_volume": duck}
    if item.get("jianying_audio") is not None:
        segments = _native_background_music(item, material, total_us, speech, volume, duck, loop)
        return segments, {**config, "jianying_audio": item["jianying_audio"], "stem_path": None}
    actual = total_us if loop else min(material.duration, total_us)
    stem = folder / "background-music.wav"
    original = {"source": str(material.path), "start": 0, "duration": actual, "source_start": 0, "source_duration": actual, "volume": 1.0}
    cmd = ["-protocol_whitelist", "file", *(["-stream_loop", "-1"] if loop else []), "-i", str(material.path)]
    filters = _audio_filter(original, offset=False, fade=True)
    _audio_job(settings, folder, stem, cmd, f"[0:a:0]{filters},{_duck_filter(speech, volume, duck)}[out]")
    processed = {**original, "source": str(stem)}
    # WAV duration is reported to milliseconds by the draft library.
    processed["source_duration"] = min(actual, materials.open(processed, "bgm 渲染结果", audio=True).duration)
    return [processed], {**config, "source": str(material.path), "stem_path": str(stem)}


def _mix_audio(settings: Settings, folder: Path, clips: list[dict[str, Any]], total_us: int, materials: _Materials) -> Path:
    output = folder / "preview-mix.wav"
    arguments: list[str] = []
    filters = []
    links = []
    for index, segment in enumerate(clip for clip in clips if clip["volume"]):
        source = segment.get("source")
        if source is None:
            material = materials.open(segment, "混音中的剪映音频", audio=True)
            if material.path is None:
                raise ProductionError("混音中的剪映曲库或音效素材需要可读取的本地音频；可先生成剪映草稿，再由剪映加载素材并导出")
            source = str(material.path)
        arguments.extend(["-protocol_whitelist", "file", "-i", source])
        output_label = f"[mix{index}]"
        filters.append(f"[{index}:a:0]{_audio_filter(segment, offset=True)}{output_label}")
        links.append(output_label)
    count = _samples(total_us)
    pad = f"apad=whole_len={count},atrim=end_sample={count}"
    if links:
        filters.append(f"{''.join(links)}amix=inputs={len(links)}:normalize=0:dropout_transition=0,{pad},alimiter=limit=0.95:level=false:latency=true[out]")
    else:
        filters.append(f"anullsrc=r={AUDIO_RATE}:cl=stereo,atrim=end_sample={count}[out]")
    _audio_job(settings, folder, output, arguments, ";\n".join(filters))
    return output


def _background_preview(
    settings: Settings, folder: Path, data: list[tuple[dict[str, Any], _Material]], maximum: int,
) -> tuple[Path, int]:
    sheet_frames = []
    count = min(maximum, len(data))
    indices = [len(data) // 2] if count == 1 else [round(Fraction(i * (len(data) - 1), count - 1)) for i in range(count)]
    for index in indices:
        clip, material = data[index]
        source = material.path
        if not material.is_photo:
            source = folder / f"preview-{index:03d}.png"
            first = clip["source_start"]
            midpoint = first + clip["source_duration"] // 2
            for seek in dict.fromkeys((midpoint, max(0, first - 1_000))):
                _ffmpeg(settings, [
                    "-protocol_whitelist", "file", "-ss", _seconds(seek), "-i", str(material.path),
                    "-map", "0:V:0", "-frames:v", "1", "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
                    "-c:v", "png", "-pix_fmt", "rgb24", "-threads", "1", str(source),
                ], f"提取第 {index + 1} 个镜头的预览")
                if source.is_file():
                    break
            else:
                raise ProductionError(f"第 {index + 1} 个镜头没有可用于预览的画面")
        stamp = _srt_time((clip["start"] + clip["duration"] // 2) // 1000).replace(",", ".")
        sheet_frames.append((source, stamp))
    contact = folder / "shot-preview.jpg"
    write_contact_sheet(sheet_frames, contact)
    for p, _ in sheet_frames:
        if p.parent == folder:
            p.unlink(missing_ok=True)
    return contact, count


def _transition_plan(
    clock: _Clock, shots: list[dict[str, Any]], visual_data: list[tuple[dict[str, Any], _Material]],
    shot_ranges: list[dict[str, Any]], chapters: list[dict[str, Any]], profile: ProductionStyle | None,
) -> dict[int, dict[str, Any]]:
    chapter_indexes = {chapter["shot_index"] for chapter in chapters}
    plan = {}
    for index, shot in enumerate(shots):
        previous, _ = visual_data[index]
        choice = shot.get("portable_transition")
        next_is_chapter = profile is not None and not profile.reference_dialogue and index + 2 in chapter_indexes
        if choice is None and not next_is_chapter:
            continue
        if index + 1 == len(shots):
            raise ProductionError("最后一个镜头后没有可连接的画面，请移除 portable_transition")
        maximum = min(shot["frames"], shots[index + 1]["frames"])
        if choice is None:
            assert profile is not None
            half = min(maximum // 2, max(1, round(clock.fps * profile.transition_half_seconds)))
            if half == 0:
                continue
            choice = {"kind": profile.portable_transition, "frames": half * 2, "color": profile.transition_color}
        spec = _object(choice, f"镜头 {index + 1} 的 portable_transition")
        if spec.keys() - {"kind", "frames", "color"} or spec.get("kind") not in {"dip", "wipe_left", "wipe_right"}:
            raise ProductionError("portable_transition 需要 kind 和 frames，可选 color；kind 支持 dip/wipe_left/wipe_right")
        frames = _frame(spec.get("frames"), "转场 frames", minimum=2)
        if frames % 2 or frames > maximum:
            raise ProductionError(f"镜头 {index + 1} 的转场需使用偶数帧，且不超过相邻较短镜头的 {maximum} 帧")
        boundary = shot_ranges[index]["end_frame"]
        span = _target(clock, boundary - frames // 2, boundary + frames // 2)
        color = spec.get("color", profile.transition_color if profile else EDITORIAL_COLOR)
        if not isinstance(color, str) or len(color) != 7 or not color.startswith("#") or any(
            digit not in "0123456789abcdefABCDEF" for digit in color[1:]
        ):
            raise ProductionError("转场 color 必须是 #RRGGBB")
        plan[index] = {"start_us": span["start"], "duration_us": span["duration"], "kind": spec["kind"], "color": color}
        if profile and next_is_chapter and "transition" not in shot:
            previous["transition"] = {"name": profile.transition, "duration": span["duration"]}
    return plan


def _openfilm_manifest(manifest: dict[str, Any], transitions: dict[int, dict[str, Any]]) -> dict[str, Any]:
    tracks = []
    for track in manifest["tracks"]:
        segments = []
        for segment_index, clip in enumerate(track["segments"]):
            removed = {"transition"} if track["name"] == "主画面" and segment_index in transitions else set()
            if track["type"] == "text":
                # OpenFilm draws text as a page: keyframes and native resources do not apply,
                # and the portable entrance replaces the native animation.
                removed.add("keyframes")
                if "portable_serif" in clip:
                    removed.add("font")
                if clip.get("portable_animation") or clip.get("portable_exit_animation"):
                    removed.update(("font", "animations"))
            segments.append({key: value for key, value in clip.items() if key not in removed})
        tracks.append({**track, "segments": segments})
    return {**manifest, "tracks": tracks}


def compile_video_timeline(
    settings: Settings,
    name: str,
    shots: list[dict[str, Any]],
    *,
    fps: int = 30,
    width: int = 1080,
    height: int = 1920,
    narration: list[dict[str, Any]] | None = None,
    audio: list[dict[str, Any]] | None = None,
    captions: list[dict[str, Any]] | None = None,
    bgm: dict[str, Any] | None = None,
    srt_mode: SrtMode = "text",
    caption_style: dict[str, Any] | None = None,
    caption_clip: dict[str, Any] | None = None,
    preview_frames: int = 0,
    mixdown: bool = False,
    include_manifest: bool = False,
    openfilm_project: bool = False,
    style_preset: StylePreset | None = None,
    caption_decoration: dict[str, Any] | None = None,
    portable_caption_animation: str | dict[str, Any] | None = None,
    caption_theme: CaptionTheme | None = None,
    caption_items: list[dict[str, Any]] | None = None,
    broll: list[dict[str, Any]] | None = None,
    min_scene_seconds: float | None = None,
) -> dict[str, Any]:
    """Compile one frame-indexed timeline into a directly buildable editable draft manifest."""
    name = _safe_name(name, "草稿名称")
    validate_canvas(width, height)
    if openfilm_project and (width % 2 or height % 2):
        raise ProductionError("OpenFilm MP4 画幅的宽和高必须是偶数")
    if type(fps) is not int or not 1 <= fps <= 120:
        raise ProductionError("fps 必须为 1–120 之间的整数")
    if srt_mode not in {"text", "native"}:
        raise ProductionError("srt_mode 必须为 text 或 native")
    if type(preview_frames) is not int or not 0 <= preview_frames <= 24:
        raise ProductionError("preview_frames 必须为 0–24 之间的整数")
    if not _array(shots, "shots"):
        raise ProductionError("shots 至少需要一个主画面镜头")
    theme = get_caption_theme(caption_theme)
    profile = get_production_style(style_preset)
    highlight_color = SELLER_HIGHLIGHT if profile and profile.reference_dialogue else HIGHLIGHT_COLOR
    decoration = {"font": profile.font, **profile.caption_decoration} if profile else {}
    if theme:
        if not theme.inherit_background:
            decoration.pop("background", None)
        decoration.update(deepcopy(theme.decoration))
    if caption_decoration is not None:
        decoration.update(_object(caption_decoration, "字幕装饰"))
    portable_decoration = {key: decoration[key] for key in ("border", "background", "shadow") if key in decoration}
    if ((theme and not theme.inherit_background) or (profile and profile.reference_dialogue)) and portable_decoration.get("background") is None:
        portable_decoration["background"] = False
    if profile and profile.serif:
        portable_decoration["serif"] = True
    portable_entrance = portable_caption_animation
    if portable_entrance is None and profile:
        portable_entrance = profile.caption_entrance
    if portable_entrance is not None and portable_entrance != "none":
        portable_decoration["portable_animation"] = portable_entrance
    clock = _Clock(fps)
    materials = _Materials(settings)
    warnings: list[str] = []
    font = str(decoration.get("font") or "")
    if font and ("台北" in font or "_TC" in font or font.endswith("TC")):
        warnings.append(
            f"字幕字体 {font} 是繁体（台湾）规范字形，逗号句号位于字格中央，简体字幕会显得标点偏高；"
            "建议改用 HarmonyOS_Sans_SC_Bold 等简体字体。"
        )
    current = 0
    video, text, visual_data, shot_ranges = [], [], [], []
    layouts: dict[int, tuple[InterviewLayout, dict[str, Any], _Material]] = {}
    layout_counts: Counter[str] = Counter()
    callout_roles: Counter[str] = Counter()
    mapped_cues = []
    chapters = []
    source_subtitle_cache: dict[Path, list[dict[str, Any]]] = {}
    for index, shot in enumerate(shots, start=1):
        label = f"镜头 {index}"
        shot = _object(shot, label)
        end = current + _frame(shot.get("frames"), f"{label}.frames", minimum=1)
        material = materials.open(shot, label)
        segment = _segment(clock, shot, material, current, end, label, default_volume=1)
        segment.update({key: shot[key] for key in ("transition", "clip", "keyframes", "animations", "filter", "effects") if key in shot})
        if "layout" in shot:
            layout = read_interview_layout(shot["layout"], width, height, label)
            if layout is not None:
                if segment.get("clip") and set(_object(segment["clip"], f"{label}.clip")) - {"alpha"}:
                    raise ProductionError(f"{label} 分屏可通过 focus 裁切，clip 仅适用 alpha")
                top_material = materials.open(layout.top_source, f"{label}.layout.top")
                top = _segment(clock, layout.top_source, top_material, current, end, f"{label}.layout.top", default_volume=0)
                layouts[index - 1] = layout, top, top_material
                crop = _panel_crop(settings, top_material, layout)
                if crop is not None:
                    warnings.append(
                        f"{label} 的副画面会被裁掉约 {crop:.0%}（素材与栏位宽高比差异大）。"
                        "动效卡请按栏位尺寸渲染，或设 layout.top.fit=\"contain\"。"
                    )
            layout_counts[layout.kind if layout else DEFAULT_LAYOUT_ID] += 1
        if "overlays" in shot:
            _video_overlays(clock, shot["overlays"], current, end, label, materials, video, text, callout_roles)
        if shot.get("chapter") is not None:
            chapter = _caption_text(shot["chapter"], f"{label}.chapter")
            if len(chapter) > 32:
                raise ProductionError(f"{label}.chapter 最多 32 个字符")
            if not chapters or chapters[-1]["title"] != chapter:
                chapters.append({"title": chapter, "shot_index": index, "start_frame": current})
        if shot.get("transcript_srt_path") is not None:
            if material.is_photo:
                raise ProductionError(f"{label} 是静态图，不能关联源视频字幕")
            mapped_cues.extend(_source_captions(shot["transcript_srt_path"], segment, current, end, source_subtitle_cache))
        visual_data.append((segment, material))
        shot_ranges.append({"index": index, "source": str(material.path), "start_frame": current, "end_frame": end})
        current = end
    duration = clock.us(current)
    placements = plan_broll(settings, broll, fps=fps, total_frames=current, width=width, height=height)
    if any("native_size" in row for row in placements) and (preview_frames or openfilm_project):
        warnings.append("原生官方素材需在剪映中预览和导出，本次生成可编辑草稿清单。")
        preview_frames, openfilm_project = 0, False
    check_picture_pacing([{**shot, "_still_image": material.is_photo}
                          for shot, (_, material) in zip(shots, visual_data)],
                         placements, fps, style_preset, min_scene_seconds)
    portable_transitions = _transition_plan(clock, shots, visual_data, shot_ranges, chapters, profile)

    cues, highlights = [], {}
    for raw_cue in _array([] if captions is None else captions, "captions"):
        cue = _object(raw_cue, "字幕")
        row = _caption(clock, cue, current, "字幕")
        cues.append(row)
        selected = highlight_ranges(cue.get("highlights", []), row[2])
        if selected:
            highlights[row] = selected
    voice = _audio_tracks(clock, [] if narration is None else narration, current, materials, "旁白", cues)
    sounds = _audio_tracks(clock, [] if audio is None else audio, current, materials, "声音")
    material_cues = _keep_source_captions(mapped_cues, cues)
    cues.extend((a, b, text) for a, b, text, _ in material_cues)
    speech = list(voice)
    speech.extend({**_target(clock, a, b), "volume": 1.0} for a, b, _, audible in material_cues if audible)
    for cue in captions or []:
        if cue.get("duck_bgm") is True:
            speech.append({**_target(clock, cue["start_frame"], cue["end_frame"]), "volume": 1.0})

    folder = create_output_folder(settings, "timelines")
    try:
        inserts = broll_tracks(settings, folder, placements, [(clip, mat.is_photo) for clip, mat in visual_data],
                               width=width, height=height, fps=fps)
        background_segments, top_segments = [], []
        for index, (layout, top, top_material) in layouts.items():
            lower, lower_material = visual_data[index]
            matte = folder / f"panel-{index + 1:03d}-background.png"
            draw_layout_background(matte, layout, width, height)
            background_segments.append({
                "source": str(matte), "start": lower["start"], "duration": lower["duration"],
                "source_start": 0, "source_duration": lower["duration"], "volume": 0,
            })
            for panel_name, clip, material, rect, focus, fit in (
                ("top", top, top_material, layout.secondary_rect, layout.top_focus, layout.top_fit),
                ("bottom", lower, lower_material, layout.main_rect, layout.bottom_focus, "cover"),
            ):
                panel = prepare_layout_panel(
                    settings, folder, clip, material.is_photo, rect, focus,
                    name=f"panel-{index + 1:03d}-{panel_name}", width=width, height=height, fps=fps,
                    fit=fit, background=layout.background,
                )
                if panel_name == "top":
                    top_segments.append(panel)
                else:
                    visual_data[index] = panel, materials.open(panel, f"分屏 {index + 1} 下半画面")
        tracks = []
        if background_segments:
            tracks.append({"type": "video", "name": "分屏底板", "segments": background_segments})
        tracks.append({"type": "video", "name": "主画面", "segments": [clip for clip, _ in visual_data]})
        if top_segments:
            tracks.append({"type": "video", "name": "分屏上方画面", "segments": top_segments})
        tracks.extend(_lanes("video", "叠加素材", video))
        tracks.extend(inserts)
        tracks.extend(_lanes("text", "图文", text))
        caption_safety = None
        if caption_items:
            caption_items = deepcopy(caption_items)
            caption_safety = protect_captions(caption_items, tracks, width, height)
            for issue in caption_safety["review"]:
                warnings.append(f'{issue["start_us"]/1e6:.2f}–{issue["end_us"]/1e6:.2f}s：{issue["reason"]}')
            tracks.extend(_lanes("text", "字幕", caption_items))
        tracks.extend(_lanes("audio", "旁白", voice))
        tracks.extend(_lanes("audio", "音效", sounds))
        manifest = {"draft": {"name": name, "width": width, "height": height, "fps": fps}, "tracks": tracks}
        fast_cues = 0
        srt = None
        if cues:
            srt = folder / "captions.srt"
            fast_cues = _write_srt(srt, clock, cues, current, highlights)
        if cues and not caption_items:
            style = {"size": 6, "align": 1, "bold": True, "auto_wrapping": True, "max_line_width": 0.85}
            theme_style = deepcopy(theme.style) if theme else {}
            position = theme.position if theme else profile.caption_position if profile else -.75
            manifest["srt_imports"] = [{
                "source": str(srt), "track_name": "字幕", "mode": srt_mode,
                "style": {**style, **(profile.caption_style if profile else {}), **theme_style, **(caption_style or {})},
                "clip": {"transform_y": position, **(caption_clip or {})},
                **({"decoration": decoration} if decoration else {}),
            }]
            if highlights:
                manifest["srt_imports"][0].update(
                    highlights=[{"cue": index, "ranges": [list(span) for span in highlights[cue]]}
                                for index, cue in enumerate(sorted(cues), start=1) if cue in highlights],
                    highlight_color=list(highlight_color),
                )
        music_info = None
        music = []
        if bgm is not None:
            music, music_info = _background_music(settings, folder, bgm, duration, speech, materials)
            tracks.append({"type": "audio", "name": "配乐", "segments": music})
        audio_path = None
        if mixdown or openfilm_project:
            audio_by_source = {}
            original = []
            # Visual tracks can carry live sound. The optional mix includes it too.
            for clip in [*[clip for clip, material in visual_data if not material.is_photo], *video]:
                path = clip["source"]
                if clip["volume"] == 0:
                    continue
                if path not in audio_by_source:
                    audio_by_source[path] = parse_media_info(settings, path)["media_info"]["has_audio"]
                if audio_by_source[path]:
                    original.append(clip)
            audio_path = _mix_audio(settings, folder, [*original, *voice, *sounds, *music], duration, materials)
        project = None
        if openfilm_project:
            kinds = {str(material.path): material.is_photo for _, material in visual_data}
            kinds.update({clip["source"]: materials.open(clip, "叠加素材").is_photo for clip in [*video, *top_segments]})
            kinds.update({clip["source"]: True for clip in background_segments})
            for track in inserts:
                for clip in track["segments"]:
                    kinds[clip["source"]] = materials.open(clip, track["name"]).is_photo
            if highlights:
                portable_decoration["highlight_color"] = list(highlight_color)
            project = write_openfilm_project(
                folder, _openfilm_manifest(manifest, portable_transitions), kinds,
                [] if caption_items else [(clock.us(a), clock.us(b), text) for a, b, text in cues],
                audio_path, duration,
                portable_transitions=list(portable_transitions.values()), caption_decoration=portable_decoration,
                cue_highlights={(clock.us(a), clock.us(b), text): ranges for (a, b, text), ranges in highlights.items()},
            )
        preview, preview_count = None, 0
        if preview_frames:
            if placements or (profile and profile.reference_dialogue and video):
                preview, preview_count = broll_preview(settings, folder, tracks, width, height, preview_frames)
            else:
                preview, preview_count = _background_preview(settings, folder, visual_data, preview_frames)
        manifest_path, timeline_path = folder / "manifest.json", folder / "timeline.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        timeline_path.write_text(json.dumps({
            "fps": fps, "total_frames": current, "duration_us": duration, "shots": shot_ranges,
            "caption_safety": caption_safety,
            **({"broll": placements} if placements else {}),
            "captions": [{"start_frame": a, "end_frame": b, "text": text} for a, b, text in sorted(cues)],
            "narration": voice, "audio": sounds,
            "bgm": music_info, "manifest_path": str(manifest_path), "chapters": chapters,
            "portable_transitions": list(portable_transitions.values()),
            "openfilm_project_path": str(project) if project else None,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise

    result = {
        "manifest_path": str(manifest_path), "timeline_path": str(timeline_path),
        "srt_path": str(srt) if srt else None, "preview_path": str(preview) if preview else None,
        "bgm_path": music_info["stem_path"] if music_info else None,
        "mix_path": str(audio_path) if audio_path else None, "width": width, "height": height, "fps": fps,
        "openfilm_project_path": str(project) if project else None,
        "total_frames": current, "duration_us": duration, "shot_count": len(shots), "caption_count": len(cues),
        "caption_safety": caption_safety,
        "preview_frames": preview_count, "fast_caption_count": fast_cues,
        **({"broll": placements} if placements else {}),
        "source_caption_count": len(material_cues), "source_caption_omitted_count": len(mapped_cues) - len(material_cues),
    }
    if chapters or portable_transitions or decoration or portable_entrance or theme or highlights or layout_counts or callout_roles:
        result["design"] = {
            "style_preset": style_preset,
            "chapter_starts_s": [round(clock.us(chapter["start_frame"]) / US_PER_SECOND, 3) for chapter in chapters],
            "native_transitions": sum(bool(clip.get("transition")) for clip, _ in visual_data),
            "portable_transitions": len(portable_transitions),
            "caption_font": decoration.get("font") if cues else None,
            "native_caption_resources": sorted(decoration.keys() - {"background", "border", "shadow"}) if cues else [],
            "portable_caption_decorated": bool(cues and portable_decoration),
        }
        if layout_counts:
            layout_counts[DEFAULT_LAYOUT_ID] += len(shots) - sum(layout_counts.values())
            result["design"]["layouts"] = {key: value for key, value in layout_counts.items() if value}
        if callout_roles:
            result["design"]["callouts"] = dict(callout_roles)
        if theme:
            result["design"]["caption_theme"] = theme.id
        if highlights:
            result["design"]["highlighted_cues"] = len(highlights)
    if warnings:
        result["warnings"] = warnings
    if include_manifest:
        result["manifest"] = manifest
    return result


# A secondary picture losing more than this share of its frame to a cover crop
# almost always means a mismatched asset (e.g. a 9:16 card in a landscape panel).
PANEL_CROP_WARNING = 0.3


def _panel_crop(settings: Settings, material: _Material, layout: InterviewLayout) -> float | None:
    """Share of the secondary picture that a cover fit would crop away, if large."""
    if layout.top_fit != "cover" or material.path is None:
        return None
    try:
        info = parse_media_info(settings, str(material.path))["media_info"]
    except Exception:  # noqa: BLE001 - a probe failure must not block compiling
        return None
    width, height = info.get("display_width") or info.get("width"), info.get("display_height") or info.get("height")
    if not width or not height:
        return None
    _, _, panel_w, panel_h = layout.secondary_rect
    source_ratio, panel_ratio = width / height, panel_w / panel_h
    crop = 1 - min(source_ratio, panel_ratio) / max(source_ratio, panel_ratio)
    return crop if crop > PANEL_CROP_WARNING else None
