"""Storyboard compiler: beats in, frame timeline out.

An episode spec is deliberately editorial, not technical: it names beats, the
line being spoken, the card being shown and how pictures share the frame, and
lets this module work out frame counts, take selection, captions, sound cues
and the draft manifest.

Beat kinds:

* ``talk``    – a range of camera footage with its own audio (the speaker).
* ``media``   – any other picture: B-roll, screen recording, product shot or a
                generated video/image. Muted by default.
* ``graphic`` – a full-screen motion card from the recipe library.

``talk`` and ``media`` beats take an optional ``layout`` that splits the frame:
the beat's own footage fills one panel and ``layout.with`` (a motion card, or
another picture) fills the other. Motion cards are rendered at the exact panel
size, so a vertical card is never cropped into a landscape panel.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .audio_production import create_music_bed
from .asset_batch import load_batch_catalog
from .caption_placement import choose_caption_anchor, fixed_caption_slots, zone_anchor
from .caption_text import (PUNCTUATION_MODES, CaptionPunctuation, caption_cues, caption_entities, emphasis_ranges,
                           follows_caption_step, semantic_caption_runs, tidy_caption, visible_length)
from .config import Settings
from .editing_modules.layouts import read_interview_layout
from .editorial_review import episode_review
from .episode_broll import speaker_panel_windows
from .media_info import parse_media_info
from .media_prepare import editing_clip, extract_audio_range, needs_upright_copy
from .motion_library import RECIPES, render_motion
from .production import CaptionTheme, compile_video_timeline
from .production_graphics import ProductionError, circle_matte, validate_canvas
from .production_styles import SELLER_HIGHLIGHT, ProductionStyle, StylePreset, get_production_style
from .sound_design import caption_accent_tracks, item_accent_tracks, plan_sound, synthesize_sfx


BEAT_KINDS = ("talk", "media", "graphic")
SPLIT_LAYOUTS = ("stacked", "interview_box")
BEAT_ROLES = ("summary",)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MAX_BEATS = 120
MAX_PANEL_GRAPHIC_SECONDS = 30.0
RECAP_RECIPES = frozenset(name for name, recipe in RECIPES.items() if recipe.role == "recap")
TRANSITION_POLICIES = ("auto", "all", "none")
# Caption entrance per presentation mode: a native 剪映 animation and its
# OpenFilm equivalent, so the draft and the preview move the same way.
CAPTION_ENTRANCES = {
    "single": ({"category": "text_intro", "name": "弹入", "duration": 240_000}, "rise"),
    "stack": ({"category": "text_intro", "name": "渐显", "duration": 160_000}, "fade"),
    "blocks": ({"category": "text_intro", "name": "渐显", "duration": 160_000}, "fade"),
    "words": ({"category": "text_intro", "name": "渐显", "duration": 150_000}, "fade"),
}
CAPTION_SIZE = 11.5


def read_episode_file(file: Path, media_root: str | None = None) -> dict:
    """Resolve only declared media slots, relative to the spec or an explicit footage root."""
    spec = json.loads(file.read_text(encoding="utf-8-sig"))
    if not isinstance(spec, dict):
        raise ProductionError("分镜 JSON 须为对象")
    root = Path(media_root).expanduser() if media_root else file.resolve().parent
    if not root.is_dir() or not root.is_absolute():
        raise ProductionError("media_root 需要存在的绝对目录")
    def resolve_slot(slot: Any, key: str = "source") -> None:
        if isinstance(slot, dict) and isinstance(value := slot.get(key), str) and value:
            path = Path(value).expanduser()
            slot[key] = str((root / path).resolve())
    resolve_slot(spec.get("caption_sfx"), "draft_path")
    if isinstance(spec.get("broll"), list):
        for insert in spec["broll"]:
            resolve_slot(insert)
    bgm = spec.get("bgm")
    resolve_slot(bgm)
    if isinstance(bgm, dict):
        resolve_slot(bgm.get("jianying_audio"), "draft_path")
    for beat in spec.get("beats") or []:
        if isinstance(beat, dict):
            resolve_slot(beat.get("caption_sfx"), "draft_path")
            resolve_slot(beat)
            resolve_slot(beat.get("voice"))
            layout = beat.get("layout")
            secondary = layout.get("with") if isinstance(layout, dict) else None
            if isinstance(secondary, dict):
                resolve_slot(secondary.get("media"))
    return spec


@dataclass(frozen=True)
class Beat:
    kind: str
    frames: int
    seconds: float
    shot: dict[str, Any]
    label: str
    recipe: str | None = None
    at: float = 0.0
    layout: dict[str, Any] = field(default_factory=dict)


def _number(value: Any, label: str, *, minimum: float = 0, maximum: float = 3600) -> float:
    if type(value) not in (int, float) or not minimum <= value <= maximum:
        raise ProductionError(f"{label} 需为 {minimum}–{maximum} 之间的数字")
    return float(value)


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"[\s，。！？、,.!?;；:：\"'“”‘’（）()《》—\-…]+", "", text)


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _normalise(left), _normalise(right)).ratio()


def transcript_pool(settings: Settings, batch_id: str | None = None) -> list[dict[str, Any]]:
    """Every cached source with its timed transcript, ready for take selection."""
    root = settings.work_root / "video_analyses"
    if batch_id is None:
        reports = sorted(root.glob("*/analysis.json"))
    else:
        reports = [Path(row["analysis_report_path"]) for row in load_batch_catalog(settings, batch_id)["sources"]
                   if row.get("analysis_report_path")]
    best: dict[str, dict[str, Any]] = {}
    for path in reports:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        source = data.get("local_video_path") or data.get("source") or data.get("media_path")
        cues = data.get("transcript") or []
        if not source or not cues:
            continue
        key = str(source)
        if len(cues) > len(best.get(key, {}).get("cues", [])):
            duration = (data.get("metadata") or {}).get("duration_ms") or data.get("duration_ms", 0)
            best[key] = {"source": key, "cues": cues, "duration_ms": duration}
    return sorted(best.values(), key=lambda item: item["source"])


def select_take(
    settings: Settings,
    line: str,
    *,
    pool: list[dict[str, Any]] | None = None,
    batch_id: str | None = None,
    avoid: set[str] | None = None,
    min_score: float = 0.62,
    padding: float = 0.06,
) -> dict[str, Any]:
    """Find the source segment that says ``line`` most cleanly."""
    if not isinstance(line, str) or not line.strip():
        raise ProductionError("line 不能为空")
    candidates = transcript_pool(settings, batch_id) if pool is None else pool
    if not candidates:
        raise ProductionError("没有可用的转写缓存，请先运行 analyze_media_batch")
    avoid = avoid or set()
    ranked = []
    for item in candidates:
        source = item["source"]
        penalty = 0.08 if source in avoid else 0.0
        for cue in item["cues"]:
            score = _similarity(line, cue.get("text", "")) - penalty
            if score >= min_score:
                end = cue["end_ms"] / 1000 + padding
                if item.get("duration_ms"):
                    end = min(end, item["duration_ms"] / 1000)
                ranked.append({
                    "source": source,
                    "start": max(0.0, cue["start_ms"] / 1000 - padding),
                    "end": end,
                    "text": cue.get("text", ""),
                    "score": round(score, 3),
                })
    if not ranked:
        raise ProductionError(f"没有找到与「{line[:24]}」匹配的口播（阈值 {min_score}）")
    ranked.sort(key=lambda item: (-item["score"], item["end"] - item["start"], item["source"], item["start"]))
    return ranked[0]


def cues_between(settings: Settings, source: str, start: float, end: float) -> list[dict[str, Any]]:
    """Timed cues of one source inside a source-time window."""
    for item in transcript_pool(settings):
        if item["source"] != source:
            continue
        return [
            cue for cue in item["cues"]
            if cue["end_ms"] / 1000 > start + 1e-3 and cue["start_ms"] / 1000 < end - 1e-3
        ]
    return []


def _frame_cues(
    pieces: list[tuple[float, float, str]], *, fps: int, highlights: list[str] | None, duck: bool,
) -> list[dict[str, Any]]:
    cues = []
    for start, end, text in pieces:
        first = round(start * fps)
        cue: dict[str, Any] = {"start_frame": first, "end_frame": max(first + 1, round(end * fps)), "text": text}
        terms = [term for term in highlights or [] if term in text]
        if terms:
            cue["highlights"] = terms
        if duck:
            cue["duck_bgm"] = True
        cues.append(cue)
    return cues


def text_cues(
    text: str, *, offset: float, seconds: float, fps: int, max_chars: int = 14,
    punctuation: CaptionPunctuation = "spaced", highlights: list[str] | None = None, duck: bool = False,
    mode: str = "single",
) -> list[dict[str, Any]]:
    if mode not in ("stack", "blocks"):
        pieces = caption_cues(text, offset, offset + seconds, max_chars=max_chars, punctuation=punctuation)
        return _frame_cues(pieces, fps=fps, highlights=highlights, duck=duck)
    runs = semantic_caption_runs(text, max_chars)
    total = sum(max(1, visible_length(p)) for run in runs for p in run)
    consumed, rows = 0, []
    for run in runs:
        group = round((offset + seconds * consumed / total) * fps)
        for part in run:
            start = offset + seconds * consumed / total
            consumed += max(1, visible_length(part))
            end = offset + seconds * consumed / total
            if len(run) == 1:
                pieces = caption_cues(part, start, end, max_chars=max_chars, punctuation=punctuation)
                rows.extend(_frame_cues(pieces, fps=fps, highlights=highlights, duck=duck))
                continue
            shown = tidy_caption(part, punctuation)
            if shown and round(end * fps) > round(start * fps):
                cue = _frame_cues([(start, end, shown)], fps=fps, highlights=highlights, duck=duck)[0]
                if len(run) > 1:
                    cue["caption_group"] = group
                rows.append(cue)
    return rows


def timed_caption_cues(
    entries: Any, first: int, frames: int, fps: int, mode: str, punctuation: CaptionPunctuation,
    highlights: list[str] | None, duck: bool, *, semantic: bool, max_chars: int,
) -> list[dict[str, Any]]:
    """Map manually marked phrase entrances to frames without re-estimating speech from character counts."""
    if not isinstance(entries, list) or not entries:
        raise ProductionError("caption.cues 需要非空数组，每项提供 at 和 text")
    rows = []
    count = 0
    previous_text = ""
    for entry in entries:
        if not isinstance(entry, dict) or entry.keys() != {"at", "text"} or not isinstance(entry["text"], str):
            raise ProductionError("caption.cues 每项需要 at 秒数和 text 字符串")
        local = round(_number(entry["at"], "caption.cues[].at", maximum=frames / fps) * fps)
        start = first + local
        text = tidy_caption(entry["text"], punctuation)
        if not text or local >= frames or (rows and rows[-1]["start_frame"] >= start):
            raise ProductionError("caption.cues 需在拍内按不同帧依次进入，并提供可显示的文字")
        if rows:
            rows[-1]["end_frame"] = start
        row = {"start_frame": start, "end_frame": first + frames, "text": text}
        if duck:
            row["duck_bgm"] = True
        if terms := [term for term in highlights or [] if term in text]:
            row["highlights"] = terms
        if mode in ("stack", "blocks"):
            current = entry["text"].strip()
            related = (not semantic or follows_caption_step(previous_text, current) or
                       (previous_text.endswith(("，", "、", ",")) and
                        any(len(g) > 1 for g in semantic_caption_runs(previous_text + current, max_chars))))
            if rows and related and count < 3:
                row["caption_group"] = rows[-1].setdefault("caption_group", rows[-1]["start_frame"])
                count += 1
            else:
                count = 1
            previous_text = current
        rows.append(row)
    return rows


def transcript_cues(
    cues: list[dict[str, Any]], *, source_start: float, source_end: float, offset: float, fps: int,
    max_chars: int = 14, punctuation: CaptionPunctuation = "spaced",
    highlights: list[str] | None = None, duck: bool = False, mode: str = "single",
) -> list[dict[str, Any]]:
    """Use original ASR cue boundaries and preserve semantic groups before normalizing punctuation."""
    rows = []
    previous_text, previous_group = "", []
    for cue in cues:
        start = max(source_start, cue["start_ms"] / 1000)
        end = min(source_end, cue["end_ms"] / 1000)
        if end <= start:
            continue
        raw = str(cue.get("text", "")).strip()
        new = text_cues(raw, offset=offset + start - source_start, seconds=end - start, fps=fps,
                        max_chars=max_chars, punctuation=punctuation, highlights=highlights, duck=duck, mode=mode)
        if mode in ("stack", "blocks") and len(new) == 1 and visible_length(raw) <= max_chars:
            row = new[0]
            if (previous_group and len(previous_group) < 3 and follows_caption_step(previous_text, raw)
                    and 0 <= row["start_frame"] - previous_group[-1]["end_frame"] <= round(0.45 * fps)):
                previous_group.append(row)
                for member in previous_group:
                    member["caption_group"] = previous_group[0]["start_frame"]
            else:
                previous_group = [row]
            previous_text = raw
        else:
            previous_text, previous_group = "", []
        rows.extend(new)
    return rows


def _graphic_beat(settings: Settings, raw: dict[str, Any], index: int, *, fps: int, width: int,
                  height: int, style_preset: StylePreset | None,
                  seconds_override: float | None = None) -> Beat:
    recipe = raw.get("recipe")
    if recipe not in RECIPES:
        raise ProductionError(f"beats[{index}] 的 recipe 需为 {', '.join(RECIPES)}")
    data = raw.get("data")
    if not isinstance(data, dict):
        raise ProductionError(f"beats[{index}].data 必须是对象")
    seconds = seconds_override or _number(
        raw.get("seconds", RECIPES[recipe].default_duration), f"beats[{index}].seconds",
        minimum=0.6, maximum=30,
    )
    if seconds > 30:
        raise ProductionError(f"beats[{index}] 动效时长最长 30 秒，请拆成多拍")
    rendered = render_motion(
        settings, recipe, data, duration=seconds, fps=fps, width=width, height=height,
        style_preset=style_preset,
    )
    frames = max(1, round(seconds * fps))
    return Beat(
        kind="graphic", frames=frames, seconds=seconds,
        shot={"source": rendered["mp4_path"], "frames": frames},
        label=f"{RECIPES[recipe].label}", recipe=recipe,
    )


def _local_file(value: Any, label: str) -> Path:
    path = Path(str(value or "")).expanduser()
    if not value or not path.is_absolute() or not path.is_file():
        raise ProductionError(f"{label} 需要存在的本地绝对路径")
    return path.resolve()


def _focus(value: Any, label: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2 or any(
        type(part) not in (int, float) or not 0 <= part <= 1 for part in value
    ):
        raise ProductionError(f"{label} 需为 [x, y]，取值 0–1")
    return [float(value[0]), float(value[1])]


def _source_range(raw: dict[str, Any], label: str, *, image: bool) -> tuple[float, float]:
    """Source window of a beat; images and media beats may give ``seconds`` instead."""
    if image:
        return 0.0, _number(raw.get("seconds"), f"{label}.seconds", minimum=0.3, maximum=120)
    start = _number(raw.get("start", 0), f"{label}.start")
    if raw.get("end") is None and raw.get("seconds") is not None:
        return start, start + _number(raw["seconds"], f"{label}.seconds", minimum=0.3, maximum=600)
    end = _number(raw.get("end", 0), f"{label}.end")
    if end <= start:
        raise ProductionError(f"{label} 的 end 必须大于 start")
    return start, end


def compile_split_layout(
    settings: Settings, raw: Any, label: str, *, seconds: float, frames: int, fps: int, width: int,
    height: int, style_preset: StylePreset | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Turn an editorial ``layout`` into a timeline split layout with a rendered secondary panel."""
    if not isinstance(raw, dict):
        raise ProductionError(f"{label} 必须是对象")
    extra = set(raw) - {"kind", "main", "split", "with", "background", "focus"}
    if extra:
        raise ProductionError(f"{label} 不支持 {', '.join(sorted(extra))}；可用 kind、main、split、with、background、focus")
    kind = raw.get("kind")
    if kind not in SPLIT_LAYOUTS:
        raise ProductionError(f"{label}.kind 需为 full、{'、'.join(SPLIT_LAYOUTS)}")
    timeline: dict[str, Any] = {"kind": kind, "main": raw.get("main", "bottom")}
    if raw.get("split") is not None:
        timeline["top_fraction"] = raw["split"]
    if raw.get("background") is not None:
        timeline["background"] = raw["background"]
    if raw.get("focus") is not None:
        timeline["bottom_focus"] = _focus(raw["focus"], f"{label}.focus")
    # Resolve the panel geometry first so the secondary picture can be made to fit it.
    probe = read_interview_layout({**timeline, "top": {"source": "panel"}}, width, height, label)
    _, _, panel_w, panel_h = probe.secondary_rect

    secondary = raw.get("with")
    if not isinstance(secondary, dict) or len(secondary) != 1 or next(iter(secondary)) not in ("graphic", "media"):
        raise ProductionError(f'{label}.with 需为 {{"graphic": {{recipe, data}}}} 或 {{"media": {{source, start?, focus?, fit?}}}}')
    summary: dict[str, Any] = {"layout": kind, "main": timeline["main"], "panel": [panel_w, panel_h]}
    if "graphic" in secondary:
        card = secondary["graphic"]
        if not isinstance(card, dict) or card.get("recipe") not in RECIPES or not isinstance(card.get("data"), dict):
            raise ProductionError(f"{label}.with.graphic 需要 recipe（{', '.join(RECIPES)}）和 data")
        if seconds > MAX_PANEL_GRAPHIC_SECONDS:
            raise ProductionError(f"{label} 分屏动效最长 {MAX_PANEL_GRAPHIC_SECONDS:.0f} 秒，请把这段口播拆成多拍")
        rendered = render_motion(
            settings, card["recipe"], card["data"], duration=seconds, fps=fps,
            width=panel_w, height=panel_h, style_preset=style_preset,
        )
        timeline["top"] = {"source": rendered["mp4_path"]}
        summary["with"] = f"graphic:{card['recipe']}"
    else:
        media = secondary["media"]
        if not isinstance(media, dict) or set(media) - {"source", "start", "focus", "fit"}:
            raise ProductionError(f"{label}.with.media 需要 source，可指定 start、focus、fit")
        path = _local_file(media.get("source"), f"{label}.with.media.source")
        top: dict[str, Any] = {"source": str(path), "fit": media.get("fit", "cover")}
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            top["source_start_frame"] = round(_number(media.get("start", 0), f"{label}.with.media.start") * fps)
            top["source_frames"] = frames
        if media.get("focus") is not None:
            top["focus"] = _focus(media["focus"], f"{label}.with.media.focus")
        timeline["top"] = top
        summary["with"] = "media"
    return timeline, summary



