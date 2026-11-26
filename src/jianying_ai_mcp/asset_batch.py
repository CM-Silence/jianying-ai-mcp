from __future__ import annotations

import json
import re
from collections import Counter
from itertools import islice
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import Settings
from .file_io import atomic_text_writer
from .media_info import parse_media_info
from .video_observer import analyze_video


MEDIA_EXTENSIONS = frozenset(
    ".mp4 .mov .mkv .webm .m4v .avi .mts .m2ts .mpg .mpeg .wmv .ts .flv .3gp .mxf "
    ".jpg .jpeg .png .webp .heic .heif .avif .bmp .tif .tiff .gif .apng "
    ".wav .mp3 .m4a .aac .flac .ogg .opus .aiff .wma".split()
)


def collect_local_media(sources: list[str], recursive: bool) -> list[Path]:
    if not sources:
        raise ValueError("请至少提供一个本地素材文件或目录")
    paths: dict[Path, None] = {}
    for source in sources:
        path = Path(source).expanduser()
        if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
            raise ValueError(f"素材必须使用本地绝对路径: {source}")
        if path.is_dir():
            entries = path.rglob("*") if recursive else path.iterdir()
            files = sorted(
                (entry for entry in entries if entry.suffix.lower() in MEDIA_EXTENSIONS and entry.is_file()),
                key=lambda entry: str(entry).casefold(),
            )
            for entry in files:
                paths[entry.resolve()] = None
        else:
            paths[path.resolve()] = None
    if not paths:
        raise ValueError("目录中没有可识别的媒体文件")
    return list(paths)


