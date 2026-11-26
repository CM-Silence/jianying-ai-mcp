"""Keep the complete animated text group clear of faces in the displayed footage."""
from __future__ import annotations

from collections import defaultdict
from itertools import product
from math import ceil
from pathlib import Path
from threading import local
from unicodedata import east_asian_width

import cv2
import numpy as np

from .production_graphics import ProductionError


_thread = local()


def _detect(frame):
    if not hasattr(_thread, "detector"):
        xml = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        _thread.detector = cv2.CascadeClassifier(str(xml))
        if _thread.detector.empty():
            raise ProductionError("无法加载本机人脸检测模型")
    h, w = frame.shape[:2]
    scale = min(1, 480 / max(w, h))
    preview = cv2.resize(frame, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
    gray = cv2.equalizeHist(cv2.cvtColor(preview, cv2.COLOR_BGR2GRAY))
    h, w = gray.shape
    return [(int(x) / w, int(y) / h, int(x + dx) / w, int(y + dy) / h)
            for x, y, dx, dy in _thread.detector.detectMultiScale(gray, 1.1, 5, minSize=(18, 18))]


def _sample(clip):
    """Source-normalized boxes, indexed by the fraction of the visible clip elapsed."""
    if clip.get("stock_shot_id"):
        return []
    path = Path(clip["source"])
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}:
        frame = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return []
        return [(0.5, frame.shape[:2], _detect(frame))]
    capture = cv2.VideoCapture(str(path))
    samples = []
    try:
        capture.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1)
        count = max(3, min(90, ceil(clip["source_duration"] / 750_000)))
        for i in range(count):
            pos = (i + 0.5) / count
            capture.set(cv2.CAP_PROP_POS_MSEC, (clip["source_start"] + pos * clip["source_duration"]) / 1000)
            ok, frame = capture.read()
            if ok:
                samples.append((pos, frame.shape[:2], _detect(frame)))
    finally:
        capture.release()
    return samples


def _screen_faces(clip, start, end, width, height, cache):
    key = (clip["source"], clip["source_start"], clip["source_duration"], bool(clip.get("stock_shot_id")))
    if key not in cache:
        cache[key] = _sample(clip)
    samples = cache[key]
    if not samples:
        return [], False
    # Include neighbouring samples when a caption is shorter than the sampling interval.
    first = (start - clip["start"]) / clip["duration"]
    last = (end - clip["start"]) / clip["duration"]
    nearby = [s for s in samples if first - 1 / len(samples) <= s[0] <= last + 1 / len(samples)]
    boxes = []
    for _, (h, w), faces in nearby:
        transform = clip.get("clip", {})
        if clip.get("panel_rect"):
            x, y, dw, dh = clip["panel_rect"]
        else:
            fit = min(width / w, height / h)
            dw, dh = w * fit * transform.get("scale_x", 1), h * fit * transform.get("scale_y", 1)
            x = (width * (1 + transform.get("transform_x", 0)) - dw) / 2
            y = (height * (1 - transform.get("transform_y", 0)) - dh) / 2
        for l, t, r, b in faces:
            # Include forehead, chin and a little room around animated glyphs.
            mx, my = (r - l) * dw * .13, (b - t) * dh * .20
            boxes.append((x + l * dw - mx, y + t * dh - my, x + r * dw + mx, y + b * dh + my))
    return boxes, True


def _text_box(item, width, height):
    style, pos = item["style"], item["clip"]
    em = style["size"] * min(width, height) / 144
    limit = width * style.get("max_line_width", .9)
    lines = [max(em, em * sum(1 if east_asian_width(c) in "WFA" else .72 for c in line))
             for line in item["text"].split("\n")]
    w = min(limit, max(lines)) + .30 * em
    h = em * (1.35 * sum(max(1, ceil(line / limit)) for line in lines) + .35)
    cx, cy = (1 + pos.get("transform_x", 0)) * width / 2, (1 - pos["transform_y"]) * height / 2
    return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


