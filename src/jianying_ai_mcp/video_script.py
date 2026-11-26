"""Freeze the A/V script before rendering. Stock choices refer back to its numbered scenes."""
from __future__ import annotations

import json
import math
from pathlib import Path

from .broll_library import get_broll_shot, search_broll_shots
from .broll_placement import place_broll_shots, publish_episode
from .file_io import atomic_text_writer
from .production_graphics import absolute_file, create_output_folder
from .storyboard import CAPTION_ENTRANCES, MAX_BEATS, estimate_seconds, read_episode_file, text_cues, timed_caption_cues


def _caption_plan(beat, spec, spoken, frames, fps):
    opts = beat.get("caption") or {}
    mode = opts.get("mode", spec.get("caption_mode", "blocks"))
    if mode not in CAPTION_ENTRANCES:
        raise ValueError(f"未知字幕模式 {mode}")
    if beat.get("captions") is False:
        return "关闭", [], ""
    vertical = spec.get("height", 1920) >= spec.get("width", 1080)
    max_chars = spec.get("caption_max_chars", 14 if vertical else 20)
    punctuation = spec.get("caption_punctuation", "spaced")
    if "cues" in opts:
        rows = timed_caption_cues(opts["cues"], 0, frames, fps, mode, punctuation, None, False,
                                  semantic="mode" not in opts and "caption_mode" not in spec, max_chars=max_chars)
        timing = "指定入点"
    elif spoken:
        rows = text_cues(spoken, offset=0, seconds=frames/fps, fps=fps, max_chars=max_chars,
                         punctuation=punctuation, mode=mode)
        timing = "按文长估时，回听校准"
    else:
        return "据原声分句", [], "依原片时码"
    grouped = any(r.get("caption_group") is not None for r in rows)
    label = ("逐项淡入，上下排列" if mode == "stack" else "逐项淡入，横排过宽转上下") if grouped else (
        "单条逐词" if mode == "words" else "单句")
    return label, [{"at": r["start_frame"]/fps, "text": r["text"]} for r in rows], timing


def _scenes(spec):
    fps = spec.get("fps", 30)
    beats = spec.get("beats")
    if not spec.get("name") or not isinstance(beats, list) or not 1 <= len(beats) <= MAX_BEATS:
        raise ValueError("视频脚本需要片名与非空 beats 分镜数组")
    if type(fps) is not int or not 1 <= fps <= 120:
        raise ValueError("fps 需为 1–120 的整数")
    cursor = 0
    for number, beat in enumerate(beats, 1):
        kind = beat.get("kind", "talk")
        voice = beat.get("voice") or {}
        duration = (voice["end"] - voice.get("start", 0) if voice else estimate_seconds({"beats": [beat]}))
        if kind not in ("talk", "media", "graphic") or not math.isfinite(duration) or duration <= 0:
            raise ValueError(f"第 {number} 镜需要明确类型与正时长")
        frames = max(1, round(duration * fps))
        layout = beat.get("layout") or {}
        panel = layout.get("with", {})
        stock = beat.get("stock")
        if stock is not None:
            if not isinstance(stock, dict) or not isinstance(stock.get("query"), str) or not stock["query"].strip():
                raise ValueError(f"第 {number} 镜请填写 stock.query 的可见主体和动作")
            if kind != "talk" or panel or set(stock) - {"query","layout"}:
                raise ValueError(f"第 {number} 镜的 stock 用在主讲拍，可指定 query 和 layout")
        spoken = beat.get("text") or voice.get("text")
        caption_label, cues, timing = _caption_plan(beat, spec, spoken, frames, fps)
        text = spoken or " / ".join(c["text"] for c in cues)
        if not text and kind == "talk":
            text = f"使用原片 {beat.get('start', 0):g}–{beat.get('end', beat.get('start', 0) + duration):g} 秒的同期声"
        card = panel.get("graphic") or (beat if kind == "graphic" else {})
        picture = ("剪映库存" if stock else "HTML 动画" if card else "实拍")
        composition = (stock.get("layout") or {}) if stock else layout
        yield {"scene": number, "at": cursor / fps, "duration": frames / fps, "audio": text or "按分镜声轨",
               "picture": picture, "layout": composition.get("kind", "full"), "main": composition.get("main", "bottom"),
               "intent": beat.get("intent") or "",
               "source": beat.get("source") or voice.get("source"), "recipe": card.get("recipe"),
               "source_start": (voice or beat).get("start", 0),
               "source_end": (voice or beat).get("end", (voice or beat).get("start", 0) + duration),
               "graphic_data": card.get("data"), "query": stock["query"] if stock else None,
               "caption": caption_label, "caption_cues": cues, "caption_timing": timing}
        cursor += frames


