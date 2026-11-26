from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from http.client import parse_headers
from pathlib import Path
from threading import Lock
from typing import Any, BinaryIO
from urllib.parse import urlparse

from .config import Settings
from .file_io import atomic_text_writer
from .media_info import MediaProbeError, _probe_streams
from .video_local_asr import LocalASRError, configured_model, local_asr_status, model_options, transcribe_local
from .video_whisper import ENDPOINTS, transcribe_video


DETAIL_CAPS = {"transcript": 0, "efficient": 50, "balanced": 100, "dense": 200}
TIMESTAMP_RE = re.compile(r"\[Parsed_showinfo_\d+ @ [^\]]+\]\s+n:\s*\d+\s+pts:\s*-?\d+\s+pts_time:(-?[.\d]+(?:e[+-]?\d+)?)", re.I)
ANALYSIS_VERSION = 3
ASR_CACHE_VERSION = 1
TEXT_SUBTITLE_CODECS = {"ass", "mov_text", "srt", "ssa", "subrip", "text", "webvtt"}
_DOWNLOAD_LOCK = Lock()
_ASR_LOCK = Lock()
SRT_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})"
)
VTT_TIME_RE = re.compile(
    r"(?:(\d{1,2}):)?(\d{2}):(\d{2})[.](\d{3})\s*-->\s*"
    r"(?:(\d{1,2}):)?(\d{2}):(\d{2})[.](\d{3})"
)


class VideoObserverError(ValueError):
    pass


def observer_status(settings: Settings) -> dict[str, Any]:
    return {
        "available": settings.ffmpeg_exe is not None and settings.ffprobe_exe is not None,
        "ffmpeg": str(settings.ffmpeg_exe) if settings.ffmpeg_exe else None,
        "ffprobe": str(settings.ffprobe_exe) if settings.ffprobe_exe else None,
        "yt_dlp": str(settings.yt_dlp_exe) if settings.yt_dlp_exe else None,
        "url_input": settings.yt_dlp_exe is not None,
        "transcription": {
            "sidecar_subtitles": True,
            "embedded_text_subtitles": True,
            "local_whisper": local_asr_status(),
            "groq_whisper": bool(os.environ.get("GROQ_API_KEY")),
            "openai_whisper": bool(os.environ.get("OPENAI_API_KEY")),
        },
        "setup_hint": (
            "设置 JIANYING_FFMPEG/JIANYING_FFPROBE；URL 输入另设置 JIANYING_YT_DLP。"
            if settings.ffmpeg_exe is None or settings.ffprobe_exe is None else None
        ),
    }


def _require_tool(path: Path | None, name: str) -> Path:
    if path is None or not path.is_file():
        raise VideoObserverError(f"未找到 {name}；请在环境变量中配置对应可执行文件")
    return path


def _run(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1200:]
        raise VideoObserverError(f"{label}失败: {detail}")
    return result


def _parse_time(value: str | float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    parts = value.strip().split(":")
    try:
        if len(parts) == 1:
            return float(parts[0].removesuffix("s"))
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError as exc:
        raise VideoObserverError(f"无效时间: {value}") from exc
    raise VideoObserverError(f"无效时间: {value}")


def _cache_id(identity: dict[str, Any]) -> str:
    payload = json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:20]


def _file_identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    with atomic_text_writer(path) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def _download_url(settings: Settings, source: str, work_dir: Path) -> Path:
    yt_dlp = _require_tool(settings.yt_dlp_exe, "yt-dlp")
    template = work_dir / "source.%(ext)s"
    result = _run([
        str(yt_dlp), "--no-playlist", "--write-subs", "--write-auto-subs",
        "--sub-langs", "zh.*,en.*", "--sub-format", "vtt", "--print", "after_move:filepath",
        "-o", str(template), source,
    ], "下载视频")
    candidates = [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]
    for candidate in reversed(candidates):
        if candidate.is_file() and candidate.suffix.lower() not in {".vtt", ".srt"}:
            return candidate.resolve()
    files = [
        p for p in work_dir.glob("source.*")
        if p.is_file() and p.suffix.lower() not in {".vtt", ".srt", ".json", ".part", ".ytdl"}
    ]
    if not files:
        raise VideoObserverError("yt-dlp 未返回下载后的视频路径")
    return files[0].resolve()


