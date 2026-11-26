from __future__ import annotations

import math
import re
import shutil
from html import escape
from pathlib import Path
from typing import Any

from .managed_fonts import font_face
from .production_graphics import ProductionError


STILL_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".bmp", ".svg"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".ogv"}
CLIP_FIELDS = {"alpha", "transform_x", "transform_y", "rotation", "scale_x", "scale_y", "flip_horizontal", "flip_vertical"}
TEXT_FIELDS = {"size", "bold", "italic", "underline", "color", "alpha", "align", "auto_wrapping", "max_line_width", "vertical", "letter_spacing", "line_spacing"}
FONT_STACK = '"Noto Sans SC", "Microsoft YaHei", "Noto Sans CJK SC", "PingFang SC", Arial, sans-serif'
SERIF_STACK = '"Noto Serif SC", "Noto Serif CJK SC", SimSun, Songti SC, Georgia, serif'
HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def _options(value: Any, allowed: set[str], label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProductionError(f"OpenFilm {label} 必须是对象")
    extra = value.keys() - allowed
    if extra:
        raise ProductionError(f"OpenFilm 项目暂不支持 {label} 中的 {', '.join(sorted(extra))}")
    return value


def _number(value: Any, label: str, *, minimum: float = -1e6, maximum: float = 1e6) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ProductionError(f"OpenFilm {label} 数值超出范围")
    return value


def _placement(value: Any) -> tuple[float, float, float, float, float, float]:
    clip = _options(value, CLIP_FIELDS, "clip")
    if clip.get("flip_horizontal") or clip.get("flip_vertical"):
        raise ProductionError("OpenFilm 项目暂不支持剪映的片段翻转")
    return (
        _number(clip.get("transform_x", 0), "clip.transform_x"),
        _number(clip.get("transform_y", 0), "clip.transform_y"),
        _number(clip.get("rotation", 0), "clip.rotation"),
        _number(clip.get("scale_x", 1), "clip.scale_x", minimum=0.001),
        _number(clip.get("scale_y", 1), "clip.scale_y", minimum=0.001),
        _number(clip.get("alpha", 1), "clip.alpha", minimum=0, maximum=1),
    )


def _video_css(clip: dict[str, Any], width: int, height: int) -> str:
    x, y, rotation, sx, sy, alpha = _placement(clip.get("clip"))
    if "panel_rect" in clip:
        rect = clip["panel_rect"]
        if not isinstance(rect, list) or len(rect) != 4 or any(type(value) is not int for value in rect):
            raise ProductionError("OpenFilm panel_rect 必须是四个整数 [x, y, w, h]")
        left, top, w, h = rect
        if left < 0 or top < 0 or w <= 0 or h <= 0 or left + w > width or top + h > height:
            raise ProductionError("OpenFilm panel_rect 必须位于画布内且宽高大于零")
        rotation = 0
    else:
        if sx != sy:
            raise ProductionError("OpenFilm 项目暂不支持视频或图片使用不同的横纵缩放值")
        w, h = width * sx, height * sy
        left = width * (1 + x) / 2 - w / 2
        top = height * (1 - y) / 2 - h / 2
    return f"left:{left:.5f}px;top:{top:.5f}px;width:{w:.5f}px;height:{h:.5f}px;rotate:{rotation:.5f}deg;opacity:{alpha:.5f}"


def _rgba(rgb: Any, alpha: Any) -> str:
    if not isinstance(rgb, (list, tuple)) or len(rgb) != 3:
        raise ProductionError("OpenFilm 文本颜色必须是三个 0–1 数值")
    color = [round(_number(channel, "color", minimum=0, maximum=1) * 255) for channel in rgb]
    a = _number(alpha, "alpha", minimum=0, maximum=1)
    return f"rgba({color[0]},{color[1]},{color[2]},{a:.5f})"


def _text_css(raw: Any, width: int, height: int, *, serif: bool = False) -> tuple[str, str, float, bool]:
    style = _options(raw, TEXT_FIELDS, "文本样式")
    if style.get("vertical") or style.get("line_spacing") or style.get("letter_spacing"):
        raise ProductionError("OpenFilm 项目暂不支持剪映的竖排文字或非默认行距、字间距")
    align_index = style.get("align", 0)
    if type(align_index) is not int or align_index not in (0, 1, 2):
        raise ProductionError("OpenFilm 文本 align 必须是 0、1 或 2")
    align = ("left", "center", "right")[align_index]
    max_width = _number(style.get("max_line_width", 0.82), "style.max_line_width", minimum=0.01, maximum=1)
    size = _number(style.get("size", 8), "style.size", minimum=0.1, maximum=180) * min(width, height) / 144
    color = _rgba(style.get("color", [1, 1, 1]), style.get("alpha", 1))
    css = [
        f"font-family:{SERIF_STACK if serif else FONT_STACK}", f"font-size:{size:.4f}px", "line-height:1.25", f"color:{color}",
        f"font-weight:{'700' if style.get('bold') else '400'}", f"text-align:{align}",
    ]
    if style.get("italic"):
        css.append("font-style:italic")
    if style.get("underline"):
        css.append("text-decoration:underline")
    return ";".join(css), align, max_width, bool(style.get("auto_wrapping"))


def _decoration_css(border: Any, background: Any, shadow: Any = None) -> list[str]:
    css = []
    if border is not None:
        edge = _options(border, {"color", "alpha", "width"}, "文本描边")
        color = _rgba(edge.get("color", [0, 0, 0]), edge.get("alpha", 1))
        width_em = _number(edge.get("width", 40), "border.width", minimum=0, maximum=100) * 0.002
        css.extend([f"-webkit-text-stroke:{width_em:.5f}em {color}", "paint-order:stroke fill"])
    if background is not None:
        bg = _options(background, {"color", "alpha", "style", "round_radius"}, "文本背景")
        color = bg.get("color")
        if bg.get("style", 1) != 1 or not isinstance(color, str) or not HEX_COLOR.fullmatch(color):
            raise ProductionError("OpenFilm 文本背景仅支持样式 1 和 #RRGGBB 颜色")
        rgb = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        radius = _number(bg.get("round_radius", 0), "background.round_radius", minimum=0, maximum=1)
        css.extend([f"background:{_rgba(rgb, bg.get('alpha', 1))}", f"border-radius:{radius:.4f}em", "padding:0.14em 0.24em"])
    if shadow is not None:
        shadow = _options(shadow, {"color", "alpha", "diffuse", "distance", "angle"}, "文本阴影")
        color = _rgba(shadow.get("color", [0, 0, 0]), shadow.get("alpha", 1))
        distance = _number(shadow.get("distance", 5), "shadow.distance", minimum=0, maximum=100) * 0.018
        blur = _number(shadow.get("diffuse", 15), "shadow.diffuse", minimum=0, maximum=100) * 0.01
        angle = math.radians(_number(shadow.get("angle", -45), "shadow.angle", minimum=-180, maximum=180))
        # Jianying angles use a positive vertical axis above the baseline.
        x, y = distance * math.cos(angle), -distance * math.sin(angle)
        css.append(f"text-shadow:{x:.5f}em {y:.5f}em {blur:.5f}em {color}")
    return css


def _entrance_css(raw: Any, opacity: float, clip_length_ms: int | None = None) -> tuple[list[str], str]:
    if raw is None:
        return [], ""
    animation = {"kind": raw} if isinstance(raw, str) else _options(raw, {"kind", "duration_us"}, "portable_animation")
    kind = animation.get("kind")
    movement = {
        "rise": (";translate:0 .72em", ";translate:0 0"),
        "fade": ("", ""),
        "slide_right": (";translate:-.80em 0", ";translate:0 0"),
        "wipe_in": (";clip-path:inset(0 100% 0 0)", ";clip-path:inset(0 0 0 0)"),
    }
    if not isinstance(kind, str) or kind not in movement:
        raise ProductionError("OpenFilm portable_animation.kind 支持 rise、fade、slide_right 或 wipe_in")
    duration = _number(animation.get("duration_us", 250_000), "portable_animation.duration_us", minimum=1, maximum=5_000_000)
    timing = f"{duration / 1_000_000:.6f}s"
    # Reserve at least half of a short cue or title for its settled, fully legible state.
    if clip_length_ms is None:
        timing = f"min({timing},calc(var(--clip-length,1s) * .5))"
    else:
        timing = f"{min(duration / 1_000_000, clip_length_ms / 2000):.6f}s"
    before, after = movement[kind]
    start = (f"opacity:{opacity:.5f}" if kind == "wipe_in" else "opacity:0") + before
    end = f"opacity:{opacity:.5f}" + after
    rule = f"@keyframes jianying-portable-enter {{ from {{ {start} }} to {{ {end} }} }}"
    return [f"animation:jianying-portable-enter {timing} cubic-bezier(.2,.72,.25,1) both"], rule


def _exit_css(raw: Any, opacity: float, clip_length_ms: int) -> tuple[str, str]:
    if raw is None:
        return "", ""
    animation = {"kind": raw} if isinstance(raw, str) else _options(
        raw, {"kind", "duration_us"}, "portable_exit_animation",
    )
    kind = animation.get("kind")
    movement = {
        "fade": ("", ""),
        "sink": (";translate:0 0", ";translate:0 .72em"),
        "slide_left": (";translate:0 0", ";translate:-.80em 0"),
        "wipe_out": (";clip-path:inset(0 0 0 0)", ";clip-path:inset(0 100% 0 0)"),
    }
    if not isinstance(kind, str) or kind not in movement:
        raise ProductionError("OpenFilm portable_exit_animation.kind 支持 fade、sink、slide_left 或 wipe_out")
    requested = _number(
        animation.get("duration_us", 250_000), "portable_exit_animation.duration_us", minimum=1, maximum=5_000_000,
    )
    duration = min(requested / 1_000_000, clip_length_ms / 2000)
    delay = clip_length_ms / 1000 - duration
    before, after = movement[kind]
    start = f"opacity:{opacity:.5f}" + before
    end = (f"opacity:{opacity:.5f}" if kind == "wipe_out" else "opacity:0") + after
    rule = f"@keyframes jianying-portable-exit {{ from {{ {start} }} to {{ {end} }} }}"
    timing = f"jianying-portable-exit {duration:.6f}s cubic-bezier(.35,0,.76,.4) {delay:.6f}s forwards"
    return timing, rule


def _title_css(item: dict[str, Any], width: int, height: int, length_ms: int) -> tuple[str, str]:
    style, _, max_width, wrap = _text_css(item.get("style"), width, height, serif=item.get("portable_serif") is True)
    x, y, angle, sx, sy, alpha = _placement(item.get("clip"))
    css = [
        style, "position:absolute", f"left:{(1 + x) * 50:.5f}%", f"top:{(1 - y) * 50:.5f}%",
        f"transform:translate(-50%,-50%) rotate({angle:.5f}deg) scale({sx:.5f},{sy:.5f})",
        f"opacity:{alpha:.5f}", "width:max-content", f"white-space:{'pre-wrap' if wrap else 'pre'}",
    ]
    if wrap:
        css.extend([f"max-width:{max_width * 100:.4f}%", "overflow-wrap:anywhere"])
    css.extend(_decoration_css(item.get("border") or None, item.get("background") or None, item.get("shadow") or None))
    entrance_style, entrance_rules = _entrance_css(item.get("portable_animation"), alpha, length_ms)
    exit_timing, exit_rules = _exit_css(item.get("portable_exit_animation"), alpha, length_ms)
    if exit_timing:
        # OpenFilm holds both keyframe animations on the iframe's local time.
        if entrance_style:
            entrance_style[0] += "," + exit_timing
        else:
            entrance_style.append("animation:" + exit_timing)
    return ";".join([*css, *entrance_style]), entrance_rules + exit_rules


def _unsupported(item: dict[str, Any], fields: tuple[str, ...], label: str) -> None:
    extra = [key for key in fields if item.get(key)]
    if extra:
        raise ProductionError(f"OpenFilm 项目暂不支持 {label} 中的 {', '.join(extra)}。可以将效果预渲染成素材，或仅生成剪映清单。")


def _target_ms(microseconds: int) -> int:
    # OpenFilm's edit protocol has millisecond precision. Every cut must be active
    # by the exact frame time, including when adding two floating-point times.
    return max(0, (microseconds - 1) // 1000)


def _decimal(milliseconds: int) -> str:
    return f"{milliseconds // 1000}.{milliseconds % 1000:03d}"


def _tag(name: str, attributes: dict[str, Any], *, void: bool = False) -> str:
    attrs = " ".join(f'{key}="{escape(str(value), quote=True)}"' for key, value in attributes.items())
    return f"<{name} {attrs}>" + ("" if void else f"</{name}>")


def _timestamp(ms: int) -> str:
    minutes, remainder = divmod(ms, 60_000)
    seconds, fraction = divmod(remainder, 1_000)
    hour, minute = divmod(minutes, 60)
    return f"{hour:02d}:{minute:02d}:{seconds:02d}.{fraction:03d}"


def _caption_payload(text: str, ranges: list[tuple[int, int]]) -> str:
    if not ranges:
        return escape(text, quote=False)
    ordered = []
    for pair in ranges:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ProductionError("OpenFilm 字幕高亮范围须使用 [start,end]")
        start, end = pair
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            raise ProductionError("OpenFilm 字幕高亮的字符范围超出正文")
        ordered.append((start, end))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ordered):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = previous_start, max(previous_end, end)
        else:
            merged.append((start, end))
    parts = []
    offset = 0
    for start, end in merged:
        parts.append(escape(text[offset:start], quote=False))
        parts.append(f'<c.jianying-highlight>{escape(text[start:end], quote=False)}</c>')
        offset = end
    parts.append(escape(text[offset:], quote=False))
    return "".join(parts)


def _caption_track(
    root: Path, cues: list[tuple[int, int, str]], style: Any, clip: Any, width: int, height: int,
    decoration: Any, cue_highlights: dict[tuple[int, int, str], list[tuple[int, int]]] | None,
) -> str:
    if not cues:
        return ""
    appearance = _options(
        decoration, {"border", "background", "shadow", "highlight_color", "portable_animation", "serif"}, "caption_decoration",
    )
    css, align, wrap_width, _ = _text_css(style, width, height, serif=appearance.get("serif") is True)
    x, y, angle, sx, sy, alpha = _placement(clip)
    if angle or sx != 1 or sy != 1:
        raise ProductionError("OpenFilm WebVTT 字幕轨暂不支持旋转或裁放。标题轨可使用位置、缩放、旋转。")
    if not -1 < x < 1 or not -1 < y < 1:
        raise ProductionError("OpenFilm WebVTT 字幕位置必须位于画面内")
    position = (1 + x) * 50
    line = (1 - y) * 50
    options = f"line:{line:.2f}%,center position:{position:.2f}%,center size:{wrap_width * 100:.2f}% align:{align}"
    background = appearance.get("background")
    base = "background:transparent;padding:0" if background is False else "background:rgba(0,0,0,0.56)"
    extra = _decoration_css(appearance.get("border"), None if background is False else background, appearance.get("shadow"))
    entrance_style, entrance_rules = _entrance_css(appearance.get("portable_animation"), alpha)
    if entrance_style:
        # OpenFilm uses a .cue-text span. An inline block establishes the transform box for rise.
        extra.extend(["display:inline-block", *entrance_style])
    properties = ";".join([css, f"opacity:{alpha:.4f}", base, *extra])
    data = ["WEBVTT", "", "STYLE", f"::cue {{ {properties}; }}"]
    face = font_face(root, appearance.get("serif") is True)
    if face:
        data.append(face)
    if cue_highlights or "highlight_color" in appearance:
        accent = _rgba(appearance.get("highlight_color", [1, 0.88, 0.28]), 1)
        data.append(f"::cue(.jianying-highlight) {{ color:{accent}; }}")
    if entrance_rules:
        data.append(entrance_rules)
    data.append("")
    for a, b, text in sorted(cues):
        payload = _caption_payload(text, (cue_highlights or {}).get((a, b, text), []))
        data.extend([f"{_timestamp(_target_ms(a))} --> {_timestamp(_target_ms(b))} {options}", payload, ""])
    (root / "captions.vtt").write_text("\n".join(data), encoding="utf-8")
    language = "zh-CN" if any(re.search(r"[\u3400-\u9fff]", text) for _, _, text in cues) else "en"
    return "<section>" + _tag("track", {"kind": "captions", "src": "captions.vtt", "srclang": language, "label": "字幕"}, void=True) + "</section>"


def _highlight_span(text: str, ranges: Any, accent: list[Any]) -> str:
    spans: list[tuple[int, int]] = []
    for span in ranges or []:
        if isinstance(span, list) and len(span) == 2 and all(type(i) is int for i in span):
            if 0 <= span[0] < span[1] <= len(text):
                spans.append((span[0], span[1]))
    if not spans:
        return escape(text, quote=False)
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    color = _rgba(accent, 1)
    parts, cursor = [], 0
    for start, end in merged:
        parts.append(escape(text[cursor:start], quote=False))
        parts.append(f'<span style="color:{color}">{escape(text[start:end], quote=False)}</span>')
        cursor = end
    parts.append(escape(text[cursor:], quote=False))
    return "".join(parts)


def _title_page(
    path: Path, text: str, css: str, rules: str, width: int, height: int, *,
    highlights: Any = None, accent: list[Any] | None = None,
) -> None:
    body = _highlight_span(text, highlights, accent or [1, 0.88, 0.28])
    face = font_face(path.parent.parent, SERIF_STACK in css, "../")
    path.write_text(
        '<!doctype html>\n<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width={width}, height={height}">\n'
        '<style>html,body{margin:0;width:100%;height:100%;overflow:hidden;background:transparent}'
        f'{face}{rules}</style>\n'
        f'<div id="title" style="{escape(css, quote=True)}">{body}</div>\n'
        f'<script>window.film={{width:{width},height:{height},frame(){{}}}};</script>\n',
        encoding="utf-8",
    )


def _transition_track(
    scenes: Path, raw: Any, duration_us: int, width: int, height: int,
) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, list):
        raise ProductionError("OpenFilm portable_transitions 必须是数组")
    transitions = []
    for value in raw:
        item = _options(value, {"kind", "start_us", "duration_us", "color"}, "portable_transitions")
        start, length = item.get("start_us"), item.get("duration_us")
        if type(start) is not int or type(length) is not int or start < 0 or length <= 0 or start + length > duration_us:
            raise ProductionError("OpenFilm 可移植转场必须位于时间线内，并使用整数微秒")
        kind, color = item.get("kind"), item.get("color", "#101820")
        if kind not in ("dip", "wipe_left", "wipe_right"):
            raise ProductionError("OpenFilm 可移植转场只支持 dip、wipe_left 或 wipe_right")
        if not isinstance(color, str) or not HEX_COLOR.fullmatch(color):
            raise ProductionError("OpenFilm 可移植转场颜色必须使用 #RRGGBB")
        transitions.append((start, start + length, kind, color))

    tags = []
    previous_end = 0
    for index, (start, end, kind, color) in enumerate(sorted(transitions), start=1):
        if start < previous_end:
            raise ProductionError("OpenFilm 可移植转场区间不能重叠")
        previous_end = end
        start_ms, end_ms = _target_ms(start), _target_ms(end)
        if start_ms == end_ms:
            raise ProductionError("OpenFilm 可移植转场短于项目支持的时间分辨率")
        name = f"portable-transition-{index}"
        offset = (start - start_ms * 1000) / 1_000_000
        span = (end - start) / 1_000_000
        if kind == "dip":
            step = "const a = 1 - Math.abs(2 * p - 1); matte.style.opacity = a * a * (3 - 2 * a);"
        else:
            direction = 1 if kind == "wipe_left" else -1
            step = f"matte.style.translate = `${{{direction} * (1 - 2 * p) * 100}}% 0`;"
        (scenes / f"{name}.html").write_text(
            '<!doctype html>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width={width}, height={height}">\n'
            '<style>html,body{margin:0;overflow:hidden;background:transparent}'
            f'#matte{{position:fixed;inset:0;background:{color}}}</style>\n'
            '<div id="matte" aria-hidden="true"></div>\n<script>\n'
            'const matte = document.getElementById("matte");\n'
            f'window.film = {{width:{width},height:{height},frame(t) {{\n'
            f'  const p = Math.min(1, Math.max(0, (t - {offset:.7f}) / {span:.7f}));\n'
            f'  {step}\n'
            '}};\n</script>\n',
            encoding="utf-8",
        )
        tags.append(_tag("iframe", {"id": name, "at": _decimal(start_ms), "src": f"scenes/{name}.html#t=0,{_decimal(end_ms - start_ms)}"}))
    return '<section title="可移植转场">\n' + "\n".join(tags) + "\n</section>" if tags else ""