def _script_table(brief, name, scenes):
    def cell(value):
        return str(value).replace("|", "｜").replace("\n", " ")
    lines = [f"# {name}", "", brief, "", "| 成片区间 | 原片与取段 | 口播与声音 | 画面、构图与执行 | 字幕 |",
             "| --- | --- | --- | --- | --- |"]
    for s in scenes:
        layout = "全屏" if s["layout"] == "full" else ("上栏主讲" if s["main"] == "top" else "下栏主讲")
        visual = [s["picture"], layout, s["intent"], s["query"]]
        if data := s["graphic_data"]:
            visual.append(data.get("title") or s["recipe"])
            for item in data.get("items", []):
                if isinstance(item, str):
                    visual.append(item)
                elif isinstance(item, dict) and item.get("title"):
                    stamp = f'本镜 +{item["at"]:g}s ' if isinstance(item.get("at"), (int, float)) else ""
                    visual.append(stamp + str(item["title"]))
        timed = "、".join(f'本镜 +{c["at"]:g}s {c["text"]}' for c in s["caption_cues"])
        source = f'{Path(s["source"]).name} {s["source_start"]:g}–{s["source_end"]:g}s' if s["source"] else "生成"
        captions = " · ".join(filter(None, (s["caption"], timed, s["caption_timing"])))
        lines.append(f'| {s["at"]:.2f}–{s["at"]+s["duration"]:.2f}s | {cell(source)} | {cell(s["audio"])} | '
                     f'{cell(" · ".join(filter(None, visual)))} | {cell(captions)} |')
    return "\n".join(lines) + "\n"


def plan_script(settings, brief, spec, media_root=None):
    if not brief.strip():
        raise ValueError("请写明本期视频要讲清的问题")
    spec = {"style_preset": "seller_reference", **spec}
    scenes = list(_scenes(spec))
    for scene in scenes:
        if scene["query"]:
            args = {"min_duration_ms": math.ceil(scene["duration"] * 1000 - 1e-7), "source_pool": "jianying", "limit": 5}
            hits = search_broll_shots(settings, scene["query"], availability="timeline_ready", **args)["matches"]
            if not hits:
                hits = search_broll_shots(settings, scene["query"], availability="editor_only", **args)["matches"]
            scene["candidates"] = [{k: h[k] for k in ("shot_id","availability","description","start_ms","end_ms",
                                                   "preview_frame","asset_title","tags") if k in h} for h in hits]
    folder = create_output_folder(settings, "scripts")
    path = folder / "script.json"
    sheet = folder / "视频脚本.md"
    media_dir = str(Path(media_root).expanduser().resolve()) if media_root else None
    with atomic_text_writer(path) as f:
        json.dump({**spec, "video_script": {"version": 1, "brief": brief, "media_root": media_dir, "scenes": scenes}},
                  f, ensure_ascii=False, indent=2)
    sheet.write_text(_script_table(brief, spec["name"], scenes), encoding="utf-8")
    return {"script_path": str(path), "script_table_path": str(sheet), "scenes": scenes, "next_tool": "execute_video_script"}


def execute_script(settings, path, selections, media_root, dry_run, openfilm_project=False):
    file = absolute_file(path, "视频脚本")
    document = json.loads(file.read_text(encoding="utf-8-sig"))
    saved = document.get("video_script") or {}
    if saved.get("version") != 1:
        raise ValueError("请使用 plan_video_script 返回的 script_path")
    spec = read_episode_file(file, media_root or saved.get("media_root"))
    spec.pop("video_script")
    scenes = list(_scenes(spec))
    stock_scenes = {str(s["scene"]): s for s in scenes if s["query"]}
    if selections.keys() - stock_scenes.keys():
        raise ValueError("selections 的键须为库存场景编号")
    planned = {str(s["scene"]): s for s in saved["scenes"]}
    placements, pending, decisions = [], [], []
    for number, scene in stock_scenes.items():
        stock = spec["beats"][int(number)-1].pop("stock")
        choice = selections.get(number)
        if choice == "keep_camera":
            decisions.append({"scene": number, "status": "kept_camera", "query": scene["query"]})
            continue
        if not choice:
            pending.append({"scene": number, "query": scene["query"], "candidates": planned[number]["candidates"]})
            continue
        hit = get_broll_shot(settings, choice)
        if hit["source_pool"] != "jianying" or not hit["timeline_ready"]:
            pending.append({"scene": number, "shot_id": choice, "availability": hit["availability"], "source_pool": hit["source_pool"]})
            continue
        placements.append({"shot_id": choice, "at": scene["at"], "duration": scene["duration"],
                           **({"layout": stock["layout"]} if "layout" in stock else {})})
        decisions.append({"scene": number, "shot_id": choice, "query": scene["query"], "description": hit["description"]})
    if pending:
        return {"success": False, "status": "stock_selection_required", "pending": pending, "draft_path": None,
                "next_action": "Agent 核对真实画面后选 shot_id。无合适镜头时按场景填写 keep_camera，或修改脚本。"}
    options = {"dry_run": dry_run, "openfilm_project": openfilm_project}
    result = (place_broll_shots(settings, spec, placements, **options) if placements else
              publish_episode(settings, spec, **options))
    if project := result.get("openfilm_project_path"):
        result["render_call"] = {"tool": "render_openfilm", "arguments": {"project_path": project, "fps": spec.get("fps", 30)}}
    return {**result, "script_path": str(file), "stock_decisions": decisions}
