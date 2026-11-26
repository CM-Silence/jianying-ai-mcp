from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
from typing import Any

from .audio_production import create_music_bed
from .config import Settings
from .desktop_export import export_or_handoff, manual_export
from .draft_ops import build_manifest
from .edit_recipe import EditRecipe, read_edit_inputs
from .export_review import GREEN_PERCENT, GREEN_SIMILARITY, MAX_REVIEW_SECONDS, review_export
from .media_info import count_video_frames, parse_media_info
from .media_prepare import portable_shot_source
from .openfilm import look_openfilm
from .production import compile_video_timeline
from .production_graphics import ProductionError, absolute_file, create_output_folder
from .caption_placement import zone_anchor
from .episode_broll import speaker_panel_windows
from .editing_modules.layouts import read_interview_layout
from .caption_text import caption_entities
from .production_styles import get_production_style
from .sound_design import caption_accent_tracks, item_accent_tracks
from .storyboard import CAPTION_SIZE, _caption_items, compile_split_layout


def _fingerprints(settings: Settings, mp4: str, srt: str | None, has_audio: bool) -> dict:
    # Hash decoded picture/sound so container metadata and local project paths do not affect a match.
    command = [str(settings.ffmpeg_exe), "-v", "error", "-nostdin", "-i", mp4, "-map", "0:V:0", "-map", "0:a:0?",
               "-c:v", "rawvideo", "-pix_fmt", "yuv420p", "-threads:v", "1", "-c:a", "pcm_s16le",
               "-f", "streamhash", "-hash", "sha256", "-"]
    output = subprocess.run(command, stdin=subprocess.DEVNULL, check=True, capture_output=True, text=True, timeout=3600).stdout
    streams = dict(re.findall(r"^\d+,([va]),SHA256=([0-9a-f]{64})\s*$", output, flags=re.MULTILINE))
    if "v" not in streams or (has_audio and "a" not in streams):
        raise ProductionError("无法核对成片的解码画面或音频指纹")
    return {"decoded_video": streams["v"], "decoded_audio": streams.get("a"),
            "srt": hashlib.sha256(Path(srt).read_bytes()).hexdigest() if srt else None}


def _reference(path: str) -> dict:
    data = json.loads(absolute_file(path, "参照验收报告").read_text(encoding="utf-8"))
    hashes = data.get("sha256") if isinstance(data, dict) else None
    if (not isinstance(hashes, dict) or hashes.keys() != {"decoded_video", "decoded_audio", "srt"}
            or hashes["decoded_video"] is None or not all(
                value is None or (isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value))
                for value in hashes.values())):
        raise ProductionError("reference_review_path 需要指向含解码画面、音频和 SRT 指纹的 review.json")
    return hashes