# ---------------------------------------------------------------- captions


def _caption_options(raw: dict[str, Any], spec: dict[str, Any], profile: ProductionStyle | None) -> dict[str, Any]:
    """Per-beat caption presentation, falling back to the episode defaults."""
    raw_caption = raw.get("caption")
    if raw_caption is None:
        raw_caption = {}
    if not isinstance(raw_caption, dict):
        raise ProductionError("beat.caption 必须是对象")
    allowed = {"mode", "size", "zone", "y", "keywords", "cues", "avoid"}
    extra = set(raw_caption) - allowed
    if extra:
        raise ProductionError(f"beat.caption 不支持 {', '.join(sorted(extra))}；可用 mode、size、zone、y、keywords、cues、avoid")
    mode = raw_caption.get("mode", spec.get("caption_mode", "blocks"))
    if mode not in CAPTION_ENTRANCES:
        raise ProductionError(f"caption_mode 支持 {'、'.join(CAPTION_ENTRANCES)}")
    reference = profile and profile.reference_dialogue
    default_size = profile.caption_style["size"] if reference else CAPTION_SIZE
    size = raw_caption.get("size", spec.get("caption_size", default_size))
    if type(size) not in (int, float) or not 5 <= size <= 20:
        raise ProductionError("caption.size 需为 5–20")
    zone = raw_caption.get("zone", spec.get("caption_zone", "auto"))
    if zone not in ("auto", "top", "upper", "lower", "bottom"):
        raise ProductionError("caption.zone 支持 auto、top、upper、lower、bottom")
    keywords = raw_caption.get("keywords", spec.get("caption_keywords")) or []
    if not isinstance(keywords, list) or any(not isinstance(term, str) or not term for term in keywords):
        raise ProductionError("caption_keywords 需为文字数组")
    y = raw_caption.get("y")
    layout = raw.get("layout") or {}
    if (reference and y is None and "zone" not in raw_caption and "caption_zone" not in spec
            and raw.get("kind", "talk") == "talk" and layout.get("kind", "full") == "full"):
        y = profile.caption_position
    follow_speaker = raw_caption.get("y") is None and zone == "auto"
    avoid = raw_caption.get("avoid", [])
    if not isinstance(avoid, list) or any(
        not isinstance(box, list) or len(box) != 4 or any(type(v) not in (int, float) for v in box)
        or not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1) for box in avoid
    ):
        raise ProductionError("caption.avoid 应为成片画面归一化的矩形数组，每项为 [左,上,右,下]")
    if avoid and not follow_speaker:
        raise ProductionError("caption.avoid 请搭配自动落位，或去掉手动固定位置")
    return {"mode": mode, "authored_mode": "mode" in raw_caption or "caption_mode" in spec,
            "size": float(size), "zone": zone, "y": y, "keywords": keywords,
            "follow_speaker": follow_speaker, "avoid": avoid}