def write_openfilm_project(
    folder: Path, manifest: dict[str, Any], kinds: dict[str, bool], cues: list[tuple[int, int, str]],
    mixed_audio: Path, duration_us: int,
    *, portable_transitions: list[dict[str, Any]] | None = None, caption_decoration: dict[str, Any] | None = None,
    cue_highlights: dict[tuple[int, int, str], list[tuple[int, int]]] | None = None,
) -> Path:
    """Write a local OpenFilm 0.4.1 project from a generated draft manifest."""
    stage = manifest["draft"]
    width, height = stage["width"], stage["height"]
    root = folder / "openfilm"
    assets, scenes = root / "assets", root / "scenes"
    assets.mkdir(parents=True)
    scenes.mkdir()
    sources: dict[Path, str] = {}
    sections = []
    transition_section = _transition_track(scenes, portable_transitions, duration_us, width, height)
    # Jianying lists the bottom video first. OpenFilm stacks the first section on top.
    for index, track in reversed(list(enumerate(manifest["tracks"], start=1))):
        track_type = track["type"]
        if track_type == "audio":
            continue
        if track_type == "video" and transition_section:
            sections.append(transition_section)
            transition_section = ""
        tags = []
        for ordinal, clip in enumerate(track["segments"], start=1):
            identifier = f"track-{index}-clip-{ordinal}"
            target_at = _target_ms(clip["start"])
            target_length = _target_ms(clip["start"] + clip["duration"]) - target_at
            attrs = {"id": identifier, "at": _decimal(target_at)}
            if track_type == "text":
                _unsupported(clip, ("font", "animations", "keyframes", "bubble", "flower_text_resource_id"), "文字叠加")
                css, rules = _title_css(clip, width, height, target_length)
                page = scenes / f"{identifier}.html"
                _title_page(
                    page, clip["text"], css, rules, width, height,
                    highlights=clip.get("highlight_ranges"),
                    accent=clip.get("highlight_color", (caption_decoration or {}).get("highlight_color", [1, 0.88, 0.28])),
                )
                attrs["src"] = f"scenes/{page.name}#t=0,{_decimal(target_length)}"
                tags.append(_tag("iframe", attrs))
                continue
            _unsupported(clip, ("transition", "keyframes", "animations", "filter", "effects", "mix_mode", "chroma"), "画面片段")
            source = Path(clip["source"])
            still = kinds[str(source)]
            allowed = STILL_EXTENSIONS if still else VIDEO_EXTENSIONS
            if source.suffix.lower() not in allowed:
                raise ProductionError(f"OpenFilm 项目不支持该素材扩展名和类型: {source.name}")
            relative = sources.get(source)
            if relative is None:
                copy = assets / f"media-{len(sources) + 1:04d}{source.suffix}"
                shutil.copy2(source, copy)
                relative = sources[source] = f"assets/{copy.name}"
            if still:
                attrs["src"] = f"{relative}#t=0,{_decimal(target_length)}"
            else:
                in_ms = clip["source_start"] // 1000
                out_ms = (clip["source_start"] + clip["source_duration"]) // 1000
                playback = (out_ms - in_ms) / target_length
                if not 0.1 <= playback <= 100:
                    raise ProductionError(f"OpenFilm 素材的源区间或变速超出支持范围: {source.name}")
                attrs.update({"src": f"{relative}#t={_decimal(in_ms)},{_decimal(out_ms)}", "volume": "0"})
                if playback != 1:
                    attrs["speed"] = f"{playback:.12f}"
            attrs["style"] = _video_css(clip, width, height)
            tags.append(_tag("img" if still else "video", attrs, void=still))
        sections.append(f'<section title="{escape(track["name"], quote=True)}">\n' + "\n".join(tags) + "\n</section>")

    captions = (manifest.get("srt_imports") or [{}])[0]
    caption_tag = _caption_track(
        root, cues, captions.get("style"), captions.get("clip"), width, height, caption_decoration, cue_highlights,
    )
    if caption_tag:
        sections.insert(0, caption_tag)
    shutil.copy2(mixed_audio, assets / "mix.wav")
    audio = _tag("audio", {"id": "timeline-mix", "src": f"assets/mix.wav#t=0,{_decimal(duration_us // 1000)}", "at": "0"})
    sections.append(f'<section title="试听混音">{audio}</section>')
    (root / "film.html").write_text(
        '<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width={width}, height={height}">\n'
        '</head>\n<body>\n' + "\n".join(sections) + "\n</body>\n</html>\n",
        encoding="utf-8",
    )
    return root
