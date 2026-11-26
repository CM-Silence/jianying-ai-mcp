from __future__ import annotations

import math
import os
import re
import shutil
import tempfile
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .config import Settings
from .managed_fonts import font_path as managed_font
from .production_styles import GraphicShape, StylePreset, get_production_style


GraphicKind = Literal["cover", "title", "data", "end", "lower_third"]
GraphicPreset = Literal["editorial", "midnight", "clean"]

PALETTES = {
    "editorial": {"background": "#F4F2E8", "panel": "#E8E7DC", "foreground": "#192825", "muted": "#455B53", "accent": "#E2F185", "accent_text": "#1D3025"},
    "midnight": {"background": "#14222A", "panel": "#21323B", "foreground": "#F1F5F2", "muted": "#BFCDD1", "accent": "#A6EAC8", "accent_text": "#14222A"},
    "clean": {"background": "#F6F7FA", "panel": "#E8EDF6", "foreground": "#192544", "muted": "#465681", "accent": "#C9D8FA", "accent_text": "#192544"},
}

FONT_PAIRS = {
    "cjk": (
        ("NotoSansSC-VF.ttf", "NotoSansCJK-Regular.ttc", "msyh.ttc", "PingFang.ttc", "Hiragino Sans GB.ttc"),
        ("msyhbd.ttc", "NotoSansCJK-Bold.ttc", "NotoSansSC-VF.ttf", "Hiragino Sans GB.ttc", "PingFang.ttc"),
    ),
    "default": (
        ("segoeui.ttf", "DejaVuSans.ttf", "Arial.ttf", "LiberationSans-Regular.ttf"),
        ("segoeuib.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf", "LiberationSans-Bold.ttf"),
    ),
    "cjk_serif": (
        ("NotoSerifSC-VF.ttf", "NotoSerifCJK-Regular.ttc", "simsun.ttc", "Songti.ttc"),
        ("NotoSerifSC-VF.ttf", "NotoSerifCJK-Bold.ttc", "simsunb.ttf", "Songti.ttc"),
    ),
    "default_serif": (
        ("times.ttf", "DejaVuSerif.ttf", "Times New Roman.ttf", "LiberationSerif-Regular.ttf"),
        ("timesbd.ttf", "DejaVuSerif-Bold.ttf", "Times New Roman Bold.ttf", "LiberationSerif-Bold.ttf"),
    ),
}


class ProductionError(ValueError):
    pass


def absolute_file(value: str, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ProductionError(f"{label}必须是非空的本地绝对文件路径")
    path = Path(value).expanduser()
    if not path.is_absolute() or path.drive.startswith("\\\\"):
        raise ProductionError(f"{label}必须是本地绝对文件路径")
    path = path.resolve(strict=True)
    if path.drive.startswith("\\\\") or not path.is_file():
        raise ProductionError(f"{label}必须指向本地文件")
    return path


def create_output_folder(settings: Settings, category: str) -> Path:
    root = (settings.work_root / "production" / category).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="job-", dir=root))


def validate_canvas(width: int, height: int) -> None:
    if any(type(size) is not int or not 320 <= size <= 3840 for size in (width, height)):
        raise ProductionError("width 和 height 必须是 320–3840 之间的整数")
    if not 1 / 3 <= width / height <= 3:
        raise ProductionError("画幅宽高比必须在 1:3 到 3:1 之间")


def circle_matte(settings: Settings, width: int, height: int, center: list[float], diameter: float) -> Path:
    """An editable still overlay with a transparent circular opening in canvas coordinates."""
    radius = round(min(width, height) * diameter / 2)
    x, y = round(width * center[0]), round(height * center[1])
    alpha = Image.new("L", (width, height), 255)
    ImageDraw.Draw(alpha).ellipse((x - radius, y - radius, x + radius, y + radius), fill=0)
    matte = Image.new("RGBA", alpha.size)
    matte.putalpha(alpha.filter(ImageFilter.GaussianBlur(0.6)))
    path = create_output_folder(settings, "circle_masks") / "circle.png"
    matte.save(path)
    return path


