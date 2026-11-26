"""Choose where captions sit in a shot without covering the speaker.

A talking-head clip almost always has a face in the upper middle and shoulders
below it. Rather than pinning captions to one fixed line, sample a few frames,
score horizontal bands for skin and visual detail, and put the caption in the
calmest band. The result is a vertical anchor the timeline applies to that
beat's captions, so captions move with the subject instead of repeating.
"""

from __future__ import annotations

import math
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageFont

from .config import Settings
from .managed_fonts import font_path
from .production_graphics import ProductionError


@dataclass(frozen=True)
class CaptionAnchor:
    zone: str
    transform_y: float
    skin: float
    detail: float

    def as_dict(self) -> dict[str, Any]:
        return {"zone": self.zone, "transform_y": round(self.transform_y, 3)}


# (zone, band centre as a 0–1 fraction from the top, matching 剪映 transform_y)
BANDS = (
    ("top", 0.13, 0.74),
    ("upper", 0.32, 0.36),
    ("lower", 0.70, -0.40),
    ("bottom", 0.87, -0.74),
)
# A caption whose centre sits near the bottom is the familiar default, so ties
# resolve downwards unless skin pushes them away.
BOTTOM_BIAS = -0.12
SAMPLE_SECONDS = (0.25, 0.5, 0.75)


def _frames(settings: Settings, source: Path, start: float, end: float, size: tuple[int, int]) -> list[Image.Image]:
    ffmpeg = settings.ffmpeg_exe
    if ffmpeg is None or not ffmpeg.is_file():
        return []
    images = []
    span = max(0.0, end - start)
    for fraction in SAMPLE_SECONDS:
        moment = start + span * fraction
        with subprocess.Popen(
            [
                str(ffmpeg), "-v", "error", "-nostdin", "-ss", f"{moment:.3f}", "-i", str(source),
                "-frames:v", "1", "-vf", f"scale={size[0]}:{size[1]}", "-f", "image2pipe", "-vcodec", "png", "-",
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ) as process:
            data = process.stdout.read() if process.stdout else b""
        if data:
            import io

            images.append(Image.open(io.BytesIO(data)).convert("RGB"))
    return images


def _band_scores(image: Image.Image) -> list[tuple[float, float]]:
    """(skin share, detail) for each band, computed off a small RGB frame."""
    import numpy as np

    pixels = np.asarray(image, dtype=np.float32)
    height = pixels.shape[0]
    red, green, blue = pixels[..., 0], pixels[..., 1], pixels[..., 2]
    luma = 0.299 * red + 0.587 * green + 0.114 * blue
    cb = 128 - 0.168736 * red - 0.331264 * green + 0.5 * blue
    cr = 128 + 0.5 * red - 0.418688 * green - 0.081312 * blue
    skin = (cb >= 77) & (cb <= 127) & (cr >= 133) & (cr <= 173) & (luma > 60)
    edges = np.abs(np.diff(luma, axis=1)).sum(axis=1) / max(1, luma.shape[1])
    scores = []
    for _, centre, _ in BANDS:
        half = max(1, int(height * 0.07))
        middle = int(height * centre)
        top, bottom = max(0, middle - half), min(height, middle + half)
        scores.append((float(skin[top:bottom].mean()), float(edges[top:bottom].mean()) / 255.0))
    return scores


def choose_caption_anchor(
    settings: Settings, source: str | Path, start: float, end: float,
) -> CaptionAnchor | None:
    """Pick the calmest band for captions in one shot; ``None`` when it cannot be analysed."""
    path = Path(source)
    if not path.is_file() or path.suffix.lower() != ".mp4":
        return None
    frames = _frames(settings, path, start, end, (180, 320))
    if not frames:
        return None
    try:
        import numpy  # noqa: F401
    except ImportError:
        return None
    totals = [0.0, 0.0, 0.0, 0.0]
    details = [0.0, 0.0, 0.0, 0.0]
    for frame in frames:
        for index, (skin, detail) in enumerate(_band_scores(frame)):
            totals[index] += skin
            details[index] += detail
    count = len(frames)
    scores = [
        (totals[index] / count) * 2.2 + (details[index] / count) * 0.6 + (BOTTOM_BIAS if index == 3 else 0.0)
        for index in range(len(BANDS))
    ]
    best = min(range(len(BANDS)), key=lambda index: scores[index])
    zone, _, transform_y = BANDS[best]
    return CaptionAnchor(zone, transform_y, totals[best] / count, details[best] / count)


def fixed_caption_slots(texts: tuple[str, ...], mode: str, width: int, height: int,
                        size: float, base_y: float, serif: bool) -> list[tuple[float, float]]:
    """Measure the completed group once; return its centres in Jianying coordinates."""
    em = size * min(width, height) / 144
    path = font_path(serif)
    font = ImageFont.truetype(str(path), 128) if path.is_file() else None
    if font is not None:
        font.set_variation_by_axes([700])

    def text_width(text: str) -> float:
        if font is not None:
            return font.getlength(text) * em / 128
        return em * sum(1 if unicodedata.east_asian_width(c) in "WF" else 0.65 for c in text)

    widths = [text_width(t) for t in texts]
    gap = em * 0.55
    available = width * 0.88
    total = sum(widths) + gap * (len(texts) - 1)
    cy = (1 - base_y) * height / 2
    if mode == "blocks" and total <= available:
        left = (width - total) / 2
        y = min(height - em * 0.8, max(em * 0.8, cy))
        slots = []
        for w in widths:
            slots.append((round((2 * left + w) / width - 1, 6), round(1 - 2 * y / height, 6)))
            left += w + gap
        return slots
    heights = [max(1, math.ceil(w / available)) * em * 1.3 for w in widths]
    gap = em * 0.28
    span = sum(heights) + gap * (len(texts) - 1)
    if span > height * 0.9:
        return []
    top = max(height * 0.05, min(cy - span / 2, height * 0.95 - span))
    slots = []
    for h in heights:
        slots.append((0.0, round(1 - (2 * top + h) / height, 6)))
        top += h + gap
    return slots


def zone_anchor(zone: str) -> CaptionAnchor:
    for name, _, transform_y in BANDS:
        if name == zone:
            return CaptionAnchor(name, transform_y, 0.0, 0.0)
    raise ProductionError(f"caption.zone 支持 {'、'.join(name for name, _, _ in BANDS)}")