def _shift(bounds, faces, region):
    l, t, r, b = bounds
    left, top, right, bottom = region
    if left <= l and top <= t and r <= right and b <= bottom and all(
        r <= x1 or l >= x2 or b <= y1 or t >= y2 for x1, y1, x2, y2 in faces
    ):
        return 0., 0.
    dx = {0., left - l, right - r}
    dy = {0., top - t, bottom - b}
    for x1, y1, x2, y2 in faces:
        dx.update((x1 - r, x2 - l))
        dy.update((y1 - b, y2 - t))
    for x, y in sorted(product(dx, dy), key=lambda p: (abs(p[0])*2 + abs(p[1]), abs(p[0]), -p[1], p[0])):
        if l+x < left-1e-6 or t+y < top-1e-6 or r+x > right+1e-6 or b+y > bottom+1e-6:
            continue
        if all(r+x <= x1+1e-6 or l+x >= x2-1e-6 or b+y <= y1+1e-6 or t+y >= y2-1e-6
               for x1, y1, x2, y2 in faces):
            return x, y
    return None


def protect_captions(items, tracks, width, height):
    """Move simultaneously visible lines together, using the actual cropped panel proxies."""
    groups = defaultdict(list)
    exclusions = defaultdict(set)
    for item in items:
        end = item["start"] + item["duration"]
        boxes = item.pop("_caption_exclusions", [])
        if item.pop("_adaptive_caption", False):
            groups[end].append(item)
            exclusions[end].update((l*width, t*height, r*width, b*height) for l,t,r,b in boxes)
    report = {"groups": len(groups), "moved_groups": 0, "sampled_groups": 0, "groups_with_faces": 0, "review": []}
    layers = {t["name"]: t["segments"] for t in tracks if t["type"] == "video"}
    cache = {}
    for end, group in groups.items():
        start = min(s["start"] for s in group)
        clips = [s for name in ("主画面", "补充镜头", "补充镜头人物") for s in layers.get(name, [])
                 if s["start"] < end and start < s["start"] + s["duration"]]
        boundaries = sorted({start, end, *(max(start, s["start"]) for s in clips),
                             *(min(end, s["start"] + s["duration"]) for s in clips)})
        faces, panels = [], []
        sampled = True
        for a, b in zip(boundaries, boundaries[1:]):
            # Speaker panel is on top of the main picture; a full B-roll hides it.
            clip = next((s for s in reversed(clips) if s["start"] <= a < s["start"] + s["duration"]), None)
            if clip is None:
                sampled = False
                continue
            found, ok = _screen_faces(clip, a, b, width, height, cache)
            sampled &= ok
            faces.extend(found)
            panels.append(clip.get("panel_rect") or (0, 0, width, height))
        if sampled:
            report["sampled_groups"] += 1
        else:
            report["review"].append({"start_us": start, "end_us": end, "code": "unreadable",
                                     "reason": "画面需要在剪映中复核"})
        if not panels:
            continue
        if faces:
            report["groups_with_faces"] += 1
        faces.extend(exclusions[end])
        boxes = [_text_box(s, width, height) for s in group]
        bounds = (min(v[0] for v in boxes), min(v[1] for v in boxes), max(v[2] for v in boxes), max(v[3] for v in boxes))
        rect = (max(p[0] for p in panels), max(p[1] for p in panels),
                min(p[0]+p[2] for p in panels), min(p[1]+p[3] for p in panels))
        if not faces and all(v >= low for v, low in zip(bounds[:2], rect[:2])) and bounds[2] <= rect[2] and bounds[3] <= rect[3]:
            continue
        move = _shift(bounds, faces, rect)
        if move is None:
            report["review"].append({"start_us": start, "end_us": end, "code": "no_safe_space",
                                     "reason": "字幕整组空间不足，请缩短文字或调整人物构图"})
        elif move != (0., 0.):
            for s in group:
                s["clip"] = {**s["clip"], "transform_x": s["clip"].get("transform_x", 0) + 2*move[0]/width,
                             "transform_y": s["clip"]["transform_y"] - 2*move[1]/height}
            report["moved_groups"] += 1
    return report