@lru_cache(maxsize=8)
def _find_font(cjk: bool, bold: bool, serif: bool = False) -> str:
    packaged = managed_font(serif)
    if packaged.is_file():
        return str(packaged)
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    directories = (
        windows,
        Path("/usr/share/fonts/opentype/noto"),
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/usr/share/fonts/truetype/liberation2"),
        Path("/System/Library/Fonts"),
        Path("/System/Library/Fonts/Supplemental"),
    )
    key = "cjk" if cjk else "default"
    names = (*FONT_PAIRS[f"{key}_serif"][bold], *FONT_PAIRS[key][bold]) if serif else FONT_PAIRS[key][bold]
    for name in names:
        for directory in directories:
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
        try:
            return str(ImageFont.truetype(name, 20).path)
        except OSError:
            pass
    raise ProductionError("缺少可显示图卡文字的字体。请填写 font_path，中文可使用 Noto Sans CJK 或微软雅黑。")


@lru_cache(maxsize=96)
def _font(path: str, size: int, bold: bool) -> ImageFont.FreeTypeFont:
    try:
        font = ImageFont.truetype(path, size)
    except OSError as exc:
        raise ProductionError(f"无法加载字体 {path}") from exc
    if Path(path).name.casefold() in {"notosanssc-vf.ttf", "notoserifsc-vf.ttf"}:
        font.set_variation_by_axes([750 if bold else 450])
    return font


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> str:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        # Keep Latin words intact when they fit. Break CJK text one character at a time.
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9'._-]*|\s+|.", paragraph):
            if line and draw.textlength(line + token, font=font) > width:
                lines.append(line.rstrip())
                line = ""
            if draw.textlength(token, font=font) > width:
                for character in token:
                    if line and draw.textlength(line + character, font=font) > width:
                        lines.append(line)
                        line = ""
                    line += character
            else:
                line += token if line else token.lstrip()
        lines.append(line.rstrip())
    return "\n".join(lines)


def _draw_fit(
    draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], value: str, font_path: str,
    size: int, fill: str, *, bold: bool = False, min_ratio: float = 0.52,
) -> int:
    if not value:
        return 0
    left, top, right, bottom = box
    step = max(1, round(size / 18))
    minimum = max(9, round(size * min_ratio))
    for chosen in (*range(size, minimum, -step), minimum):
        font = _font(font_path, chosen, bold)
        wrapped = _wrap(draw, value, font, right - left)
        spacing = max(2, round(chosen * 0.18))
        x, y, edge, base = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=spacing)
        if edge - x <= right - left and base - y <= bottom - top:
            draw.multiline_text((left - x, top - y), wrapped, font=font, fill=fill, spacing=spacing)
            return base - y
    raise ProductionError(f"文字在当前画幅中放不下。请缩短内容或使用更大的画幅。{value[:24]!r}")


def _paste_image(
    canvas: Image.Image, source: Path, box: tuple[int, int, int, int], focus: tuple[float, float], radius: int,
) -> None:
    left, top, right, bottom = box
    with Image.open(source) as picture:
        image = ImageOps.fit(
            ImageOps.exif_transpose(picture).convert(canvas.mode), (right - left, bottom - top),
            Image.Resampling.LANCZOS, centering=focus,
        )
    mask = Image.new("L", image.size)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, image.width - 1, image.height - 1), radius=radius, fill=255)
    if image.mode == "RGBA":
        image = Image.composite(image, Image.new("RGBA", image.size), mask)
        canvas.alpha_composite(image, (left, top))
    else:
        canvas.paste(image, (left, top), mask)


