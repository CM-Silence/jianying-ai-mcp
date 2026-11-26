"""Publish selected B-roll with an episode and report the actual serialized video segments."""
from __future__ import annotations

import json
from pathlib import Path

from .broll_library import get_broll_shot
from .config import Settings
from .draft_ops import build_manifest
from .storyboard import compile_episode


def place_broll_shots(settings: Settings, spec: dict, placements: list[dict], *, dry_run: bool = True,
                      openfilm_project: bool = False) -> dict:
    if not placements:
        raise ValueError("至少选择一个库存选段")
    if not isinstance(spec.get("broll", []), list):
        raise ValueError("spec.broll 必须为数组")
    rows = list(spec.get("broll", []))
    for row in placements:
        if row not in rows:
            rows.append(row)
    blocked = []
    for key in dict.fromkeys(r["shot_id"] for r in rows if isinstance(r, dict) and "shot_id" in r):
        shot = get_broll_shot(settings, key)
        if not shot["timeline_ready"]:
            blocked.append({field: shot[field] for field in ("shot_id", "availability", "next_action")})
    if blocked:
        return {"success": False, "status": "not_timeline_ready", "blocked": blocked, "draft_path": None}

    episode = {"style_preset": "seller_reference", **spec, "broll": rows}
    return publish_episode(settings, episode, dry_run=dry_run, openfilm_project=openfilm_project)


def publish_episode(settings: Settings, episode: dict, *, dry_run: bool, openfilm_project: bool = False) -> dict:
    compiled = compile_episode(settings, episode, include_manifest=True, openfilm_project=openfilm_project)
    safety = compiled.get("caption_safety") or {}
    if any(issue["code"] == "no_safe_space" for issue in safety.get("review", [])):
        return {"success": False, "status": "caption_layout_required", "draft_path": None, "caption_safety": safety,
                "timeline_path": compiled["timeline_path"], "warnings": compiled.get("warnings", [])}
    published = build_manifest(settings, compiled["manifest"], dry_run=dry_run)
    planned = compiled.get("broll", [])
    result = {**published, "status": "validated" if dry_run else "placed",
              "timeline_path": compiled["timeline_path"], "warnings": compiled.get("warnings", []),
              "caption_safety": compiled.get("caption_safety"), "openfilm_project_path": compiled.get("openfilm_project_path")}
    if dry_run:
        return {**result, "planned": planned}

    try:
        content = json.loads((Path(published["draft_path"]) / "draft_content.json").read_text(encoding="utf-8"))
        readback = _read_placements(content, compiled) if planned else []
        captions = _caption_readback(content)
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        return {**result, "success": False, "status": "readback_mismatch", "error": str(exc)}
    return {**result, "readback": readback, "caption_readback": captions}


def _caption_readback(content: dict) -> list[dict]:
    texts = {m["id"]: json.loads(m["content"]) for m in content["materials"].get("texts", []) if m.get("content")}
    anims = {m["id"]: m.get("animations", []) for m in content["materials"].get("material_animations", [])}
    rows = []
    for t in content["tracks"]:
        if t["type"] != "text" or not t["name"].startswith("字幕"):
            continue
        for s in t["segments"]:
            body = texts[s["material_id"]]
            rows.append({"text": body["text"], "target_range_us": s["target_timerange"],
                         "position": s["clip"]["transform"], "sizes": sorted({r["size"] for r in body["styles"]}),
                         "entrances": [a["name"] for ref in s.get("extra_material_refs", []) for a in anims.get(ref, [])
                                       if a.get("type") == "in"]})
    return sorted(rows, key=lambda r: r["target_range_us"]["start"])


def _read_placements(content: dict, compiled: dict) -> list[dict]:
    stock_track = next(t for t in content["tracks"] if t["type"] == "video" and t["name"] == "补充镜头")
    expected = next(t["segments"] for t in compiled["manifest"]["tracks"] if t["name"] == "补充镜头")
    speaker_segments = next((t["segments"] for t in content["tracks"] if t["name"] == "补充镜头人物"), [])
    segments = {s["target_timerange"]["start"]: s for s in stock_track["segments"]}
    materials = {m["id"]: m for m in content["materials"]["videos"]}
    readback = []
    if len(segments) != len(compiled["broll"]):
        raise ValueError("回读的补充镜头数量与分镜不符")
    for row, clip in zip(compiled["broll"], expected, strict=True):
        start = clip["start"]
        end = start + clip["duration"]
        segment = segments.get(start)
        if not segment or segment["target_timerange"]["duration"] != end - start or segment["volume"] != 0:
            raise ValueError(f"补充镜头的成片区间或音量与分镜不符，预期起点 {start} 微秒")
        material = materials[segment["material_id"]]
        if Path(material["path"]) != Path(clip["source"]) or segment["source_timerange"]["start"] != clip["source_start"]:
            raise ValueError(f"补充镜头的源媒体或入点与分镜不符，成片起点 {start} 微秒")
        if "asset_id" in row and material.get("material_id") != row["asset_id"]:
            raise ValueError(f"原生素材身份与镜头 {row['shot_id']} 不符")
        speakers = [{"target_range_us": s["target_timerange"], "volume": s["volume"],
                     "transform": s["clip"]["transform"]} for s in speaker_segments
                    if start <= s["target_timerange"]["start"] < end]
        readback.append({"shot_id": row.get("shot_id"), "layout": row["layout"], "track": stock_track["name"],
                         "segment_id": segment["id"], "target_range_us": segment["target_timerange"],
                         "selected_source_start_s": row["source_start"], "track_source_range_us": segment["source_timerange"],
                         "track_media_path": material["path"], "asset_id": row.get("asset_id"),
                         "draft_material_id": segment["material_id"], "speaker_panel_segments": speakers,
                         "volume": segment["volume"], "transform": segment["clip"]["transform"]})
    return readback
