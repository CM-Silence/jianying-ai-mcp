from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

from .config import Settings
from .editing_modules.layouts import DEFAULT_LAYOUT_ID, InterviewLayout, read_interview_layout
from .editing_modules.layouts.common import Focus, Rect
from .media_prepare import run_ffmpeg


def draw_layout_background(path: Path, layout: InterviewLayout, width: int, height: int) -> None:
    image = Image.new("RGB", (width, height), layout.background)
    if layout.bottom_frame_color is not None:
        x, y, w, h = layout.bottom
        rim = max(2, round(min(width, height) * .004))
        draw = ImageDraw.Draw(image)
        draw.rectangle((x - rim, y - rim, x + w + rim - 1, y + h + rim - 1), fill=layout.bottom_frame_color)
    image.save(path)


def prepare_layout_panel(
    settings: Settings, folder: Path, clip: dict[str, Any], is_photo: bool, rect: Rect, focus: Focus,
    *, name: str, width: int, height: int, fps: int, fit: str = "cover", background: str = "#000000",
) -> dict[str, Any]:
    """Fit one picture into its panel rectangle and place it on the canvas.

    ``cover`` crops to fill the panel, which suits footage. ``contain`` keeps the
    whole frame on the layout background, which suits designed graphics whose
    aspect differs from the panel.
    """
    x, y, w, h = rect
    source = Path(clip["source"])
    output = folder / (f"{name}.png" if is_photo else f"{name}.mp4")
    if is_photo:
        with Image.open(source) as image:
            picture = ImageOps.exif_transpose(image).convert("RGB")
            if fit == "contain":
                ImageOps.pad(picture, (w, h), Image.Resampling.LANCZOS, color=background, centering=focus).save(output)
            else:
                ImageOps.fit(picture, (w, h), Image.Resampling.LANCZOS, centering=focus).save(output)
    else:
        duration = clip["source_duration"] / 1_000_000
        frames = max(1, round(duration * fps))
        # Bake the container rotation before fitting. Pad a short final frame
        # so differing source/project frame rates cannot shorten the panel.
        placement = (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={w}:{h}:(ow-iw)*{focus[0]}:(oh-ih)*{focus[1]}:color=0x{background.lstrip('#')},"
            if fit == "contain" else
            f"scale={w}:{h}:force_original_aspect_ratio=increase:force_divisible_by=2,"
            f"crop={w}:{h}:(in_w-out_w)*{focus[0]}:(in_h-out_h)*{focus[1]},"
        )
        filters = (
            r"scale=max(2\,trunc(iw*sar/2)*2):ih,setsar=1,"
            f"{placement}"
            f"fps={fps},tpad=stop_mode=clone:stop_duration={1 / fps:.8f}"
        )
        arguments = [
            "-protocol_whitelist", "file", "-ss", f'{clip["source_start"] / 1_000_000:.6f}',
            "-t", f"{duration:.6f}", "-i", str(source), "-map", "0:V:0",
        ]
        arguments.extend(["-map", "0:a:0?", "-c:a", "aac", "-ar", "48000"] if clip["volume"] else ["-an"])
        run_ffmpeg(settings, [
            *arguments, "-vf", filters, "-frames:v", str(frames), "-sn", "-dn",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-map_metadata", "-1", "-movflags", "+faststart", str(output),
        ])
    # Editor placement scales the aspect-fitted panel. OpenFilm instead uses
    # the exact pixel rectangle directly.
    scale = max(w / width, h / height)
    return {
        **clip, "source": str(output), "source_start": 0, "panel_rect": list(rect),
        "clip": {**(clip.get("clip") or {}), "scale_x": scale, "scale_y": scale,
                 "transform_x": (2 * x + w) / width - 1, "transform_y": 1 - (2 * y + h) / height},
    }