def _read_items(items: list[dict[str, str]] | None) -> list[dict[str, str]]:
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > 6:
        raise ProductionError("items 最多可包含 6 项")
    cleaned = []
    for item in items:
        if not isinstance(item, dict) or item.keys() - {"label", "value", "detail"}:
            raise ProductionError("每项须提供 label，可附加 value 和 detail")
        row = {key: _copy(value, key) for key, value in item.items()}
        if not row.get("label"):
            raise ProductionError("每项须提供非空的 label")
        cleaned.append(row)
    return cleaned


def _copy(value: str | None, label: str, maximum: int = 120) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > maximum:
        raise ProductionError(f"{label} 必须为 {maximum} 字以内的文字")
    return value.strip()


def _text_theme(color_overrides: dict[str, str] | None, preset: str) -> dict[str, str]:
    if preset not in PALETTES:
        raise ProductionError("preset 必须是 editorial、midnight 或 clean")
    palette = dict(PALETTES[preset])
    if color_overrides is not None and not isinstance(color_overrides, dict):
        raise ProductionError("colors 必须是对象")
    for name, color in (color_overrides or {}).items():
        if name not in palette or not isinstance(color, str) or not re.fullmatch(r"#[\dA-Fa-f]{6}", color):
            raise ProductionError("colors 可覆盖 background、panel、foreground、muted、accent、accent_text，值为 #RRGGBB")
        palette[name] = color
    return palette


