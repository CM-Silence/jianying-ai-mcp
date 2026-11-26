from __future__ import annotations

import json
import math
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from .config import Settings


class MediaProbeError(ValueError):
    pass


def _probe_streams(ffprobe: Path, source: Path, *, local_only: bool = False) -> dict[str, Any]:
    command = [str(ffprobe), "-v", "error"]
    if local_only:
        command.extend(["-protocol_whitelist", "file"])
    command.extend(["-show_streams", "-show_format", "-of", "json", str(source)])
    label = "读取媒体信息" if local_only else "读取视频信息"
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30 if local_only else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaProbeError(f"{label}超时") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1200:]
        raise MediaProbeError(f"{label}失败: {detail}")
    return json.loads(result.stdout)


def _duration(*values: str | None) -> float | None:
    for value in values:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    return None


def _frame_rate(value: str | None) -> float | None:
    if not value:
        return None
    try:
        rate = float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def count_video_frames(settings: Settings, path: str) -> int:
    """Decode frame counts only for deliveries whose container omits nb_frames."""
    result = subprocess.run([str(settings.ffprobe_exe), "-v", "error", "-protocol_whitelist", "file",
                             "-select_streams", "V:0", "-count_frames", "-show_entries", "stream=nb_read_frames",
                             "-of", "default=noprint_wrappers=1:nokey=1", path],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3600, check=True)
    if not result.stdout.strip().isdigit():
        raise MediaProbeError("未能读取成片帧数")
    return int(result.stdout.strip())


def parse_media_info(settings: Settings, media_path: str) -> dict[str, Any]:
    """Read metadata for a local video, audio file, or image without extracting content."""
    path = Path(media_path).expanduser()
    if not path.is_absolute():
        raise MediaProbeError("媒体文件必须使用本地绝对路径")
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    ffprobe = settings.ffprobe_exe
    if ffprobe is None or not ffprobe.is_file():
        raise MediaProbeError("未找到 ffprobe；请设置 JIANYING_FFPROBE 或将 ffprobe 加入 PATH")

    data = _probe_streams(ffprobe, path, local_only=True)
    streams = data.get("streams") or []
    container = data.get("format") or {}
    format_name = container.get("format_name")
    visual = next((
        stream for stream in streams if stream.get("codec_type") == "video"
        and not (stream.get("disposition") or {}).get("attached_pic")
    ), {})
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
    formats = (format_name or "").split(",")
    is_image = bool(visual) and not audio and (
        path.suffix.lower() in {".avif", ".heic", ".heif"} or any(
            name in {"image2", "image2pipe", "gif", "apng", "ico"} or name.endswith("_pipe") for name in formats
        )
    )
    if not (visual or audio):
        raise MediaProbeError("文件没有可识别的视频、音频或图片流")

    duration = None if is_image else _duration(
        container.get("duration"), visual.get("duration"), audio.get("duration"),
    )
    width, height = visual.get("width"), visual.get("height")
    rotation = next((item["rotation"] for item in visual.get("side_data_list", []) if "rotation" in item), 0)
    quarter_turn = isinstance(rotation, (int, float)) and abs(rotation) % 180 == 90
    sample_rate = audio.get("sample_rate")
    return {
        "success": True,
        "media_path": str(path),
        "media_info": {
            "type": "image" if is_image else "video" if visual else "audio",
            "format": format_name,
            "file_size": path.stat().st_size,
            "duration": duration,
            "duration_ms": round(duration * 1000) if duration is not None else None,
            "video_duration": _duration(visual.get("duration")) if not is_image else None,
            "video_frames": int(visual["nb_frames"]) if not is_image and str(visual.get("nb_frames")).isdigit() else None,
            "has_video": bool(visual) and not is_image,
            "has_audio": bool(audio),
            "width": width,
            "height": height,
            "resolution": f"{width}x{height}" if width and height else None,
            "rotation_degrees": rotation if visual else None,
            "display_width": height if quarter_turn else width,
            "display_height": width if quarter_turn else height,
            "frame_rate": None if is_image else _frame_rate(visual.get("avg_frame_rate")),
            "video_codec": None if is_image else visual.get("codec_name"),
            "image_codec": visual.get("codec_name") if is_image else None,
            "audio_codec": audio.get("codec_name"),
            "sample_rate": int(sample_rate) if str(sample_rate).isdigit() else None,
            "channels": audio.get("channels"),
        },
    }
