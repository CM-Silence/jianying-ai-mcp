from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .config import Settings
from .media_info import parse_media_info
from .video_observer import _parse_time


MAX_REVIEW_SECONDS = 600
MAX_REVIEW_FRAMES = 30
BLACK_FILTER = "blackdetect=d=0.2:pix_th=0.1:pic_th=0.98"
# Measure decoded pixels on every frame, independently of the retained JPEG samples.
GREEN_SIMILARITY = 0.20
GREEN_PERCENT = 85
GREEN_GRID = f"scale=32:32:flags=neighbor,format=rgba,colorkey=0x00ff00:{GREEN_SIMILARITY}:0,alphaextract,blackframe=amount=0:threshold=1"
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
GREEN_FRAME = re.compile(rf"\[Parsed_blackframe_\d+ @ [^\]]+\]\s+frame:\d+\s+pblack:(\d+)\s+pts:-?\d+\s+t:({NUMBER})")
BLACK_PATTERN = re.compile(rf"black_start:({NUMBER})\s+black_end:({NUMBER})")
FRAME_PATTERN = re.compile(rf"^\[Parsed_showinfo_\d+ @ [^\r\n]+?\]\s+n:\s*\d+\s+pts:\s*-?\d+\s+pts_time:({NUMBER})", re.M)
LOUDNESS_PATTERN = re.compile(rf"^\s*I:\s*({NUMBER})\s+LUFS", re.M)
PEAK_PATTERN = re.compile(rf"^\s*Peak:\s*({NUMBER})\s+dB(?:FS|TP)", re.M)


class ExportReviewError(ValueError):
    pass


def _run_ffmpeg(command: list[str], output: list[str], label: str) -> str:
    try:
        result = subprocess.run(
            [*command, *output], stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExportReviewError(f"{label}超时。请缩短检查区间。") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-1200:]
        raise ExportReviewError(f"{label}失败。{detail}")
    return result.stderr


def _audio_review(command: list[str], has_audio: bool, target: float | None) -> dict[str, Any]:
    integrated = peak = None
    if has_audio:
        log = _run_ffmpeg(command, [
            "-map", "0:a:0", "-af", "ebur128=peak=true:framelog=verbose", "-f", "null", "-",
        ], "检测响度")
        loudness_match = LOUDNESS_PATTERN.search(log)
        peak_match = PEAK_PATTERN.search(log)
        # EBU R128's -70 LUFS absolute gate is also the sum-up value for no gated blocks.
        if loudness_match and float(loudness_match[1]) > -70:
            integrated = float(loudness_match[1])
        if peak_match:
            peak = float(peak_match[1])
    return {
        "has_audio": has_audio,
        "integrated_lufs": integrated,
        "true_peak_dbtp": peak,
        "target_loudness_lufs": target,
        "difference_from_target_lu": round(integrated - target, 1) if integrated is not None and target is not None else None,
    }


def _green_report(log: str, start: float, end: float, fps: float | None) -> tuple[list[dict], dict]:
    segments = []
    suspect = total = 0
    first = None
    last = start
    for percent, timestamp in GREEN_FRAME.findall(log):
        total += 1
        last = min(end, start + max(0.0, float(timestamp)))
        if int(percent) >= GREEN_PERCENT:
            suspect += 1
            if first is None:
                first = last
        elif first is not None:
            segments.append({"start_ms": round(first * 1000), "end_ms": round(last * 1000)})
            first = None
    if first is not None:
        finish = min(end, last + 1 / fps) if fps else end
        segments.append({"start_ms": round(first * 1000), "end_ms": round(finish * 1000)})
    return segments, {"frames_checked": total, "suspect_frames": suspect,
                      "fraction": round(suspect / total, 6) if total else 0,
                      "minimum_green_percent": GREEN_PERCENT}


