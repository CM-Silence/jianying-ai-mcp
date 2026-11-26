"""Check the duration of the visible composition, independently of dialogue and caption cues."""
from __future__ import annotations

import math

from .production_graphics import ProductionError


def check_picture_pacing(shots: list[dict], inserts: list[dict], fps: int, style: str | None,
                         min_scene_seconds: float | None = None) -> None:
    minimum = (4 if style == "seller_reference" else 0) if min_scene_seconds is None else min_scene_seconds
    if type(minimum) not in (int, float) or not math.isfinite(minimum) or minimum < 0:
        raise ProductionError("min_scene_seconds 需要非负有限秒数")
    if not minimum:
        return
    # Touching source ranges stay one take when dialogue is split only for captions.
    ends, cameras, compositions = [], [], []
    cursor = 0
    previous_source, expected_source_frame = None, 0
    camera = 0
    for shot in shots:
        source, first = shot.get("_original_take", (shot["source"], shot.get("source_start_frame", 0)))
        if source != previous_source or (not shot.get("_still_image") and abs(first - expected_source_frame) > 1):
            camera += 1
        previous_source, expected_source_frame = source, first + shot.get("source_frames", shot["frames"])
        layout = shot.get("layout") or {}
        cameras.append((camera, shot.get("clip")))
        compositions.append((cameras[-1], layout if layout.get("kind") != "full" else {}))
        cursor += shot["frames"]
        ends.append(cursor)
    boundaries = sorted({0, *ends, *(point for row in inserts for point in (row["start_frame"], row["end_frame"]))})
    base = insert = 0
    scenes = []
    for start, end in zip(boundaries, boundaries[1:]):
        while ends[base] <= start:
            base += 1
        while insert < len(inserts) and inserts[insert]["end_frame"] <= start:
            insert += 1
        visible = compositions[base]
        if insert < len(inserts) and inserts[insert]["start_frame"] <= start:
            row = inserts[insert]
            layout = row["layout"]
            speaker = cameras[base] if layout["kind"] != "full" else None
            offset = round(row["source_start"] * fps) - row["start_frame"]
            visible = (speaker, row["source"], offset, layout)
        if scenes and scenes[-1][2] == visible:
            scenes[-1] = (scenes[-1][0], end, visible)
        else:
            scenes.append((start, end, visible))
    if len(scenes) > 1:
        short = [f"{a / fps:.2f}–{b / fps:.2f} 秒" for a, b, _ in scenes if (b - a) / fps < minimum]
        if short:
            raise ProductionError(f"分镜停留不足 {minimum:g} 秒（{'、'.join(short)}）。"
                                  "可延续同一构图、合并短暂回切，或重新选取更长的库存镜头。")
