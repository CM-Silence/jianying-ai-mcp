"""Place selected footage in program time while keeping the primary edit and mix intact."""
from __future__ import annotations

import math
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from PIL import Image, ImageOps

from .broll_library import resolve_broll_shot
from .config import Settings
from .editing_modules.layouts import InterviewLayout, read_interview_layout
from .media_info import parse_media_info
from .media_prepare import needs_upright_copy, run_ffmpeg
from .native_stock import stock_panel
from .production_graphics import ProductionError, absolute_file, write_contact_sheet
from .production_layouts import draw_layout_background, prepare_layout_panel


def _seconds(value: Any, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ProductionError(f"{label} 需要非负有限秒数")
    return float(value)


def _layout(row: dict, width: int, height: int) -> InterviewLayout | None:
    raw = row["layout"]
    if not isinstance(raw, dict):
        raise ProductionError("broll.layout 需要包含 kind 的对象")
    if raw == {"kind": "full"}:
        return None
    if raw.keys() - {"kind", "main", "split", "focus", "broll_focus", "fit", "background"}:
        raise ProductionError("broll.layout 支持 kind、main、split、focus、broll_focus、fit、background")
    layout = {k: raw[k] for k in ("kind", "main", "background") if k in raw}
    if "split" in raw:
        layout["top_fraction"] = raw["split"]
    if "focus" in raw:
        layout["bottom_focus"] = raw["focus"]
    layout["top"] = {"source": row["source"], "fit": raw.get("fit", "cover")}
    if "broll_focus" in raw:
        layout["top"]["focus"] = raw["broll_focus"]
    return read_interview_layout(layout, width, height, "broll")


def plan_broll(
    settings: Settings, rows: list[dict] | None, *, fps: int, total_frames: int, width: int, height: int,
) -> list[dict[str, Any]]:
    """Resolve the indexed source and validate both the program and source windows."""
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ProductionError("broll 必须为数组")
    planned = []
    info_by_path = {}
    for index, item in enumerate(rows):
        label = f"broll[{index}]"
        if not isinstance(item, dict) or item.keys() - {"shot_id", "source", "source_start", "at", "duration", "layout"}:
            raise ProductionError(f"{label} 支持 shot_id 或 source、source_start、at、duration、layout")
        if ("shot_id" in item) == ("source" in item):
            raise ProductionError(f"{label} 需要 shot_id 或 source 其中之一")
        at = _seconds(item.get("at"), f"{label}.at")
        duration = _seconds(item.get("duration"), f"{label}.duration")
        first, last = round(at * fps), round((at + duration) * fps)
        if first >= last or last > total_frames:
            raise ProductionError(f"{label} 的成片区间须至少一帧且位于时间线内")
        record = None
        if "shot_id" in item:
            if not isinstance(item["shot_id"], str) or not item["shot_id"].strip():
                raise ProductionError(f"{label}.shot_id 必须为非空文字")
            record = resolve_broll_shot(settings, item["shot_id"])
            if record["availability"] == "editor_only":
                raise ProductionError(f"{label} 是已在剪映预览的官方素材，当前文件仅支持编辑器播放。"
                                      "请先绑定官方素材采集稿，或用 availability=timeline_ready 检索。")
        path = str(absolute_file(record["source"] if record else item["source"], label))
        native = record is not None and record["availability"] == "native_draft"
        if native:
            info = {"type": "video", "duration": record["duration_us"] / 1_000_000}
        else:
            if path not in info_by_path:
                info_by_path[path] = parse_media_info(settings, path)["media_info"]
            info = info_by_path[path]
        if info["type"] not in {"image", "video"}:
            raise ProductionError(f"{label} 需要含画面的视频或图片")
        source_start = _seconds(item.get("source_start", record["start_ms"] / 1000 if record else 0),
                                f"{label}.source_start")
        source_end = source_start + (last - first) / fps
        if record and (source_start < record["start_ms"] / 1000 or source_end > record["end_ms"] / 1000 + 1e-6):
            raise ProductionError(f"{label} 源区间超出了索引镜头")
        if info["type"] == "image":
            if source_start:
                raise ProductionError(f"{label} 静态图的 source_start 应为零")
        elif source_end > (info.get("video_duration") or info["duration"] or 0) + 1e-6:
            raise ProductionError(f"{label} 源区间超出了素材时长")
        row = {
            "source": path, "source_start": source_start, "at": first / fps, "duration": (last - first) / fps,
            "start_frame": first, "end_frame": last, "layout": item.get("layout", {"kind": "full"}),
            "volume": 0, **({"shot_id": item["shot_id"]} if record else {}),
        }
        if native:
            row.update(native_size=[record["width"], record["height"]], asset_id=record["asset_id"])
        _layout(row, width, height)
        planned.append(row)
    planned.sort(key=lambda row: row["start_frame"])
    if any(a["end_frame"] > b["start_frame"] for a, b in zip(planned, planned[1:])):
        raise ProductionError("broll 的成片区间不能相互重叠")
    return planned


def speaker_panel_windows(rows: list[dict] | None, *, fps: int, width: int, height: int) -> list[tuple]:
    """Caption geometry uses the very same rectangles and frame boundaries as the picture tracks."""
    windows = []
    for row in rows if isinstance(rows, list) else []:
        layout = row.get("layout") if isinstance(row, dict) else None
        if not isinstance(layout, dict) or layout.get("kind") not in {"stacked", "interview_box"}:
            continue
        placement = _layout({**row, "source": ""}, width, height)
        at = _seconds(row.get("at"), "broll.at")
        end = at + _seconds(row.get("duration"), "broll.duration")
        windows.append((round(at * fps), round(end * fps), placement.main_rect))
    return windows


def broll_tracks(
    settings: Settings, folder: Path, rows: list[dict], primary: list[tuple[dict, bool]], *,
    fps: int, width: int, height: int,
) -> list[dict]:
    """Add only silent picture layers. The primary camera's source audio remains continuous."""
    mattes, speakers, footage = [], [], []
    media = {}
    for index, row in enumerate(rows):
        first, last = (round(row[k] * 1_000_000 / fps) for k in ("start_frame", "end_frame"))
        duration = last - first
        stamp = f"broll-{index + 1:03d}"
        clip = {"source": row["source"], "source_start": round(row["source_start"] * 1_000_000),
                "source_duration": duration, "start": first, "duration": duration, "volume": 0}
        layout = _layout(row, width, height)
        if "native_size" in row:
            panel = stock_panel(row["native_size"], layout.secondary_rect if layout else (0, 0, width, height),
                                (width, height), layout.top_focus if layout else (.5, .5),
                                layout.top_fit if layout else "cover")
            footage.append({**clip, **panel, "stock_shot_id": row["shot_id"]})
            if layout is None:
                continue
        else:
            if clip["source"] not in media:
                media[clip["source"]] = parse_media_info(settings, clip["source"])["media_info"]
            info = media[clip["source"]]
            photo = info["type"] == "image"
        if layout is None:
            if needs_upright_copy(info):
                clip = prepare_layout_panel(settings, folder, clip, False, (0, 0, width, height), (.5, .5),
                                            name=stamp, width=width, height=height, fps=fps)
            else:
                # Native and OpenFilm both aspect-fit the source. Enlarge to cover the canvas completely.
                ratio = (info["display_width"] / info["display_height"]) / (width / height)
                scale = max(ratio, 1 / ratio)
                clip["clip"] = {"scale_x": scale, "scale_y": scale}
            footage.append(clip)
            continue

        matte = folder / f"{stamp}-background.png"
        draw_layout_background(matte, layout, width, height)
        mattes.append({**clip, "source": str(matte), "source_start": 0})
        if "native_size" not in row:
            footage.append(prepare_layout_panel(
                settings, folder, clip, photo, layout.secondary_rect, layout.top_focus, name=f"{stamp}-insert",
                width=width, height=height, fps=fps, fit=layout.top_fit, background=layout.background,
            ))
        # One silent crop per intersecting primary clip, including cuts inside one B-roll insert.
        for cut, (base, is_photo) in enumerate(primary):
            a = max(first, base["start"])
            b = min(last, base["start"] + base["duration"])
            if a >= b:
                continue
            rate = base["source_duration"] / base["duration"]
            offset = round((a - base["start"]) * rate)
            end_offset = round((b - base["start"]) * rate)
            speaker = {"source": base["source"], "start": a, "duration": b - a, "volume": 0,
                       "source_start": 0 if is_photo else base["source_start"] + offset,
                       "source_duration": end_offset - offset}
            speakers.append(prepare_layout_panel(
                settings, folder, speaker, is_photo, layout.main_rect, layout.bottom_focus, name=f"{stamp}-main-{cut}",
                width=width, height=height, fps=fps,
            ))
    return [{"type": "video", "name": name, "segments": segments} for name, segments in (
        ("补充镜头底板", mattes), ("补充镜头人物", speakers), ("补充镜头", footage),
    ) if segments]


def broll_preview(
    settings: Settings, folder: Path, tracks: list[dict], width: int, height: int, maximum: int,
) -> tuple[Path, int]:
    """Sample the edited picture at cuts and insert boundaries, prioritising the selected B-roll."""
    visuals = [t for t in tracks if t["type"] == "video"]
    layers = [(t["name"], s) for t in visuals for s in t["segments"]]
    boundaries = sorted({p for _, s in layers for p in (s["start"], s["start"] + s["duration"])})
    samples = sorted(((a + b) // 2 for a, b in zip(boundaries, boundaries[1:])),
                     key=lambda p: not any(s["start"] <= p < s["start"] + s["duration"]
                                           for s in visuals[-1]["segments"]))
    scale = 320 / max(width, height)
    size = (round(width * scale), round(height * scale))
    frames = []
    media = {}
    with TemporaryDirectory(dir=folder) as temp:
        for i, point in enumerate(sorted(samples[:maximum])):
            canvas = Image.new("RGB", size)
            for name, clip in layers:
                if not clip["start"] <= point < clip["start"] + clip["duration"]:
                    continue
                source = Path(clip["source"])
                if source not in media:
                    media[source] = parse_media_info(settings, str(source))["media_info"]
                info = media[source]
                if info["type"] != "image":
                    offset = (point - clip["start"]) * clip["source_duration"] / clip["duration"]
                    target = Path(temp) / "frame.png"
                    seek = (clip["source_start"] + offset) / 1e6
                    # In the final source-frame interval the next PTS is already EOF.
                    for time in (seek, max(0, seek - 1 / (info["frame_rate"] or 24))):
                        run_ffmpeg(settings, [
                            "-protocol_whitelist", "file", "-ss", f"{time:.6f}",
                            "-i", str(source), "-map", "0:V:0", "-frames:v", "1", "-vf",
                            "scale=320:320:force_original_aspect_ratio=decrease", "-threads", "1", str(target),
                        ])
                        if target.is_file():
                            break
                    else:
                        raise ProductionError(f"无法从 {source.name} 在 {seek:.3f} 秒提取预览帧")
                    source = target
                rect = clip.get("panel_rect", (0, 0, width, height))
                left, top, w, h = (round(v * scale) for v in rect)
                with Image.open(source) as raw:
                    im = ImageOps.exif_transpose(raw).convert("RGBA")
                    fit = ImageOps.fit if name == "补充镜头" or "panel_rect" in clip else ImageOps.contain
                    tile = fit(im, (max(1, w), max(1, h)))
                    canvas.paste(tile, (left + (w - tile.width) // 2, top + (h - tile.height) // 2), tile)
                if source.parent == Path(temp):
                    source.unlink()
            frame = Path(temp) / f"{i}.png"
            canvas.save(frame)
            frames.append((frame, f"{point / 1e6:.2f} s"))
        output = folder / "shot-preview.jpg"
        write_contact_sheet(frames, output)
    return output, len(frames)