def _check_green_previews(settings: Settings, project: str | None, segments: list[dict], recipe: EditRecipe) -> list[dict]:
    """Check a suspect run against the authored stage, once per affected shot."""
    from PIL import Image
    import numpy as np

    if not project:
        return []
    evidence = []
    start = 0
    for cut in recipe.cuts:
        end = start + cut.frames
        for segment in segments:
            first = max(start, round(segment["start_ms"] * recipe.fps / 1000))
            last = min(end, round(segment["end_ms"] * recipe.fps / 1000))
            if first >= last:
                continue
            moment = ((first + last - 1) // 2) / recipe.fps
            item = {"time_s": moment, "matches_preview": False}
            evidence.append(item)
            try:
                preview = look_openfilm(settings, project, start=moment)["artifact_paths"][0]
                with Image.open(preview) as image:
                    grid = np.asarray(image.convert("RGB").resize((32, 32), Image.Resampling.NEAREST), dtype=np.int32)
                delta = grid - np.array([0, 255, 0], dtype=np.int32)
                fraction = float(((delta ** 2).sum(axis=2) <= 3 * (255 * GREEN_SIMILARITY) ** 2).mean())
                item.update(preview_path=preview, green_fraction=round(fraction, 6), matches_preview=100 * fraction >= GREEN_PERCENT)
            except (RuntimeError, OSError, ValueError, IndexError) as exc:
                item["error"] = str(exc)
            if not item["matches_preview"]:
                return evidence
        start = end
    return evidence


def _review(settings: Settings, path: str, recipe: EditRecipe, project: str | Callable[[], str | None] | None, *, accents: bool = False) -> dict:
    info = parse_media_info(settings, path)["media_info"]
    duration = info["duration"]
    if not duration or info["type"] != "video":
        raise ValueError("导出的文件缺少有效视频轨或时长")
    if info["video_frames"] is None:
        info["video_frames"] = count_video_frames(settings, path)
    expected_sound = bool(accents or recipe.music or any(cut.sound for cut in recipe.cuts))
    checks = {
        "resolution": [info["display_width"], info["display_height"]] == [recipe.width, recipe.height],
        "fps": abs((info["frame_rate"] or 0) - recipe.fps) < 0.01,
        "duration": abs((info["video_duration"] or duration) * recipe.fps - recipe.total_frames) <= 1,
        "frames": info["video_frames"] == recipe.total_frames,
        "audio_track": info["has_audio"] or not expected_sound,
    }
    ranges = []
    start = 0
    while start < duration:
        end = min(start + MAX_REVIEW_SECONDS, duration)
        review = review_export(settings, path, start=start, end=end, max_frames=6)
        ranges.append({key: review[key] for key in ("range", "frames", "black_segments", "green_segments", "green_detection", "audio")})
        start = end
    suspects = [s for part in ranges for s in part["green_segments"]]
    evidence = []
    if suspects:
        try:
            preview = project() if callable(project) else project
            evidence = _check_green_previews(settings, preview, suspects, recipe)
        except (RuntimeError, OSError, ValueError) as exc:
            evidence = [{"matches_preview": False, "error": str(exc)}]
    scanned = sum(part["green_detection"]["frames_checked"] for part in ranges)
    checks["green_scan_complete"] = scanned > 0 and scanned == info["video_frames"]
    checks["green_frames"] = not suspects or bool(evidence) and all(item["matches_preview"] for item in evidence)
    warnings = []
    if any(part["black_segments"] for part in ranges):
        warnings.append("检测到黑场，请根据报告时间码检查是否为预期转场。")
    if expected_sound and not any(part["audio"]["integrated_lufs"] is not None for part in ranges):
        warnings.append("预期有声，未测得有效综合响度，请复听。")
    return {"passed": all(checks.values()), "checks": checks,
            "green_review": {"frames_checked": scanned, "suspect_frames": sum(part["green_detection"]["suspect_frames"] for part in ranges),
                             "segments": suspects, "preview_checks": evidence},
            "expected_frames": recipe.total_frames, "media_info": info, "ranges": ranges, "warnings": warnings}


def _recipe_captions(settings: Settings, recipe: EditRecipe) -> tuple[list[dict], list[dict]]:
    rows = [c.model_dump() for c in recipe.captions]
    profile = get_production_style(recipe.style_preset)
    reference = profile.reference_dialogue
    windows = speaker_panel_windows([row.placement("", recipe.fps) for row in recipe.broll],
                                   fps=recipe.fps, width=recipe.width, height=recipe.height)
    styled = reference or any(cut.caption_mode != "single" for cut in recipe.cuts)
    items, sounds, accents = [], [], []
    first = 0
    for cut in recipe.cuts:
        last = first + cut.frames
        cues = [c for c in rows if first <= c["start_frame"] < last]
        terms = list(dict.fromkeys(term for c in cues for term in c["highlights"]))
        choice = cut.caption_sfx if isinstance(cut.caption_sfx, bool) else cut.caption_sfx.model_dump()
        sounds.append((cues, terms, choice))
        if reference and cut.panel and choice is not False:
            accents.extend(item_accent_tracks(settings, cut.panel.recipe, cut.panel.data, first, cut.frames, recipe.fps))
        if (styled or windows or recipe.adaptive_captions) and cues:
            entities = caption_entities([(c["start_frame"] / recipe.fps, c["end_frame"] / recipe.fps, c["text"])
                                         for c in cues], mode=cut.caption_mode, groups=[c["caption_group"] for c in cues])
            zone = "upper" if cut.panel and cut.panel.main == "top" else "bottom"
            anchor = zone_anchor(zone).as_dict()
            if (reference and not cut.panel) or not styled:
                anchor["transform_y"] = profile.caption_position
            cut_windows = list(windows)
            if cut.panel:
                panel = read_interview_layout({"kind": cut.panel.kind, "main": cut.panel.main,
                                               "top_fraction": cut.panel.split, "top": {"source": "graphic"}},
                                              recipe.width, recipe.height, "panel")
                cut_windows.append((first, last, panel.main_rect))
            items.extend(_caption_items(entities, fps=recipe.fps, anchor=anchor,
                                        width=recipe.width, height=recipe.height,
                                        size=profile.caption_style.get("size", 6) if reference or not styled else CAPTION_SIZE,
                                        terms=terms, total_frames=recipe.total_frames, profile=profile,
                                        speaker_windows=cut_windows, use_srt_style=not styled,
                                        adaptive=recipe.adaptive_captions))
        first = last
    accents.extend(caption_accent_tracks(settings, sounds, recipe.fps, recipe.total_frames,
                                         [s["start_frame"] for s in accents], reference_dialogue=reference))
    return items, accents


def deliver_edit(settings: Settings, recipe_path: str, *, media_root: str | None = None,
                 replacements: dict[str, str] | None = None, reference_review_path: str | None = None) -> dict[str, Any]:
    recipe, shots, broll = read_edit_inputs(recipe_path, media_root, replacements)
    reference = _reference(reference_review_path) if reference_review_path else None
    job = create_output_folder(settings, "deliveries")
    archived = job / "edit.json"
    shutil.copyfile(recipe_path, archived)
    result = {"success": False, "recipe_path": recipe_path, "archived_recipe_path": str(archived),
              "delivery_dir": str(job), "report_path": str(job / "delivery.json"), "warnings": list(recipe.warnings)}
    if recipe.producer_version != version("jianying-ai-mcp"):
        result["warnings"].append(f"配方来自 {recipe.producer_version}，本机包装或渲染版本可能有差异。")
    stage = "prepare_media_assets"
    try:
        metadata = {path: parse_media_info(settings, path)["media_info"] for path in dict.fromkeys(s["source"] for s in shots)}
        for i, (cut, shot) in enumerate(zip(recipe.cuts, shots, strict=True)):
            stage = "prepare_media_assets"
            original = shot["source"]
            shot["source"], shot["source_start_frame"] = portable_shot_source(
                settings, Path(original), metadata[original], first=cut.first, frames=cut.frames,
                fps=recipe.fps, width=recipe.width, height=recipe.height, source_sha256=recipe.sources[cut.source_id].sha256,
            )
            if shot["source"] != original:
                shot["_original_take"] = (original, cut.first)
            if cut.panel:
                stage = "create_motion_clip"
                shot["layout"], _ = compile_split_layout(
                    settings, cut.panel.layout(), f"cuts[{i}].panel", seconds=cut.frames / recipe.fps,
                    frames=cut.frames, fps=recipe.fps, width=recipe.width, height=recipe.height,
                    style_preset=recipe.style_preset,
                )
        stage = "create_music_bed"
        bgm = None
        if recipe.music:
            length = recipe.total_frames / recipe.fps
            bed = create_music_bed(settings, min(600, max(1, length)), preset=recipe.music)
            bgm = {"source": bed["audio_path"], "loop": length > 600, "volume": 0.18, "duck_volume": 0.07}
        stage = "compile_video_timeline"
        caption_items, accents = _recipe_captions(settings, recipe)
        built = compile_video_timeline(
            settings, recipe.name, shots, width=recipe.width, height=recipe.height, fps=recipe.fps,
            captions=[c.model_dump() for c in recipe.captions], bgm=bgm, style_preset=recipe.style_preset,
            openfilm_project=False, include_manifest=True, caption_items=caption_items or None, audio=accents or None,
            broll=broll or None,
            min_scene_seconds=recipe.min_scene_seconds,
        )
        result.update({k: built[k] for k in ("manifest_path", "timeline_path", "srt_path", "openfilm_project_path")})
        if built["srt_path"]:
            result["srt_path"] = str(shutil.copyfile(built["srt_path"], job / "captions.srt"))
        result["warnings"].extend(built.get("warnings", []))
        result["caption_safety"] = built.get("caption_safety") or {}
        if any(issue["code"] == "no_safe_space" for issue in result["caption_safety"].get("review", [])):
            result.update(status="caption_layout_required", draft_ready=False, failed_stage="caption_layout",
                          error="字幕整组与人脸的可用空间不足，需调整标记的分镜。")
            Path(result["report_path"]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return result
        stage = "build_draft"
        # Publish under a fresh name in the editor's library, including on a failed export.
        manifest = built["manifest"]
        draft_name = f"{recipe.name[:80]}-{job.name.removeprefix('job-')}"
        manifest["draft"] = {**manifest["draft"], "name": draft_name}
        Path(built["manifest_path"]).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        draft = build_manifest(settings, manifest, dry_run=False)
        path = Path(draft["draft_path"])
        result.update(draft_name=path.name, draft_path=str(path), draft_ready=True, renderer="jianying")
        stage = "export_jianying_mp4"
        resolution = {720: "720P", 1080: "1080P", 1440: "2K", 2160: "4K"}.get(min(recipe.width, recipe.height))
        if resolution is None:
            export = manual_export(path, f"剪映自动导出菜单没有配方所需的 {recipe.width}×{recipe.height} 尺寸。")
        else:
            export = export_or_handoff(settings, path.name, resolution=resolution, framerate=recipe.fps)
        result["native_export"] = export
        if not export["success"]:
            result.update(export, failed_stage=stage)
            Path(result["report_path"]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return result
        stage = "review_export"
        result["mp4_path"] = str(shutil.copyfile(export["output_path"], job / "video.mp4"))
        def reference_picture_project() -> str | None:
            # Only render inspection stills if native output contains a suspect green interval.
            if any(cut.native_stock for cut in recipe.broll):
                return None
            preview = compile_video_timeline(settings, recipe.name, shots, width=recipe.width, height=recipe.height,
                                            fps=recipe.fps, captions=[c.model_dump() for c in recipe.captions],
                                            style_preset=recipe.style_preset, caption_items=caption_items or None,
                                            openfilm_project=True, broll=broll or None, min_scene_seconds=recipe.min_scene_seconds)
            return preview["openfilm_project_path"]

        review = _review(replace(settings, work_root=job), result["mp4_path"], recipe, reference_picture_project,
                         accents=bool(accents))
        for part in review["ranges"]:
            for frame in part["frames"]:
                frame["file"] = Path(frame["path"]).relative_to(job).as_posix()
        hashes = _fingerprints(settings, result["mp4_path"], result["srt_path"], review["media_info"]["has_audio"])
        review["sha256"] = hashes
        if reference is not None:
            review["reference_review_path"] = reference_review_path
            review["checks"].update({f"same_{key}": digest == reference[key] for key, digest in hashes.items()})
            review["passed"] = all(review["checks"].values())
        review_path = job / "review.json"
        review_path.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
        result.update(review_path=str(review_path), checks=review["checks"], success=review["passed"],
                      frames=review["media_info"]["video_frames"], duration_s=review["media_info"]["duration"])
        result["warnings"].extend(review["warnings"])
        if review["passed"]:
            result["status"] = "exported"
        else:
            error = ("导出画面有疑似异常绿帧，未能与工程预览吻合。见 review_path 中的 green_review。"
                     if not review["checks"]["green_frames"] else "成片与定剪规格或参照报告不一致，请查看 checks 和 review_path")
            result.update(manual_export(path, error), failed_stage=stage)
    except Exception as exc:
        result.update(failed_stage=stage, error_type=type(exc).__name__, error=str(exc))
        if result.get("draft_ready"):
            result.update(manual_export(Path(result["draft_path"]), str(exc)))
    if not result["success"] and "mp4_path" in result:
        result["unverified_mp4_path"] = result.pop("mp4_path")
    Path(result["report_path"]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
