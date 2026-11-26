from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .asset_batch import collect_local_media
from .config import Settings
from .media_info import parse_media_info


def run_ffmpeg(settings: Settings, args: list[str], *, timeout: int = 1800) -> None:
    if settings.ffmpeg_exe is None or not settings.ffmpeg_exe.is_file():
        raise ValueError("未找到 ffmpeg；请设置 JIANYING_FFMPEG 或加入 PATH")
    command = [str(settings.ffmpeg_exe), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"FFmpeg 处理失败: {result.stderr.strip()[-1200:]}")


def _visual_filter(width: int | None, height: int | None, fit: str, fps: int | None, *, video: bool) -> str:
    filters = []
    if video:
        # FFmpeg applies container display rotation first. Bake display aspect into square pixels.
        filters.extend(["scale=max(2\\,trunc(iw*sar/2)*2):max(2\\,trunc(ih/2)*2)", "setsar=1"])
    if width is not None and height is not None:
        aspect = "decrease" if fit == "contain" else "increase"
        filters.append(f"scale={width}:{height}:force_original_aspect_ratio={aspect}:force_divisible_by=2")
        filters.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2" if fit == "contain" else f"crop={width}:{height}")
    if video and fps is not None:
        filters.append(f"fps={fps}")
    return ",".join(filters)


def _prepare_one(
    settings: Settings, path: Path, *, width: int | None, height: int | None, fps: int | None,
    fit: str, normalize: bool, extract_audio: bool, audio_rate: int, source_sha256: str | None = None,
) -> dict[str, Any]:
    info = parse_media_info(settings, str(path))["media_info"]
    kind = info["type"]
    stat = path.stat()
    key = hashlib.sha256(json.dumps([
        1, str(path), stat.st_size, stat.st_mtime_ns, width, height, fps, fit, normalize, extract_audio, audio_rate,
        source_sha256,
    ]).encode()).hexdigest()[:24]
    folder = settings.work_root / "prepared_media" / key
    visual = folder / ("editor.mp4" if kind == "video" else "image.png") if normalize and kind != "audio" else None
    audio = folder / "audio.wav" if info["has_audio"] and (extract_audio or (normalize and kind == "audio")) else None
    audio_input = ["-protocol_whitelist", "file", "-i", str(path), "-map", "0:a:0", "-vn", "-sn", "-dn"]
    input_args = ["-protocol_whitelist", "file", "-i", str(path), "-map", "0:V:0"]
    if visual is not None or audio is not None:
        folder.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=folder, prefix="working-") as tmp:
            temporary = Path(tmp)
            if visual is not None and not visual.is_file():
                target = temporary / visual.name
                vf = _visual_filter(width, height, fit, fps, video=kind == "video")
                filters = ["-vf", vf] if vf else []
                if kind == "video":
                    command = [
                        *input_args, "-map", "0:a:0?", "-sn", "-dn", *filters,
                        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-ar", "48000", "-movflags", "+faststart", "-map_metadata", "-1", str(target),
                    ]
                else:
                    command = [*input_args, *filters, "-frames:v", "1", "-update", "1", str(target)]
                run_ffmpeg(settings, command)
                os.replace(target, visual)
            if audio is not None and not audio.is_file():
                target = temporary / audio.name
                channels = "1" if audio_rate == 16000 else "2"
                run_ffmpeg(settings, [*audio_input, "-ar", str(audio_rate), "-ac", channels, "-c:a", "pcm_s16le", str(target)])
                os.replace(target, audio)

    prepared = (visual or audio) if normalize else None
    return {
        "media_path": str(path), "media_info": info,
        "prepared_path": str(prepared) if prepared is not None else None,
        "prepared_info": parse_media_info(settings, str(prepared))["media_info"] if prepared is not None else None,
        "audio_path": str(audio) if audio is not None else None,
    }


def prepare_media_assets(
    settings: Settings,
    sources: list[str],
    *,
    recursive: bool = False,
    normalize: bool = True,
    extract_audio: bool = False,
    width: int | None = None,
    height: int | None = None,
    fps: int | None = None,
    fit: str = "contain",
    audio_rate: int = 16000,
) -> dict[str, Any]:
    if not normalize and not extract_audio:
        raise ValueError("请选择 normalize 或 extract_audio")
    if (width is None) != (height is None) or any(
        type(size) is not int or not 2 <= size <= 4096 or size % 2 for size in (width, height) if size is not None
    ):
        raise ValueError("宽高需同时提供，为 2–4096 的偶数")
    if fps is not None and (type(fps) is not int or not 1 <= fps <= 120):
        raise ValueError("fps 必须是 1–120 的整数")
    if fit not in {"contain", "cover"} or audio_rate not in {16000, 44100, 48000}:
        raise ValueError("fit 请选择 contain/cover，audio_rate 请选择 16000/44100/48000")

    items = []
    for path in collect_local_media(sources, recursive):
        try:
            items.append(_prepare_one(
                settings, path, width=width, height=height, fps=fps, fit=fit,
                normalize=normalize, extract_audio=extract_audio, audio_rate=audio_rate,
            ))
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            items.append({"media_path": str(path), "error": str(exc)})
    errors = sum("error" in item for item in items)
    return {"success": errors < len(items), "items": items, "error_count": errors}