def _anchor_for(
    settings: Settings, cache: dict[tuple[str, float, float], Any], options: dict[str, Any],
    raw: dict[str, Any], *, start: float, end: float, source: str | None,
) -> dict[str, Any]:
    zone = options["zone"]
    if options["y"] is not None:
        y = _number(options["y"], "caption.y", minimum=-0.95, maximum=0.95)
        return {"zone": "custom", "transform_y": y}
    if zone != "auto":
        return zone_anchor(zone).as_dict()
    if isinstance(raw.get("layout"), dict) and raw["layout"].get("kind", "full") != "full":
        # The speaker shares the frame with a card; a caption in the card half
        # would cover the card, so stay in the speaker's band.
        return zone_anchor("upper" if raw["layout"].get("main") == "top" else "bottom").as_dict()
    if raw.get("kind") == "graphic" or source is None:
        return zone_anchor("bottom").as_dict()
    if settings.ffprobe_exe is None or settings.ffmpeg_exe is None:
        return zone_anchor("bottom").as_dict()
    window = (source, start, end)
    if window not in cache:
        cache[window] = choose_caption_anchor(settings, source, start, end)
    found = cache[window]
    return (found or zone_anchor("bottom")).as_dict()


def _caption_terms(raw: dict[str, Any], options: dict[str, Any]) -> list[str]:
    """Words to colour: explicit beats keywords, episode keywords, and split-panel points."""
    terms = [*options["keywords"], *(raw.get("highlights") or [])]
    layout = raw.get("layout")
    if isinstance(layout, dict):
        with_panel = layout.get("with")
        graphic = with_panel.get("graphic") if isinstance(with_panel, dict) else None
        data = graphic.get("data") if isinstance(graphic, dict) else None
        if isinstance(data, dict):
            for item in data.get("items") or []:
                if isinstance(item, dict) and isinstance(item.get("title"), str):
                    terms.append(item["title"])
                elif isinstance(item, str):
                    terms.append(item)
            if isinstance(data.get("title"), str):
                terms.append(data["title"])
    return list(dict.fromkeys(term.strip() for term in terms if isinstance(term, str) and term.strip()))