def _graphic_mark(draw: ImageDraw.ImageDraw, x: int, y: int, unit: int, gap: int, color: str, shape: GraphicShape) -> None:
    if shape == "dot":
        diameter = round(unit * 0.034)
        draw.ellipse((x, y, x + diameter, y + diameter), fill=color)
    elif shape == "block":
        edge = round(unit * 0.043)
        draw.rectangle((x, y, x + edge * 2, y + edge), fill=color)
    elif shape == "line":
        draw.rectangle((x, y + gap // 4, x + round(unit * 0.23), y + gap // 2), fill=color)
    else:
        draw.rounded_rectangle((x, y, x + round(unit * 0.16), y + gap), radius=gap // 3, fill=color)


def create_video_graphic(
    settings: Settings,
    kind: GraphicKind,
    title: str,
    *,
    subtitle: str | None = None,
    eyebrow: str | None = None,
    items: list[dict[str, str]] | None = None,
    visible_items: int | None = None,
    image_path: str | None = None,
    image_focus: tuple[float, float] = (0.5, 0.5),
    preset: GraphicPreset = "editorial",
    colors: dict[str, str] | None = None,
    width: int = 1080,
    height: int = 1920,
    font_path: str | None = None,
    style_preset: StylePreset | None = None,
) -> dict[str, Any]:
    """Render a reusable card to a local PNG and a small JPEG preview."""
    validate_canvas(width, height)
    if kind not in {"cover", "title", "data", "end", "lower_third"}:
        raise ProductionError("kind 必须是 cover、title、data、end 或 lower_third")
    title = _copy(title, "title")
    subtitle = _copy(subtitle, "subtitle", 180)
    eyebrow = _copy(eyebrow, "eyebrow", 42)
    if not title:
        raise ProductionError("title 不能为空")
    rows = _read_items(items)
    if kind == "lower_third" and (rows or image_path):
        raise ProductionError("lower_third 仅接收文字内容")
    if visible_items is not None:
        if type(visible_items) is not int or not 0 <= visible_items <= len(rows):
            raise ProductionError("visible_items 必须落在 0 到 items 项数之间")
    if not isinstance(image_focus, (tuple, list)) or len(image_focus) != 2 or any(type(v) not in (int, float) or not 0 <= v <= 1 for v in image_focus):
        raise ProductionError("image_focus 必须是两个 0–1 数值，分别为横向和纵向裁切重心")
    source = absolute_file(image_path, "image_path") if image_path else None
    profile = get_production_style(style_preset)
    if profile and colors is not None and not isinstance(colors, dict):
        raise ProductionError("colors 必须是对象")
    palette = _text_theme({**profile.palette, **(colors or {})} if profile else colors, preset)
    shape = profile.graphic_shape if profile else "rule"
    all_text = title + subtitle + eyebrow + "".join(value for row in rows for value in row.values())
    cjk = bool(re.search(r"[\u2e80-\u9fff\uac00-\ud7af\uff00-\uffef]", all_text))
    custom = str(absolute_file(font_path, "font_path")) if font_path else None
    serif = bool(profile and profile.serif)
    plain, bold = (custom, custom) if custom else (_find_font(cjk, False, serif), _find_font(cjk, True, serif))

    canvas = Image.new("RGBA" if kind == "lower_third" else "RGB", (width, height),
                       (0, 0, 0, 0) if kind == "lower_third" else palette["background"])
    draw = ImageDraw.Draw(canvas)
    unit = min(width, height)
    pad, gap, radius = (max(12, round(unit * ratio)) for ratio in (0.075, 0.025, 0.022))
    radius = {"rule": radius, "dot": radius // 2, "block": 0, "line": radius * 2}[shape]
    text_x, text_right = pad, width - pad

    def copy(box: tuple[int, int, int, int], value: str, ratio: float, color: str, heavy: bool = False) -> int:
        return _draw_fit(draw, box, value, bold if heavy else plain, round(unit * ratio), palette[color], bold=heavy)

    if kind == "lower_third":
        top = height - pad - round(unit * 0.28)
        outer_right = min(text_right, pad + round(width * 0.84))
        draw.rounded_rectangle((pad, top, outer_right, height - pad), radius, fill=palette["panel"])
        draw.rounded_rectangle((pad, top, pad + gap, height - pad), radius, fill=palette["accent"])
        left, right = pad + gap * 2, outer_right - gap
        at = top + gap
        if eyebrow:
            at += copy((left, at, right, at + round(unit * 0.065)), eyebrow, 0.034, "muted", True) + gap // 2
        title_end = height - pad - gap - (round(unit * 0.073) if subtitle else 0)
        at += copy((left, at, right, title_end), title, 0.064, "foreground", True) + gap // 2
        copy((left, at, right, height - pad - gap), subtitle, 0.035, "muted")
    else:
        at = pad
        if not source and not rows:
            at += round(height * 0.24)
            _graphic_mark(draw, text_x, at - gap * 2, unit, gap, palette["accent"], shape)
        if source and kind == "cover" and not rows:
            photo_bottom = round(height * 0.54)
            _paste_image(canvas, source, (pad, pad, text_right, photo_bottom), image_focus, radius)
            at = photo_bottom + gap * 2
            source = None
        if eyebrow:
            eyebrow_x = text_x + gap
            if shape == "rule":
                draw.rounded_rectangle((text_x, at, text_x + gap // 2, at + unit * 0.044), radius=gap // 4, fill=palette["accent"])
            elif shape == "dot":
                _graphic_mark(draw, text_x, at + gap // 4, unit, gap, palette["accent"], shape)
                eyebrow_x = text_x + gap * 2
            else:
                eyebrow_x = text_x
            at += copy((eyebrow_x, at, text_right, at + round(unit * 0.07)), eyebrow, 0.037, "muted", True) + gap
        has_body = bool(rows or source)
        reserve = max(round(height * 0.43), gap * 3) if has_body else 0
        bottom = height - pad - reserve
        title_end = bottom - (round(unit * 0.15) if subtitle else 0)
        at += copy((text_x, at, text_right, title_end), title, 0.093 if kind != "data" else 0.075, "foreground", True) + gap
        if kind == "end" and subtitle and not has_body:
            button_bottom = min(bottom, at + round(unit * 0.16))
            draw.rounded_rectangle((text_x, at, text_right, button_bottom), radius, fill=palette["accent"])
            copy((text_x + gap, at + gap, text_right - gap, button_bottom - gap), subtitle, 0.042, "accent_text", True)
        else:
            at += copy((text_x, at, text_right, bottom), subtitle, 0.042, "muted") + gap
        if has_body:
            at = max(at, round(height * 0.37))
            if source:
                photo_left = round(width * 0.61) if rows else pad
                _paste_image(canvas, source, (photo_left, at, text_right, height - pad), image_focus, radius)
                text_right = photo_left - gap
            if rows:
                _draw_rows(draw, rows, visible_items, palette, copy, text_x, text_right, at, height - pad, gap, radius, unit)

    folder = create_output_folder(settings, "graphics")
    png, jpg = folder / "graphic.png", folder / "preview.jpg"
    try:
        canvas.save(png)
        if canvas.mode == "RGBA":
            preview = Image.new("RGB", canvas.size, "#303638")
            preview.paste(canvas, mask=canvas.getchannel("A"))
        else:
            preview = canvas.copy()
        preview.thumbnail((640, 720), Image.Resampling.LANCZOS)
        preview.save(jpg, quality=88, optimize=True)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return {
        "kind": kind, "preset": preset, "colors": palette, "width": width, "height": height,
        "image_path": str(png), "preview_path": str(jpg), "transparent": canvas.mode == "RGBA",
        **({"style_preset": style_preset, "graphic_shape": shape} if profile else {}),
    }


def _draw_rows(draw, rows, visible_items, palette, copy, left, right, top, bottom, gap, radius, unit) -> None:
    shown = len(rows) if visible_items is None else visible_items
    row_gap = max(4, gap // 2)
    step = (bottom - top + row_gap) / len(rows)
    for index, row in enumerate(rows[:shown]):
        first = top + round(index * step)
        last = top + round((index + 1) * step) - row_gap
        if last <= first + 10:
            raise ProductionError("画幅内没有足够高度显示数据项")
        inset = min(row_gap, max(4, (last - first) // 7))
        highlight = visible_items is not None and index == shown - 1
        draw.rounded_rectangle((left, first, right, last), min(radius, (last - first) // 4),
                               fill=palette["accent" if highlight else "panel"])
        tw = right - left - inset * 2
        split = left + inset + round(tw * (0.61 if row.get("value") else 1))
        top_line = first + inset
        lower_line = first + (last - first) * 5 // 9 if row.get("detail") else last - inset
        ratio = min(0.047, (lower_line - top_line) * 1.13 / unit)
        color = "accent_text" if highlight else "foreground"
        copy((left + inset, top_line, split, lower_line), row["label"], ratio, color, True)
        copy((split + inset, top_line, right - inset, lower_line), row.get("value", ""), ratio, color, True)
        copy((left + inset, lower_line, right - inset, last - inset), row.get("detail", ""), min(0.033, ratio), color)


def write_contact_sheet(frames: Sequence[tuple[Path, str]], output: Path) -> None:
    """Write a compact, timestamp-labeled JPEG contact sheet for local preview images."""
    if not 1 <= len(frames) <= 24:
        raise ProductionError("预览拼图需要 1–24 张图片")
    columns = min(4, len(frames))
    cell_width, cell_height = 240, 212
    sheet = Image.new("RGB", (columns * cell_width, math.ceil(len(frames) / columns) * cell_height), "#172226")
    draw = ImageDraw.Draw(sheet)
    for index, (path, label) in enumerate(frames):
        x, y = index % columns * cell_width, index // columns * cell_height
        with Image.open(path) as image:
            preview = ImageOps.contain(ImageOps.exif_transpose(image).convert("RGB"), (cell_width - 16, cell_height - 34))
            sheet.paste(preview, (x + (cell_width - preview.width) // 2, y + 7 + (cell_height - 34 - preview.height) // 2))
        cjk = bool(re.search(r"[\u2e80-\u9fff\uac00-\ud7af\uff00-\uffef]", label))
        _draw_fit(draw, (x + 8, y + cell_height - 20, x + cell_width - 8, y + cell_height - 4), label,
                  _find_font(cjk, False), 12, "#F3F6F6")
    sheet.save(output, quality=88, optimize=True)