def needs_upright_copy(info: dict[str, Any]) -> bool:
    """True when a video carries a quarter-turn display rotation.

    Draft libraries register such files by their coded size (e.g. 3840×2160 for a
    portrait phone clip), so the editor would fit a landscape frame into the canvas.
    """
    rotation = info.get("rotation_degrees")
    return info.get("type") == "video" and isinstance(rotation, (int, float)) and abs(rotation) % 180 == 90


def editing_clip(
    settings: Settings, source: Path, start: float, end: float, *,
    width: int, height: int, fps: int, handle: float = 1.0, source_sha256: str | None = None,
) -> dict[str, Any]:
    """Cut ``start``–``end`` (plus handles) into an upright, canvas-sized copy.

    Only the range a beat uses is transcoded, so long 4K takes stay cheap. The copy
    keeps the source audio and covers the canvas without cropping, and the returned
    ``offset`` is where ``start`` now sits inside the copy.
    """
    if end <= start:
        raise ValueError("editing_clip 需要 end 大于 start")
    info = parse_media_info(settings, str(source))["media_info"]
    duration = info.get("duration") or end
    first = max(0.0, start - handle)
    last = min(float(duration), end + handle)
    stat = source.stat()
    key = hashlib.sha256(json.dumps([
        1, str(source), stat.st_size, stat.st_mtime_ns, round(first, 3), round(last, 3), width, height, fps, source_sha256,
    ]).encode()).hexdigest()[:24]
    folder = settings.work_root / "prepared_media" / "clips"
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / f"{key}.mp4"
    if not output.is_file():
        with TemporaryDirectory(dir=folder, prefix="working-") as tmp:
            target = Path(tmp) / output.name
            # FFmpeg applies the display rotation on decode; scale up to cover the canvas.
            vf = (
                r"scale=max(2\,trunc(iw*sar/2)*2):max(2\,trunc(ih/2)*2),setsar=1,"
                f"scale={width}:{height}:force_original_aspect_ratio=increase:force_divisible_by=2,fps={fps}"
            )
            run_ffmpeg(settings, [
                "-protocol_whitelist", "file", "-ss", f"{first:.6f}", "-t", f"{last - first:.6f}",
                "-i", str(source), "-map", "0:V:0", "-map", "0:a:0?", "-sn", "-dn", "-vf", vf,
                "-c:v", "libx264", "-preset", "fast", "-crf", "17", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", "-map_metadata", "-1",
                str(target),
            ])
            os.replace(target, output)
    return {"path": str(output), "offset": start - first, "source": str(source), "range": [first, last]}


def portable_shot_source(
    settings: Settings, source: Path, info: dict, *, first: int, frames: int, fps: int,
    width: int, height: int, source_sha256: str,
) -> tuple[str, int]:
    """Bind an original recipe range to media usable by both the draft and the browser."""
    suffix = source.suffix.lower()
    if info["type"] == "image":
        if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
            return str(source), 0
        result = _prepare_one(settings, source, width=None, height=None, fps=None, fit="contain",
                              normalize=True, extract_audio=False, audio_rate=48000, source_sha256=source_sha256)
        return result["prepared_path"], 0
    if (not info.get("rotation_degrees") and info["video_codec"] == "h264"
            and suffix in {".mp4", ".m4v", ".mov"} and info.get("audio_codec") in {None, "aac", "mp3"}):
        return str(source), first
    proxy = editing_clip(settings, source, first / fps, (first + frames) / fps, width=width, height=height,
                         fps=fps, source_sha256=source_sha256)
    return proxy["path"], round(proxy["offset"] * fps)


def extract_audio_range(settings: Settings, source: Path, start: float, end: float) -> dict[str, Any]:
    """Cut the audio of ``start``–``end`` into a standalone WAV for a voice-over card."""
    if end <= start:
        raise ValueError("extract_audio_range 需要 end 大于 start")
    stat = source.stat()
    key = hashlib.sha256(json.dumps([
        1, str(source), stat.st_size, stat.st_mtime_ns, round(start, 3), round(end, 3),
    ]).encode()).hexdigest()[:24]
    folder = settings.work_root / "prepared_media" / "voice"
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / f"{key}.wav"
    if not output.is_file():
        # Exact range: captions and the card are timed off the same seconds.
        first, last = start, end
        with TemporaryDirectory(dir=folder, prefix="working-") as tmp:
            target = Path(tmp) / output.name
            run_ffmpeg(settings, [
                "-protocol_whitelist", "file", "-ss", f"{first:.6f}", "-t", f"{last - first:.6f}",
                "-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn",
                "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(target),
            ])
            os.replace(target, output)
    return {"path": str(output), "offset": 0.0, "seconds": end - start}