def _caption_items(
    entities: list[dict[str, Any]], *, fps: int, anchor: dict[str, Any], width: int, height: int,
    size: float, terms: list[str], total_frames: int, profile: Any, speaker_windows: list[tuple] | None = None,
    use_srt_style: bool = False, adaptive: bool = True, avoid: list | None = None,
) -> list[dict[str, Any]]:
    items = []
    layouts = {}
    for entity in entities:
        first = round(entity["start"] * fps)
        last = min(total_frames, round(entity["end"] * fps))
        if last <= first:
            continue
        x, y = 0, anchor["transform_y"]
        mode = entity["anim"]
        if "siblings" in entity:
            key = (tuple(entity["siblings"]), mode)
            if key not in layouts:
                layouts[key] = fixed_caption_slots(key[0], mode, width, height, size, y, bool(profile and profile.serif))
            if layouts[key]:
                x, y = layouts[key][entity["slot"]]
            else:
                mode = "single"
                last = min(last, round(entity["cue_end"] * fps))
                if last <= first:
                    continue
        native, portable = CAPTION_ENTRANCES[mode]
        duration = round(last / fps * 1_000_000) - round(first / fps * 1_000_000)
        entrance = min(native["duration"], max(1, duration // 2))
        item = {
            "_adaptive_caption": adaptive,
            "_caption_exclusions": avoid or [],
            "text": entity["text"], "start": round(first / fps * 1_000_000), "duration": duration,
            "style": {"size": size, "align": 1, "bold": True, "auto_wrapping": True, "max_line_width": 0.9},
            "clip": {"transform_x": x, "transform_y": y},
            "animations": [{**native, "duration": entrance}],
            "portable_animation": {"kind": portable, "duration_us": entrance},
        }
        if profile:
            item["font"] = profile.font
            if profile.caption_decoration.get("border"):
                item["border"] = dict(profile.caption_decoration["border"])
            item["portable_serif"] = profile.serif
        if use_srt_style:
            # Preserve the recipe's normal SRT appearance when it needs independently placed text segments.
            item.pop("animations")
            item.pop("portable_animation")
            item["style"]["max_line_width"] = 0.85
            item["style"].update(profile.caption_style)
            item.update(profile.caption_decoration)
            if profile.caption_entrance:
                item["portable_animation"] = profile.caption_entrance
        if ranges := emphasis_ranges(entity["text"], terms):
            item["highlight_ranges"] = [list(span) for span in ranges]
        if profile and profile.reference_dialogue:
            item["shadow"] = dict(profile.caption_decoration["shadow"])
            item["style"]["color"] = [1, 1, 1]
            if ranges:
                item["highlight_color"] = list(SELLER_HIGHLIGHT)
            if mode == "single" and not ranges:
                # Ordinary speech cuts on with the voice. Emphasis and cumulative text retain their entrances.
                del item["animations"], item["portable_animation"]
        overlaps = [(max(first, a), min(last, b), rect) for a, b, rect in (speaker_windows or [])
                    if a < last and first < b]
        if not overlaps:
            items.append(item)
            continue
        boundaries = sorted({first, last, *(edge for a, b, _ in overlaps for edge in (a, b))})
        for a, b in zip(boundaries, boundaries[1:]):
            piece = item.copy()
            panel = next((rect for left, right, rect in overlaps if left <= a < right), None)
            if panel is not None:
                left, top, w, h = panel
                px, py = 0.0, -0.60  # Single-line centre at 80% of the speaker panel's height.
                if "siblings" in entity and mode in ("blocks", "stack"):
                    key = (tuple(entity["siblings"]), mode, w, h)
                    if key not in layouts:
                        measure = size * min(width, height) / min(w, h)
                        layouts[key] = fixed_caption_slots(key[0], mode, w, h, measure, py, bool(profile and profile.serif))
                    if layouts[key]:
                        px, py = layouts[key][entity["slot"]]
                    else:
                        # A small panel can accommodate the active phrase on its own.
                        b = min(b, round(entity["cue_end"] * fps))
                        if b <= a:
                            continue
                piece["clip"] = {"transform_x": (2 * left + (1 + px) * w) / width - 1,
                                 "transform_y": 1 - (2 * top + (1 - py) * h) / height}
                piece["style"] = {**item["style"], "max_line_width": item["style"]["max_line_width"] * w / width}
            piece["start"] = round(a / fps * 1_000_000)
            piece["duration"] = round(b / fps * 1_000_000) - piece["start"]
            if a > first:
                piece.pop("animations", None)
                piece.pop("portable_animation", None)
            else:
                if "animations" in piece:
                    piece["animations"] = [{**anim, "duration": min(piece["duration"], anim["duration"])}
                                           for anim in item["animations"]]
                if "portable_animation" in piece:
                    anim = item["portable_animation"]
                    piece["portable_animation"] = {**anim, "duration_us": min(piece["duration"], anim["duration_us"])}
            items.append(piece)
    return items


def _transitions(
    beats: list[Beat], raw_beats: list[dict[str, Any]], policy: str, profile: Any, fps: int,
) -> None:
    """Connect cuts so sections do not jump: a short native transition, doubled in OpenFilm."""
    if policy == "none" or profile is None:
        return
    for index, beat in enumerate(beats[:-1]):
        raw = raw_beats[index]
        explicit = raw.get("transition")
        if explicit is False:
            continue
        following = raw_beats[index + 1]
        kind_change = beats[index + 1].kind != beat.kind
        section_start = bool(following.get("chapter"))
        if explicit is True or isinstance(explicit, dict) or policy == "all" or kind_change or section_start:
            if isinstance(explicit, dict):
                continue  # a full specification is passed through untouched
            seconds = min(profile.transition_half_seconds * 2, 0.4)
            frames = min(max(2, round(seconds * fps)), beat.frames, beats[index + 1].frames)
            duration = round(frames / fps * 1_000_000)
            beat.shot.setdefault("transition", {"name": profile.transition, "duration": duration})
            portable_frames = min(frames, beat.frames, beats[index + 1].frames)
            if portable_frames >= 2 and "portable_transition" not in beat.shot:
                beat.shot["portable_transition"] = {
                    "kind": profile.portable_transition, "frames": portable_frames - portable_frames % 2,
                    "color": profile.transition_color,
                }


def _is_rotated(settings: Settings, path: Path) -> bool:
    """Quarter-turn display rotation; without FFmpeg tools nothing can be baked anyway."""
    if settings.ffprobe_exe is None or settings.ffmpeg_exe is None:
        return False
    return needs_upright_copy(parse_media_info(settings, str(path))["media_info"])


def _circle_overlay(settings: Settings, raw: Any, frames: int, fps: int, width: int, height: int) -> dict:
    if not isinstance(raw, dict) or raw.keys() - {"at", "duration", "center", "diameter"}:
        raise ProductionError("circle_mask 需要对象，可指定 at、duration、center、diameter")
    seconds = frames / fps
    at = _number(raw.get("at", 0), "circle_mask.at", maximum=seconds)
    duration = _number(raw.get("duration", seconds - at), "circle_mask.duration", maximum=seconds)
    first, last = round(at * fps), round((at + duration) * fps)
    if first >= last or last > frames:
        raise ProductionError("circle_mask 需覆盖本拍内至少一帧")
    center = _focus(raw.get("center", [0.5, 0.35]), "circle_mask.center")
    diameter = _number(raw.get("diameter", 0.72), "circle_mask.diameter", minimum=0.01, maximum=1)
    return {"source": str(circle_matte(settings, width, height, center, diameter)),
            "offset_frame": first, "frames": last - first, "volume": 0}


def _footage_beat(
    settings: Settings, raw: dict[str, Any], index: int, kind: str, *, fps: int, width: int, height: int,
    style_preset: StylePreset | None, upright: bool,
) -> Beat:
    label = f"beats[{index}]"
    path = _local_file(raw.get("source"), f"{label}.source")
    image = path.suffix.lower() in IMAGE_SUFFIXES
    if image and kind == "talk":
        raise ProductionError(f"{label} 口播需要视频素材；静态图片请用 kind=media")
    start, end = _source_range(raw, label, image=image)
    seconds = end - start
    frames = max(1, round(seconds * fps))
    layout = raw.get("layout")
    split = isinstance(layout, dict) and layout.get("kind", "full") != "full"
    shot: dict[str, Any] = {"source": str(path), "frames": frames}
    if not image:
        source_start = start
        if upright and not split and _is_rotated(settings, path):
            # Draft libraries register rotated phone/camera clips by coded size, so the
            # editor would fit them sideways. Hand it an upright copy of just this range.
            clip = editing_clip(settings, path, start, end, width=width, height=height, fps=fps)
            shot["source"] = clip["path"]
            shot["_original_take"] = (str(path), round(start * fps))
            source_start = clip["offset"]
        shot.update(source_start_frame=round(source_start * fps), source_frames=frames)
    volume = raw.get("volume", None if kind == "talk" else 0)
    if volume is not None:
        shot["volume"] = float(_number(volume, f"{label}.volume", maximum=2))
    summary: dict[str, Any] = {}
    if split:
        shot["layout"], summary = compile_split_layout(
            settings, layout, f"{label}.layout", seconds=seconds, frames=frames, fps=fps,
            width=width, height=height, style_preset=style_preset,
        )
    elif layout is not None and (not isinstance(layout, dict) or set(layout) - {"kind"}):
        raise ProductionError(f"{label}.layout 全屏时只需 {{\"kind\": \"full\"}}")
    if "circle_mask" in raw:
        shot["overlays"] = [_circle_overlay(settings, raw["circle_mask"], frames, fps, width, height)]
    return Beat(
        kind=kind, frames=frames, seconds=seconds, shot=shot,
        label=("镜头" if kind == "talk" else "素材") + f" {index}", layout=summary,
    )



def _voice_range(
    settings: Settings, raw: Any, label: str,
) -> dict[str, Any]:
    """A summary card speaks the original take instead of showing it.

    ``{"source": ..., "start": ..., "end": ..., "text"?: ...}`` yields a WAV of just
    that sentence plus the caption source, so the card can layer points on the voice.
    """
    if not isinstance(raw, dict) or set(raw) - {"source", "start", "end", "text", "volume"}:
        raise ProductionError(f"{label}.voice 需要 source、start、end，可选 text 和 volume")
    source = _local_file(raw.get("source"), f"{label}.voice.source")
    start, end = _source_range(raw, f"{label}.voice", image=False)
    clip = extract_audio_range(settings, source, start, end)
    return {
        "source": str(source), "start": start, "end": end, "seconds": end - start,
        "text": raw.get("text"), "volume": raw.get("volume", 1.0), "audio": clip["path"],
    }


def check_structure(beats: list[Any]) -> None:
    """Enforce the editorial rules that keep cards from restating the speaker.

    Section transitions are title-only chapter cards (enforced by the recipe).
    Recap cards (list, steps, checklist, compare) repeat what was just said, so as
    full-screen beats they are only allowed in the summary section, which starts
    at the first beat marked ``role: "summary"``. Inside a section the same card
    belongs in a split panel next to the speaker (``layout.with.graphic``).
    """
    in_summary = False
    for index, raw in enumerate(beats):
        if not isinstance(raw, dict):
            continue
        role = raw.get("role")
        if role is not None and role not in BEAT_ROLES:
            raise ProductionError(f"beats[{index}].role 仅支持 {'、'.join(BEAT_ROLES)}")
        in_summary = in_summary or role == "summary"
        if raw.get("kind") == "graphic" and raw.get("recipe") in RECAP_RECIPES and not in_summary:
            raise ProductionError(
                f"beats[{index}] 是全屏 {raw['recipe']} 卡，会把刚讲过的内容再复述一遍。"
                "小节里的要点请放进口播分屏随讲随出（talk 的 layout.with.graphic，items[].at 对齐口播）；"
                "全屏要点卡只用于总结段，在总结段第一拍标 role:\"summary\"。"
            )


def estimate_seconds(spec: dict[str, Any]) -> float:
    """Cheap duration estimate from the beat list, without rendering anything."""
    total = 0.0
    for raw in spec.get("beats") or []:
        if not isinstance(raw, dict):
            continue
        if raw.get("kind", "talk") == "graphic":
            recipe = raw.get("recipe")
            total += float(raw.get("seconds") or RECIPES[recipe].default_duration) if recipe in RECIPES else 0.0
        elif raw.get("end") is None and raw.get("seconds") is not None:
            total += float(raw["seconds"])
        else:
            total += max(0.0, float(raw.get("end", 0)) - float(raw.get("start", 0)))
    return total


def _music_bed(settings: Settings, music: Any, seconds: float) -> dict[str, Any] | None:
    if music is None:
        return None
    if not isinstance(music, dict) or set(music) - {"preset", "bpm", "root_note", "volume", "duck_volume"}:
        raise ProductionError("music 可指定 preset、bpm、root_note、volume、duck_volume")
    try:
        bed = create_music_bed(
            settings, max(1.0, seconds), preset=music.get("preset", "calm"),
            bpm=music.get("bpm", 92), root_note=music.get("root_note", 60),
        )
    except ValueError as exc:
        raise ProductionError(f"music: {exc}") from exc
    bgm: dict[str, Any] = {"source": bed["audio_path"], "loop": False}
    for key in ("volume", "duck_volume"):
        if music.get(key) is not None:
            bgm[key] = music[key]
    return bgm


def compile_episode(
    settings: Settings,
    spec: dict[str, Any],
    *,
    preview_frames: int = 0,
    mixdown: bool = False,
    include_manifest: bool = False,
    openfilm_project: bool = False,
    include_review_guidance: bool = False,
) -> dict[str, Any]:
    """Compile an episode spec into a draft manifest, captions and a sound bed."""
    if not isinstance(spec, dict):
        raise ProductionError("spec 必须是对象")
    name = spec.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ProductionError("spec.name 不能为空")
    fps = spec.get("fps", 30)
    width = spec.get("width", 1080)
    height = spec.get("height", 1920)
    validate_canvas(width, height)
    if type(fps) is not int or not 1 <= fps <= 120:
        raise ProductionError("fps 需为 1–120 的整数")
    style_preset: StylePreset | None = spec.get("style_preset")
    profile = get_production_style(style_preset)
    reference = bool(profile and profile.reference_dialogue)
    raw_beats = spec.get("beats")
    if not isinstance(raw_beats, list) or not raw_beats or len(raw_beats) > MAX_BEATS:
        raise ProductionError(f"beats 需要 1–{MAX_BEATS} 个分镜")
    caption_theme: CaptionTheme | None = spec.get("caption_theme")
    punctuation = spec.get("caption_punctuation", "spaced")
    if punctuation not in PUNCTUATION_MODES:
        raise ProductionError(f"caption_punctuation 支持 {'、'.join(PUNCTUATION_MODES)}")
    max_chars = spec.get("caption_max_chars", 14 if height >= width else 20)
    if type(max_chars) is not int or not 6 <= max_chars <= 40:
        raise ProductionError("caption_max_chars 需为 6–40 的整数")
    source_proxy = spec.get("source_proxy", "auto")
    if source_proxy not in ("auto", "off"):
        raise ProductionError("source_proxy 需为 auto 或 off")
    check_structure(raw_beats)
    transitions_policy = spec.get("transitions", "none" if reference else "auto")
    if transitions_policy not in TRANSITION_POLICIES:
        raise ProductionError(f"transitions 支持 {'、'.join(TRANSITION_POLICIES)}")

    beats: list[Beat] = []
    events: list[dict[str, Any]] = []
    item_accents: list[dict[str, Any]] = []
    captions: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    narration: list[dict[str, Any]] = []
    anchors: dict[tuple[str, float, float], Any] = {}
    caption_zones: dict[int, str] = {}
    small_captions: set[float] = set()
    total_frames = 0
    for index, raw in enumerate(raw_beats):
        if not isinstance(raw, dict):
            raise ProductionError(f"beats[{index}] 必须是对象")
        kind = raw.get("kind", "talk")
        if kind not in BEAT_KINDS:
            raise ProductionError(f"beats[{index}].kind 需为 {'、'.join(BEAT_KINDS)}")
        if kind == "graphic" and "circle_mask" in raw:
            raise ProductionError("circle_mask 用在口播或素材分镜")
        voice = _voice_range(settings, raw["voice"], f"beats[{index}]") if (
            kind == "graphic" and raw.get("voice") not in (None, False)) else None
        if kind == "graphic":
            beat = _graphic_beat(
                settings, raw, index, fps=fps, width=width, height=height, style_preset=style_preset,
                seconds_override=voice["seconds"] if voice else None,
            )
        else:
            beat = _footage_beat(
                settings, raw, index, kind, fps=fps, width=width, height=height,
                style_preset=style_preset, upright=source_proxy == "auto",
            )
        beat = replace(beat, at=total_frames / fps, seconds=beat.frames / fps)
        total_frames += beat.frames
        if raw.get("chapter"):
            beat.shot["chapter"] = raw["chapter"]  # Editorial marker for transitions only.
        if isinstance(raw.get("transition"), dict):
            beat.shot["transition"] = raw["transition"]
        beats.append(beat)
        if raw.get("sfx") is not False:
            events.append({
                "kind": beat.recipe or kind, "at": beat.at,
                "sfx": raw.get("sfx", ""), "gain_db": raw.get("sfx_gain_db", -6.0),
            })
            if reference and raw.get("caption_sfx", spec.get("caption_sfx", True)) is not False:
                card = raw if kind == "graphic" else ((raw.get("layout") or {}).get("with") or {}).get("graphic", {})
                if card:
                    item_accents.extend(item_accent_tracks(settings, card["recipe"], card["data"],
                                                          total_frames - beat.frames, beat.frames, fps))
        caption_options = _caption_options(raw, spec, profile)
        caption_mode = caption_options["mode"]
        group_cues: list[dict[str, Any]] = []
        if voice is not None:
            narration.append({
                "source": voice["audio"], "start_frame": round(beat.at * fps),
                "frames": max(1, round(beat.seconds * fps)), "volume": float(voice["volume"]),
            })
        authored = (raw.get("caption") or {}).get("cues")
        if authored is not None:
            if not raw.get("captions", True):
                raise ProductionError("caption.cues 与 captions=false 不能同时使用")
            group_cues = timed_caption_cues(authored, total_frames - beat.frames, beat.frames, fps, caption_mode,
                                            punctuation, raw.get("highlights"), kind == "talk" or voice is not None,
                                            semantic=not caption_options["authored_mode"], max_chars=max_chars)
        elif voice is not None:
            start, end = voice["start"], voice["end"]
            spoken = str(voice["text"]) if voice["text"] else ""
            group_cues = text_cues(spoken, offset=beat.at, seconds=beat.seconds, **{
                "fps": fps, "max_chars": max_chars, "punctuation": punctuation,
                "highlights": raw.get("highlights"), "duck": True, "mode": caption_mode,
            }) if spoken else transcript_cues(
                cues_between(settings, voice["source"], start, end),
                source_start=start, source_end=end, offset=beat.at,
                fps=fps, max_chars=max_chars, punctuation=punctuation,
                highlights=raw.get("highlights"), duck=True, mode=caption_mode,
            )
        elif kind != "graphic" and raw.get("captions", True):
            options = {
                "fps": fps, "max_chars": max_chars, "punctuation": punctuation,
                "highlights": raw.get("highlights"), "duck": kind == "talk", "mode": caption_mode,
            }
            if raw.get("text"):
                group_cues = text_cues(str(raw["text"]), offset=beat.at, seconds=beat.seconds, **options)
            elif kind == "talk":
                start, end = _source_range(raw, f"beats[{index}]", image=False)
                source = str(Path(str(raw["source"])).resolve())
                group_cues = transcript_cues(
                    cues_between(settings, source, start, end),
                    source_start=start, source_end=end, offset=beat.at, **options,
                )
        if group_cues:
            group_cues = [cue for cue in group_cues if cue["start_frame"] < total_frames]
            for cue in group_cues:
                cue["end_frame"] = min(cue["end_frame"], total_frames)
            options = caption_options
            if not options["authored_mode"] and not any(c.get("caption_group") is not None for c in group_cues):
                options["mode"] = "single"
            if width < height and options["size"] < 11:
                small_captions.add(options["size"])
            source_start = voice["start"] if voice else float(raw.get("start", 0))
            anchor = _anchor_for(
                settings, anchors, options, raw, start=source_start, end=source_start + beat.seconds,
                source=voice["source"] if voice else (str(raw.get("source")) if kind != "graphic" else None),
            )
            caption_zones[index] = anchor["zone"]
            groups.append({
                "beat": index + 1, "cues": group_cues, "options": options, "anchor": anchor,
                "timing": ("按分镜指定入点" if authored is not None else
                           "按文长估时，请回听" if (voice["text"] if voice else raw.get("text")) else "按带时码转写分句"),
                "terms": _caption_terms(raw, options),
                "accent_terms": [*options["keywords"], *(raw.get("highlights") or [])],
                "accent": raw.get("caption_sfx", spec.get("caption_sfx", True)) if raw.get("sfx") is not False else False,
            })
            captions.extend(group_cues)

    shots = [beat.shot for beat in beats]
    duration = total_frames / fps
    _transitions(beats, raw_beats, transitions_policy, profile, fps)
    audio: list[dict[str, Any]] = []
    for placement in plan_sound(events):
        asset = synthesize_sfx(settings, placement["name"], gain_db=placement["gain_db"])
        start = round(placement["at"] * fps)
        if start >= total_frames:
            continue
        frames = max(1, min(int(asset["seconds"] * fps), total_frames - start))
        audio.append({
            "source": asset["path"],
            "start_frame": start,
            "frames": frames,
            "volume": 1.0,
        })

    occupied = [s["start_frame"] for s in audio]
    item_accents = [s for s in item_accents if all(abs(s["start_frame"] - at) > 0.15 * fps for at in occupied)]
    audio.extend(item_accents)
    accents = caption_accent_tracks(settings, [(g["cues"], g["accent_terms"], g["accent"]) for g in groups],
                                  fps, total_frames, [s["start_frame"] for s in audio],
                                  reference_dialogue=reference)
    audio.extend(accents)
    for cue in captions:
        cue["end_frame"] = max(cue["start_frame"] + 1, min(cue["end_frame"], total_frames))
    captions = [cue for cue in captions if cue["start_frame"] < total_frames]
    caption_items: list[dict[str, Any]] = []
    speaker_windows = speaker_panel_windows(spec.get("broll"), fps=fps, width=width, height=height)
    for beat in beats:
        if layout := beat.shot.get("layout"):
            panel = read_interview_layout(layout, width, height, "beat")
            if panel:
                first = round(beat.at * fps)
                speaker_windows.append((first, first + beat.frames, panel.main_rect))
    for group in groups:
        entities = caption_entities(
            [(cue["start_frame"] / fps, cue["end_frame"] / fps, cue["text"]) for cue in group["cues"]],
            mode=group["options"]["mode"], groups=[cue.get("caption_group") for cue in group["cues"]],
        )
        caption_items.extend(_caption_items(
            entities, fps=fps, anchor=group["anchor"], size=group["options"]["size"], width=width, height=height,
            terms=group["terms"], total_frames=total_frames, profile=profile,
            speaker_windows=speaker_windows if group["options"]["follow_speaker"] else None,
            adaptive=group["options"]["follow_speaker"],
            avoid=group["options"]["avoid"],
        ))

    bgm = spec.get("bgm")
    if bgm is None:
        bgm = _music_bed(settings, spec.get("music"), duration)
    elif spec.get("music") is not None:
        raise ProductionError("bgm 与 music 只能二选一")

    result = compile_video_timeline(
        settings, name, shots, fps=fps, width=width, height=height,
        audio=audio or None, captions=captions or None,
        narration=narration or None, caption_items=caption_items or None,
        broll=spec.get("broll"),
        min_scene_seconds=spec.get("min_scene_seconds"),
        bgm=bgm, srt_mode=spec.get("srt_mode", "text"),
        caption_style=spec.get("caption_style"), caption_clip=spec.get("caption_clip"),
        preview_frames=preview_frames, mixdown=mixdown, include_manifest=include_manifest,
        openfilm_project=openfilm_project, style_preset=style_preset,
        caption_decoration=spec.get("caption_decoration"),
        portable_caption_animation=spec.get("portable_caption_animation"),
        caption_theme=caption_theme,
    )
    manifest = {
        "beats": [
            {
                "index": index + 1, "kind": beat.kind, "label": beat.label,
                "recipe": beat.recipe, "start_s": round(beat.at, 3),
                "end_s": round(beat.at + beat.seconds, 3), "frames": beat.frames,
                **({"caption_zone": caption_zones[index]} if index in caption_zones else {}),
                **beat.layout,
            }
            for index, beat in enumerate(beats)
        ],
        "duration_s": round(duration, 3),
        **({"broll": result["broll"]} if result.get("broll") else {}),
        "cue_count": len(audio),
        "caption_sfx_frames": [s["start_frame"] for s in accents],
        **({"item_sfx_frames": [s["start_frame"] for s in item_accents]} if reference else {}),
        "caption_count": len(captions),
        "split_beats": sum(bool(beat.layout) for beat in beats),
        "caption_items": len(caption_items),
        "voice_cards": len(narration),
    }
    advice: list[str] = []
    if small_captions:
        sizes = "、".join(f"{size:g}" for size in sorted(small_captions))
        advice.append(
            f"字幕字号 {sizes} 小于竖屏标准 11.5，手机上会偏小。"
            "建议使用 11.5，并按原声停顿调整字幕。"
        )
    if advice:
        result = {**result, "warnings": [*result.get("warnings", []), *advice]}
    if include_review_guidance:
        result["review_guidance"] = episode_review(beats, raw_beats, groups, fps, project=bool(result.get("openfilm_project_path")))
    return {**result, "episode": manifest}