def _resolve_source(settings: Settings, source: str) -> tuple[Path, bool]:
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        work_dir = settings.work_root / "video_sources" / _cache_id({"url": source})
        marker = work_dir / "download.json"
        with _DOWNLOAD_LOCK:
            if marker.is_file():
                record = json.loads(marker.read_text(encoding="utf-8"))
                path = work_dir / Path(record["file"]).name
                if path.is_file():
                    return path.resolve(), True
            work_dir.mkdir(parents=True, exist_ok=True)
            video = _download_url(settings, source, work_dir)
            _write_json(marker, {"file": video.name})
        return video, True
    path = Path(source).expanduser()
    if not path.is_absolute():
        raise VideoObserverError("本地视频必须使用绝对路径")
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return path, False


def _probe(ffprobe: Path, video: Path) -> tuple[dict[str, Any], list[int]]:
    try:
        data = _probe_streams(ffprobe, video, local_only=True)
    except MediaProbeError as exc:
        raise VideoObserverError(str(exc)) from exc
    streams = data.get("streams") or []
    visual = next((s for s in streams if s.get("codec_type") == "video"
                   and not (s.get("disposition") or {}).get("attached_pic")), {})
    duration = float((data.get("format") or {}).get("duration") or visual.get("duration") or 0)
    metadata = {
        "duration_ms": round(duration * 1000),
        "width": visual.get("width"),
        "height": visual.get("height"),
        "codec": visual.get("codec_name"),
        "fps": visual.get("avg_frame_rate"),
        "has_video": bool(visual),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }
    text_tracks = [
        s for s in streams
        if s.get("codec_type") == "subtitle" and s.get("codec_name") in TEXT_SUBTITLE_CODECS
    ]
    text_tracks.sort(key=lambda s: (
        bool((s.get("disposition") or {}).get("forced")),
        not bool((s.get("disposition") or {}).get("default")), s["index"],
    ))
    return metadata, [s["index"] for s in text_tracks]


def _scale_filter(width: int) -> str:
    if width < 64 or width > 1920:
        raise VideoObserverError("resolution 必须在 64 到 1920 之间")
    return f"scale=min({width}\\,iw):-2"


@dataclass(frozen=True)
class _Frame:
    time_ms: int
    offset: int
    size: int
    digest: str
    reason: str


def _read_jpegs(spool: BinaryIO, begin: int, times: list[int], reason: str) -> list[_Frame]:
    # The MJPEG muxer frames every image with its byte length. FFmpeg diagnostics use a separate pipe.
    end = spool.seek(0, os.SEEK_END)
    spool.seek(begin)
    frames = []
    while spool.tell() < end:
        if spool.readline() != b"--ffmpeg\r\n":
            raise VideoObserverError("FFmpeg 返回的 JPEG 分隔符无效")
        if spool.tell() == end:
            break
        headers = parse_headers(spool)
        size = int(headers.get("Content-Length", "0"))
        offset = spool.tell()
        if not 0 < size <= end - offset or len(frames) >= len(times):
            raise VideoObserverError("FFmpeg 返回的 JPEG 长度或时间戳无效")
        digest = hashlib.sha256(spool.read(size)).hexdigest()
        if spool.readline() != b"\r\n":
            raise VideoObserverError("FFmpeg 返回的 JPEG 数据不完整")
        frames.append(_Frame(times[len(frames)], offset, size, digest, reason))
    return frames