def review_export(
    settings: Settings,
    media_path: str,
    *,
    start: str | float | int | None = None,
    end: str | float | int | None = None,
    max_frames: int = 12,
    target_loudness: float | None = None,
) -> dict[str, Any]:
    """Measure a local finished video's picture and audio, without changing the file."""
    if type(max_frames) is not int or not 0 <= max_frames <= MAX_REVIEW_FRAMES:
        raise ExportReviewError(f"max_frames 必须是 0 到 {MAX_REVIEW_FRAMES} 之间的整数")
    if target_loudness is not None and not math.isfinite(target_loudness):
        raise ExportReviewError("target_loudness 必须是有限数值")

    metadata = parse_media_info(settings, media_path)
    info = metadata["media_info"]
    duration = info["duration"]
    if info["type"] != "video" or duration is None or duration <= 0:
        raise ExportReviewError("请提供有可读时长的视频文件")
    from_s = _parse_time(start) if start is not None else 0.0
    to_s = _parse_time(end) if end is not None else duration
    if not (math.isfinite(from_s) and math.isfinite(to_s) and 0 <= from_s < to_s <= duration):
        raise ExportReviewError("检查范围须位于视频内，且结束时间晚于起始时间")
    length = to_s - from_s
    if length > MAX_REVIEW_SECONDS:
        raise ExportReviewError(f"单次最多检查 {MAX_REVIEW_SECONDS} 秒。请给出较短的 start 和 end。")

    ffmpeg = settings.ffmpeg_exe
    if ffmpeg is None or not ffmpeg.is_file():
        raise ExportReviewError("未找到 ffmpeg。请设置 JIANYING_FFMPEG 或将 ffmpeg 加入 PATH。")
    command = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
        "-protocol_whitelist", "file", "-threads", "2", "-filter_threads", "2",
        "-ss", f"{from_s:.9f}", "-t", f"{length:.9f}", "-i", metadata["media_path"],
    ]
    root = (settings.work_root / "export_reviews").resolve()
    root.mkdir(parents=True, exist_ok=True)
    review_dir = Path(tempfile.mkdtemp(prefix="review-", dir=root))
    try:
        filters = BLACK_FILTER
        output = ["-f", "null", "-"]
        if max_frames:
            interval = length / max_frames
            filters += (
                f",select='lt(selected_n,{max_frames})*(isnan(prev_selected_t)+gte(t-prev_selected_t,{interval:.9f}))'"
                ",scale='min(512,iw)':'min(512,ih)':force_original_aspect_ratio=decrease,showinfo=checksum=0"
            )
            output = ["-fps_mode", "vfr", "-q:v", "3", "-f", "image2", str(review_dir / "frame-%03d.jpg")]
        graph = f"[0:V:0]split[image][green];[green]{GREEN_GRID},nullsink;[image]{filters}[review]"
        log = _run_ffmpeg(command, ["-filter_complex_threads", "2", "-filter_complex", graph, "-map", "[review]", *output], "检测画面")
        green_segments, green_detection = _green_report(log, from_s, to_s, info.get("frame_rate"))
        if not green_detection["frames_checked"] and from_s < (info.get("video_duration") or duration) - 1 / (info.get("frame_rate") or 1):
            raise ExportReviewError("未能读取区间内的逐帧颜色检测结果")

        def absolute_ms(timestamp: str) -> int:
            return round((from_s + min(length, max(0.0, float(timestamp)))) * 1000)

        frame_files = sorted(review_dir.glob("frame-*.jpg"))
        frame_times = FRAME_PATTERN.findall(log)
        if len(frame_files) != len(frame_times):
            raise ExportReviewError("预览帧数量与报告的时间戳不一致")
        frames = [{"time_ms": absolute_ms(t), "path": str(p)} for t, p in zip(frame_times, frame_files)]
        black_segments = []
        for first, last in BLACK_PATTERN.findall(log):
            first_ms, last_ms = absolute_ms(first), absolute_ms(last)
            if last_ms > first_ms:
                black_segments.append({"start_ms": first_ms, "end_ms": last_ms, "duration_ms": last_ms - first_ms})
        audio = _audio_review(command, info["has_audio"], target_loudness)
    except BaseException:
        shutil.rmtree(review_dir, ignore_errors=True)
        raise

    return {
        **metadata,
        "range": {"start_ms": round(from_s * 1000), "end_ms": round(to_s * 1000)},
        "frames": frames,
        "black_segments": black_segments,
        "green_segments": green_segments,
        "green_detection": green_detection,
        "black_detection": {"minimum_duration_ms": 200, "pixel_threshold": 0.1, "frame_threshold": 0.98},
        "audio": audio,
        "review_dir": str(review_dir),
        "limitations": [
            "仅检查已导出文件的首个视频流和首个音频流。",
            "抽帧供人工或视觉模型核对。黑场可能是有意的转场，时间按检测到的帧计算。",
            "green_segments 列出画面大面积接近纯绿的时段。可用工程预览辨认有意的绿底，deliver_video_edit 会自动核对。",
            "响度结果取决于选定区间。片段太短或没有有效响度时，综合响度为空。",
            "此检查无法核实剪映中的草稿效果、字幕内容或音画同步。",
        ],
    }