def _spread(items: list[Any], count: int) -> list[Any]:
    if count <= 0:
        return []
    if len(items) <= count:
        return items
    if count == 1:
        return [items[len(items) // 2]]
    return [items[round(i * (len(items) - 1) / (count - 1))] for i in range(count)]


def _read_transcript(source: dict[str, Any]) -> list[dict[str, Any]]:
    report = source.get("analysis_report_path")
    if not report:
        return []
    payload = json.loads(Path(report).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("transcript", []), list):
        raise ValueError("转写报告格式无效")
    return payload.get("transcript", [])


def _cue_clock(ms: int) -> str:
    seconds, milliseconds = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"


def _write_batch_transcript(directory: Path, sources: list[dict]) -> dict:
    target = directory / "transcripts.txt"
    total = 0
    unavailable = []
    with atomic_text_writer(target) as out:
        for source in sources:
            source_id = source["source_id"]
            out.write(f"[{source_id}] {source['media_path']}\n")
            lines = []
            issue = None
            try:
                cues = _read_transcript(source)
                if not cues:
                    issue = source.get("error") or source.get("transcript_warning")
                    if source.get("transcript_count") or source.get("transcript_status") == "unavailable":
                        issue = issue or "缺少可读的转写缓存"
                for cue in cues:
                    if not isinstance(cue, dict):
                        raise ValueError("对白记录格式无效")
                    flag = " [待复听]" if cue.get("quality") == "review" else ""
                    lines.append(f"{_cue_clock(cue['start_ms'])} --> {_cue_clock(cue['end_ms'])}{flag}  {cue['text']}\n")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                issue = f"转写缓存不可读：{exc}"
            out.writelines(lines)
            total += len(lines)
            if issue:
                unavailable.append(source_id)
            if issue or not lines:
                out.write(f"（{issue or '无对白记录'}）\n")
            out.write("\n")
    return {"transcript_path": str(target), "source_count": len(sources), "transcript_cue_count": total,
            "unavailable_source_ids": unavailable}


def _compact_evidence(source: dict[str, Any], cue: dict[str, Any], char_limit: int) -> dict[str, Any]:
    text = cue["text"]
    return {
        "source_id": source["source_id"],
        "start_ms": cue["start_ms"],
        "end_ms": cue["end_ms"],
        "text": text[:char_limit],
        **({"quality": "review"} if cue.get("quality") == "review" else {}),
        **({"text_truncated": True} if len(text) > char_limit else {}),
    }


def _source_summary(item: dict[str, Any]) -> dict[str, Any]:
    info = item.get("media_info", {})
    fields = (
        "source_id", "media_path", "transcript_status", "transcript_source", "transcript_count", "analysis_id",
        "transcript_warning", "preview_warning", "error",
    )
    return {**{key: item[key] for key in fields if key in item}, **{
        key: info[key] for key in ("type", "duration_ms", "resolution", "has_audio") if key in info
    }}


def _overview(sources: list[dict[str, Any]], char_budget: int) -> list[dict[str, Any]]:
    if not char_budget:
        return []
    available = [source for source in sources if source.get("transcript_count", 0)]
    char_per_cue = min(180, max(50, char_budget // max(1, min(24, len(available)))))
    selected = _spread(available, min(24, max(1, char_budget // char_per_cue)))
    queues = [(source, _spread(_read_transcript(source), 3)) for source in selected]
    evidence = []
    remaining = char_budget
    for slot in range(3):
        for source, cues in queues:
            # Start from the middle for each source, then add earlier and later context.
            order = [1, 0, 2] if len(cues) == 3 else list(range(len(cues)))
            if slot >= len(order) or not remaining:
                continue
            item = _compact_evidence(source, cues[order[slot]], min(remaining, char_per_cue))
            evidence.append(item)
            remaining -= len(item["text"])
    return evidence


def analyze_media_batch(
    settings: Settings,
    sources: list[str],
    *,
    recursive: bool = False,
    transcription: str = "local",
    max_preview_frames: int = 12,
    transcript_chars: int = 3500,
) -> dict[str, Any]:
    if transcription not in {"local", "none", "groq", "openai"}:
        raise ValueError("批量转写请选择 local、none、groq 或 openai")
    if type(max_preview_frames) is not int or not 0 <= max_preview_frames <= 24:
        raise ValueError("max_preview_frames 必须是 0–24 的整数")
    if type(transcript_chars) is not int or not 0 <= transcript_chars <= 12000:
        raise ValueError("transcript_chars 必须是 0–12000 的整数")
    paths = collect_local_media(sources, recursive)
    batch_id = uuid4().hex
    directory = settings.work_root / "media_batches" / batch_id
    directory.mkdir(parents=True)
    items: list[dict[str, Any]] = []
    for number, path in enumerate(paths, 1):
        row: dict[str, Any] = {"source_id": f"m{number:04d}", "media_path": str(path)}
        items.append(row)
        try:
            row["media_info"] = parse_media_info(settings, str(path))["media_info"]
        except (ValueError, RuntimeError, OSError) as exc:
            row["error"] = str(exc)

    visual = [item for item in items if item.get("media_info", {}).get("type") in {"video", "image"}]
    preview = _spread(visual, max_preview_frames)
    preview_ids = {item["source_id"] for item in preview}
    per_video = min(4, max_preview_frames // max(1, len(preview)))
    frames: list[dict[str, Any]] = []
    for row in items:
        info = row.get("media_info")
        if not info:
            continue
        source_id = row["source_id"]
        selected = source_id in preview_ids
        if info["type"] == "image":
            if selected:
                frames.append({"source_id": source_id, "path": row["media_path"], "time_ms": 0})
            continue
        try:
            report = analyze_video(
                settings, row["media_path"], detail="transcript", max_frames=0,
                transcription=transcription, transcript_limit=0,
            )
            row.update(
                analysis_id=report["analysis_id"], analysis_report_path=report["report_path"],
                transcript_status=report["transcript_status"], transcript_count=report["transcript_total"],
                transcript_source=report.get("transcript_source"), audio_evidence=report.get("audio_evidence"),
            )
            if report.get("transcript_warning"):
                row["transcript_warning"] = report["transcript_warning"]
        except (ValueError, RuntimeError, OSError) as exc:
            row.update(transcript_status="unavailable", transcript_warning=str(exc))
        if selected:
            try:
                visual_report = analyze_video(
                    settings, row["media_path"], detail="efficient", max_frames=per_video,
                    transcription="none", transcript_limit=0,
                )
                row["visual_analysis_id"] = visual_report["analysis_id"]
                frames.extend({"source_id": source_id, **frame} for frame in visual_report["frames"])
            except (ValueError, RuntimeError, OSError) as exc:
                row["preview_warning"] = str(exc)

    # Keep the full catalog locally. Only excerpts and path references enter the default response.
    catalog = directory / "batch.json"
    catalog.write_text(json.dumps({"sources": items, "frames": frames}, ensure_ascii=False, indent=2), encoding="utf-8")
    result: dict[str, Any] = {
        "success": any("media_info" in row for row in items),
        "batch_id": batch_id,
        "catalog_path": str(catalog),
        "source_counts": dict(Counter(item.get("media_info", {}).get("type", "error") for item in items)),
        "transcript_status_counts": dict(Counter(item["transcript_status"] for item in items if "transcript_status" in item)),
        "sources": [_source_summary(item) for item in items[:16]],
        "next_source_offset": 16 if len(items) > 16 else None,
        "dialogue_evidence": _overview(items, transcript_chars),
        "preview_frames": frames,
        **_write_batch_transcript(directory, items),
    }
    if frames:
        from .production_graphics import write_contact_sheet

        sheet = directory / "preview.jpg"
        try:
            write_contact_sheet(
                [(Path(frame["path"]), f'{frame["source_id"]}  {frame["time_ms"] / 1000:.2f}s') for frame in frames],
                sheet,
            )
            result["preview_sheet_path"] = str(sheet)
        except (OSError, ValueError) as exc:
            result["preview_warning"] = str(exc)
    return result


def load_batch_catalog(settings: Settings, batch_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", batch_id):
        raise ValueError("batch_id 格式无效")
    catalog = settings.work_root / "media_batches" / batch_id / "batch.json"
    return json.loads(catalog.read_text(encoding="utf-8"))


def export_media_batch_transcript(settings: Settings, batch_id: str) -> dict:
    rows = load_batch_catalog(settings, batch_id)["sources"]
    return {"batch_id": batch_id, **_write_batch_transcript(settings.work_root / "media_batches" / batch_id, rows)}


def get_media_batch_context(
    settings: Settings,
    batch_id: str,
    *,
    query: str | None = None,
    source_id: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    offset: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 60:
        raise ValueError("offset 必须非负，limit 必须是 1–60 的整数")
    if (start_ms is not None and start_ms < 0) or (end_ms is not None and end_ms < (start_ms or 0)):
        raise ValueError("时间范围无效")
    batch = load_batch_catalog(settings, batch_id)
    rows = batch["sources"]
    if source_id is not None:
        rows = [item for item in rows if item["source_id"] == source_id]
        if not rows:
            raise ValueError(f"批次中没有素材 {source_id}")
    if query is None and source_id is None and start_ms is None and end_ms is None:
        return {
            "batch_id": batch_id, "sources": [_source_summary(item) for item in rows[offset:offset + limit]],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
        }

    terms = (query or "").casefold().split()
    matches = (
        _compact_evidence(row, cue, 300)
        for row in rows
        for cue in _read_transcript(row)
        if (start_ms is None or cue["end_ms"] >= start_ms)
        and (end_ms is None or cue["start_ms"] <= end_ms)
        and all(term in cue["text"].casefold() for term in terms)
    )
    page = list(islice(matches, offset, offset + limit + 1))
    matched_ids = {item["source_id"] for item in page[:limit]}
    result = {
        "batch_id": batch_id,
        "sources": rows if source_id is not None else [_source_summary(item) for item in rows if item["source_id"] in matched_ids],
        "dialogue_evidence": page[:limit],
        "next_offset": offset + limit if len(page) > limit else None,
    }
    if source_id is not None:
        result["preview_frames"] = [frame for frame in batch["frames"] if frame["source_id"] == source_id]
    return result