def _extract_frames(
    ffmpeg: Path,
    video: Path,
    spool: BinaryIO,
    mode: str,
    resolution: int,
    start: float,
    end: float,
    limit: int,
) -> list[_Frame]:
    offset = spool.seek(0, os.SEEK_END)
    command = [str(ffmpeg), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info"]
    if start > 0:
        command += ["-ss", f"{start:.3f}"]
    if mode == "efficient":
        command += ["-skip_frame", "nokey"]
    command += ["-protocol_whitelist", "file", "-i", str(video)]
    duration = end - start
    spacing = duration / limit
    select = f"isnan(prev_selected_t)+gte(t-prev_selected_t,{spacing:.9f})"
    reason = "uniform"
    if mode == "efficient":
        reason = "keyframe"
    elif mode in {"balanced", "dense"}:
        threshold = 0.24 if mode == "balanced" else 0.15
        select = f"gt(scene,{threshold})*({select})"
        reason = "scene"
    command += [
        "-t", f"{duration:.3f}", "-map", "0:V:0",
        "-vf", f"select='{select}',{_scale_filter(resolution)},showinfo",
        "-fps_mode", "vfr", "-c:v", "mjpeg", "-q:v", "4", "-frames:v", str(limit),
        "-f", "mpjpeg", "-boundary_tag", "ffmpeg", "pipe:1",
    ]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=spool, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", timeout=900)
    if result.returncode != 0:
        if mode != "uniform" and "nothing was written" in result.stderr.lower():
            return []
        detail_text = result.stderr.strip()[-1200:]
        raise VideoObserverError(f"提取视频代表帧失败: {detail_text}")
    times = [round((start + float(match.group(1))) * 1000) for match in TIMESTAMP_RE.finditer(result.stderr)]
    return _read_jpegs(spool, offset, times, reason)


def _sample_frames(
    ffmpeg: Path,
    video: Path,
    frame_dir: Path,
    detail: str,
    resolution: int,
    start: float,
    end: float,
    cap: int,
) -> list[dict[str, Any]]:
    # Keep candidates in one seekable stream. Only selected JPEGs become named files.
    with tempfile.TemporaryFile(dir=frame_dir) as spool:
        candidates = []
        if detail != "transcript":
            candidates = _extract_frames(ffmpeg, video, spool, detail, resolution, start, end, min(1000, max(12, cap * 2)))
        events = _unique_frames(candidates)
        if cap == 1 and events:
            selected = _even_sample(events, cap)
        else:
            count = max(1, cap // 2, cap - len(events))
            uniform = _extract_frames(ffmpeg, video, spool, "uniform", resolution, start, end, count)
            by_time = {f.time_ms: f for f in events}
            by_digest = {f.digest: f for f in events}
            scaffold = _unique_frames([by_time.get(f.time_ms) or by_digest.get(f.digest, f) for f in uniform])
            used = {f.digest for f in scaffold}
            selected = scaffold + _even_sample([f for f in events if f.digest not in used], cap - len(scaffold))
        result = []
        for index, frame in enumerate(sorted(selected, key=lambda f: f.time_ms), 1):
            path = frame_dir / f"frame_{index:05d}.jpg"
            spool.seek(frame.offset)
            path.write_bytes(spool.read(frame.size))
            result.append({"time_ms": frame.time_ms, "path": str(path.resolve()), "reason": frame.reason})
        return result


def _unique_frames(frames: list[_Frame]) -> list[_Frame]:
    unique = {}
    for frame in frames:
        unique.setdefault(frame.digest, frame)
    return list(unique.values())


def _even_sample(frames: list[_Frame], cap: int) -> list[_Frame]:
    if len(frames) <= cap:
        return frames
    indices = {min(len(frames) - 1, math.floor((i + 0.5) * len(frames) / cap)) for i in range(cap)}
    return [frame for index, frame in enumerate(frames) if index in indices]


def _timestamp(groups: tuple[str | None, ...]) -> int:
    h, m, s, ms = groups
    return (int(h or 0) * 3600 + int(m) * 60 + int(s)) * 1000 + int(ms)


def parse_subtitle_file(path: Path) -> list[dict[str, Any]]:
    return _parse_subtitle_text(path.read_text(encoding="utf-8-sig", errors="replace"))


def _parse_subtitle_text(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    segments: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        match = SRT_TIME_RE.match(line) or VTT_TIME_RE.match(line)
        if not match:
            index += 1
            continue
        values = match.groups()
        start_groups = values[:4]
        end_groups = values[4:]
        index += 1
        body = []
        while index < len(lines) and lines[index].strip():
            cleaned = re.sub(r"<[^>]+>", "", lines[index]).strip()
            if cleaned:
                body.append(cleaned)
            index += 1
        if body:
            segments.append({
                "start_ms": _timestamp(start_groups),
                "end_ms": _timestamp(end_groups),
                "text": " ".join(body),
            })
        index += 1
    return segments


def _embedded_transcript(ffmpeg: Path, video: Path, tracks: list[int]) -> tuple[list[dict[str, Any]], str | None]:
    for index in tracks:
        try:
            result = subprocess.run([
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file",
                "-i", str(video), "-map", f"0:{index}", "-c:s", "srt", "-f", "srt", "pipe:1",
            ], stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            transcript = _parse_subtitle_text(result.stdout)
            if transcript:
                return transcript, f"embedded:{index}"
    return [], None


def _find_subtitle(video: Path, explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        return path
    for extension in (".srt", ".vtt"):
        candidate = video.with_suffix(extension)
        if candidate.is_file() and candidate != video:
            return candidate
    candidates = sorted(
        p for p in video.parent.iterdir()
        if p.is_file() and p.stem.startswith(f"{video.stem}.") and p.suffix.lower() in {".srt", ".vtt"}
    )
    return candidates[0] if candidates else None


def _write_srt(segments: list[dict[str, Any]], path: Path) -> None:
    def stamp(ms: int) -> str:
        hours, remain = divmod(ms, 3_600_000)
        minutes, remain = divmod(remain, 60_000)
        seconds, millis = divmod(remain, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
    blocks = [f"{i}\n{stamp(s['start_ms'])} --> {stamp(s['end_ms'])}\n{s['text']}" for i, s in enumerate(segments, 1)]
    path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")


def _transcription_backend(transcription: str) -> str:
    if transcription != "auto":
        return transcription
    if configured_model():
        return "local"
    for backend, variable in (("groq", "GROQ_API_KEY"), ("openai", "OPENAI_API_KEY")):
        if os.environ.get(variable):
            return backend
    return "none"


def _cached_asr(
    settings: Settings, video: Path, identity: dict[str, Any], ffmpeg: Path, ffprobe: Path,
    backend: str, local_options: dict[str, Any] | None, duration_ms: int,
) -> tuple[dict[str, Any], bool]:
    key = _cache_id({
        "version": ASR_CACHE_VERSION, "source": identity, "backend": backend,
        "options": local_options if backend == "local" else ENDPOINTS[backend][1],
    })
    directory = settings.work_root / "video_transcripts" / key
    path = directory / "transcript.json"
    with _ASR_LOCK:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8")), True
        if backend == "local":
            assert local_options is not None
            result = transcribe_local(video, ffmpeg, local_options, duration_ms=duration_ms or None)
        else:
            api_key = os.environ.get("GROQ_API_KEY" if backend == "groq" else "OPENAI_API_KEY")
            if not api_key:
                raise VideoObserverError(f"transcription={backend} 但未设置对应 API Key")
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=directory) as scratch:
                segments = transcribe_video(video, Path(scratch), ffmpeg, ffprobe, backend, api_key)
            result = {"segments": segments, "status": "available" if segments else "no_transcript", "evidence": None}
        directory.mkdir(parents=True, exist_ok=True)
        _write_json(path, result)
        return result, False


def _max_frame_gap(frames: list[dict[str, Any]], start_ms: int, end_ms: int) -> int | None:
    if not frames:
        return None
    points = [start_ms, *(item["time_ms"] for item in frames), end_ms]
    return max(right - left for left, right in zip(points, points[1:]))


def analyze_video(
    settings: Settings,
    source: str,
    *,
    detail: str = "efficient",
    start: str | float | int | None = None,
    end: str | float | int | None = None,
    max_frames: int | None = None,
    resolution: int = 512,
    subtitle_path: str | None = None,
    transcription: str = "auto",
    transcript_limit: int = 120,
    prefer_subtitles: bool = True,
) -> dict[str, Any]:
    if detail not in DETAIL_CAPS:
        raise VideoObserverError(f"detail 必须是 {sorted(DETAIL_CAPS)}")
    if transcription not in {"auto", "none", "local", "groq", "openai"}:
        raise VideoObserverError("transcription 必须是 auto/none/local/groq/openai")
    if type(transcript_limit) is not int or not 0 <= transcript_limit <= 500:
        raise VideoObserverError("transcript_limit 必须在 0 到 500 之间")
    cap = DETAIL_CAPS[detail] if max_frames is None else max_frames
    if type(cap) is not int or not 0 <= cap <= 500:
        raise VideoObserverError("max_frames 必须在 0 到 500 之间")
    backend = _transcription_backend(transcription)
    local_options = model_options() if backend == "local" else None
    video, downloaded = _resolve_source(settings, source)
    identity = _file_identity(video)
    subtitle = _find_subtitle(video, subtitle_path) if prefer_subtitles else None
    analysis_id = _cache_id({
        "version": ANALYSIS_VERSION, "source": identity,
        "subtitle": _file_identity(subtitle) if subtitle else None,
        "detail": detail, "start": start, "end": end, "max_frames": cap,
        "resolution": resolution, "transcription": transcription, "backend": backend,
        "local_options": local_options, "prefer_subtitles": prefer_subtitles,
    })
    work_dir = settings.work_root / "video_analyses" / analysis_id
    report_path = work_dir / "analysis.json"
    previous = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else None
    if previous and previous.get("transcript_status") != "unavailable":
        previous["cache_hit"] = True
        return _compact_report(previous, transcript_limit)

    ffmpeg = _require_tool(settings.ffmpeg_exe, "ffmpeg")
    ffprobe = _require_tool(settings.ffprobe_exe, "ffprobe")
    work_dir.mkdir(parents=True, exist_ok=True)
    metadata, text_tracks = (previous["metadata"], []) if previous else _probe(ffprobe, video)
    full_end = metadata["duration_ms"] / 1000
    start_s = _parse_time(start) or 0.0
    end_s = _parse_time(end) if end is not None else full_end
    if (not math.isfinite(start_s) or not math.isfinite(end_s) or start_s < 0
            or end_s <= start_s or (full_end and start_s >= full_end)):
        raise VideoObserverError("分析时间范围无效")
    end_s = min(end_s, full_end) if full_end else end_s
    start_ms, end_ms = round(start_s * 1000), round(end_s * 1000)
    if end_ms <= start_ms:
        raise VideoObserverError("分析时间范围不足 1 毫秒")

    transcript: list[dict[str, Any]] = parse_subtitle_file(subtitle) if subtitle else []
    transcript_source = "sidecar" if transcript else None
    if prefer_subtitles and not transcript and text_tracks:
        transcript, transcript_source = _embedded_transcript(ffmpeg, video, text_tracks)
    transcript_status = "available" if transcript else "disabled" if transcription == "none" else "no_transcript"
    transcript_warning = None
    audio_evidence = None
    asr_cache_hit = False
    if not transcript and transcription != "none":
        if not metadata["has_audio"]:
            transcript_status = "no_audio"
        elif backend != "none":
            transcript_source = f"whisper:{backend}"
            try:
                result, asr_cache_hit = _cached_asr(
                    settings, video, identity, ffmpeg, ffprobe, backend, local_options, metadata["duration_ms"],
                )
                transcript = result["segments"]
                transcript_status = result["status"]
                if result["evidence"] is not None:
                    audio_evidence = {"scope": "source", **result["evidence"]}
            except LocalASRError as exc:
                transcript_status = "unavailable"
                transcript_warning = str(exc)
                if exc.evidence is not None:
                    audio_evidence = {"scope": "source", **exc.evidence}
    transcript = [s for s in transcript if s["end_ms"] >= start_ms and s["start_ms"] <= end_ms]
    if not transcript and transcript_status == "available":
        transcript_status = "no_transcript"
    srt_path = work_dir / "transcript.srt"
    if transcript:
        _write_srt(transcript, srt_path)

    frames = previous["frames"] if previous else []
    if cap and metadata["has_video"] and not previous:
        frame_dir = Path(tempfile.mkdtemp(prefix="frames-", dir=work_dir))
        frames = _sample_frames(ffmpeg, video, frame_dir, detail, resolution, start_s, end_s, cap)

    report = {
        "success": True,
        "analysis_id": analysis_id,
        "cache_hit": False,
        "source": source,
        "local_video_path": str(video),
        "source_identity": identity,
        "downloaded": downloaded,
        "metadata": metadata,
        "range": {"start_ms": start_ms, "end_ms": end_ms},
        "detail": detail,
        "frames": frames,
        "frame_count": len(frames),
        "max_frame_gap_ms": _max_frame_gap(frames, start_ms, end_ms),
        "transcript": transcript,
        "transcript_source": transcript_source,
        "transcript_status": transcript_status,
        "transcript_warning": transcript_warning,
        "audio_evidence": audio_evidence,
        "asr_cache_hit": asr_cache_hit,
        "transcript_srt_path": str(srt_path) if transcript else None,
        "report_path": str(report_path),
        "privacy": "本地 Whisper 不上传媒体；仅云端 Whisper 上传压缩音频。",
        "limitations": ["代表帧不是完整视频流", "字幕为语句级而非逐字对齐", "精确卡点和口型同步需要额外分析"],
    }
    _write_json(report_path, report)
    return _compact_report(report, transcript_limit)


def _compact_report(report: dict[str, Any], transcript_limit: int = 120) -> dict[str, Any]:
    result = dict(report)
    transcript = list(report.get("transcript") or [])
    result["transcript_total"] = len(transcript)
    result["transcript_truncated"] = len(transcript) > transcript_limit
    result["transcript"] = transcript[:transcript_limit]
    return result


def get_analysis_context(
    settings: Settings,
    analysis_id: str,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
    offset: int = 0,
    limit: int = 120,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{20}", analysis_id):
        raise VideoObserverError("analysis_id 格式无效")
    if offset < 0 or limit < 1 or limit > 500:
        raise VideoObserverError("offset 必须非负，limit 必须在 1 到 500 之间")
    report_path = (settings.work_root / "video_analyses" / analysis_id / "analysis.json").resolve()
    if not report_path.is_file():
        raise FileNotFoundError(report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    range_start = int(report["range"]["start_ms"] if start_ms is None else start_ms)
    range_end = int(report["range"]["end_ms"] if end_ms is None else end_ms)
    if range_start < 0 or range_end <= range_start:
        raise VideoObserverError("上下文时间范围无效")
    transcript = [
        item for item in report.get("transcript", [])
        if int(item["end_ms"]) >= range_start and int(item["start_ms"]) <= range_end
    ]
    frames = [
        item for item in report.get("frames", [])
        if range_start <= int(item["time_ms"]) <= range_end
    ]
    page = transcript[offset:offset + limit]
    observations_path = report_path.parent / "observations.json"
    return {
        "success": True,
        "analysis_id": analysis_id,
        "range": {"start_ms": range_start, "end_ms": range_end},
        "metadata": report.get("metadata"),
        "frames": frames,
        "transcript": page,
        "transcript_source": report.get("transcript_source"),
        "transcript_status": report.get("transcript_status"),
        "transcript_warning": report.get("transcript_warning"),
        "audio_evidence": report.get("audio_evidence"),
        "max_frame_gap_ms": _max_frame_gap(frames, range_start, range_end),
        "transcript_page": {
            "offset": offset,
            "returned": len(page),
            "total_in_range": len(transcript),
            "has_more": offset + len(page) < len(transcript),
            "next_offset": offset + len(page) if offset + len(page) < len(transcript) else None,
        },
        "transcript_srt_path": report.get("transcript_srt_path"),
        "observations_path": str(observations_path) if observations_path.is_file() else None,
        "report_path": str(report_path),
    }


def save_observations(
    settings: Settings,
    analysis_id: str,
    observations: list[dict[str, Any]],
    edit_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{20}", analysis_id):
        raise VideoObserverError("analysis_id 格式无效")
    root = (settings.work_root / "video_analyses" / analysis_id).resolve()
    report_path = root / "analysis.json"
    if not report_path.is_file():
        raise FileNotFoundError(report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    analysis_start_ms = int(report["range"]["start_ms"])
    end_ms = int(report["range"]["end_ms"])
    for item in observations:
        time_ms = int(item.get("time_ms", -1))
        if time_ms < analysis_start_ms or time_ms > end_ms or not str(item.get("description", "")).strip():
            raise VideoObserverError("每条 observation 需要范围内的 time_ms 和 description")
    for item in edit_candidates or []:
        start_ms = int(item.get("start_ms", -1)); candidate_end = int(item.get("end_ms", -1))
        if start_ms < analysis_start_ms or candidate_end <= start_ms or candidate_end > end_ms:
            raise VideoObserverError("edit_candidate 时间范围无效")
    payload = {
        "analysis_id": analysis_id,
        "observations": observations,
        "edit_candidates": edit_candidates or [],
    }
    output = root / "observations.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"success": True, "analysis_id": analysis_id, "observations_path": str(output), "counts": {"observations": len(observations), "edit_candidates": len(edit_candidates or [])}}
