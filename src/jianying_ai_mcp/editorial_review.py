"""Choose editorial review samples from the actual compiled timings."""
from __future__ import annotations

from math import floor
from typing import TYPE_CHECKING, Any

from .motion_library import motion_item_starts

if TYPE_CHECKING:
    from .storyboard import Beat


_REPORT = "记录时间段、预期、实见和结论。用实际帧时码标出要改的位置，再回查同一处。"
_PROJECT = "用 look_openfilm(project_path, start=检查时点) 看工程。动效素材及剪映原生导出的成片可用 review_export 抽检。"
_MP4 = "用 review_export(media_path=mp4_path, start=区间起点, end=区间终点) 查看短区间，按 frames[].time_ms 记录实见。"

_RECIPE_FOCUS = {
    "hook": "先认出主标题，停稳时读完副标题，检查首行入场时有无裁字。",
    "headline": "按正常速度读完标题，长行入场和停稳时都在栏内。",
    "chapter": "先辨认章节号，再读到完整节名，衔接下一镜时仍记得主题。",
    "list": "每个要点随相应口播出现，最后一项完整可读，上一项留在原位。",
    "steps": "新步骤随动作词出现，按顺序能指出当前步骤，上一项的位置稳定。",
    "checklist": "观察当前条目出现和勾选完成的先后，尾项也留够阅读时间。",
    "compare": "在同一帧读全两栏，标签、单位和比较口径一致。",
    "number": "观察数字变化，停稳后对照原始数值、正负号和单位，最大位数没有挤出画面。",
    "quote": "逐字对照引语及出处，末行和署名在同一稳定画面可读。",
    "end": "看清署名和行动指引，最后一个字出现后仍能从容读完。",
}
_ANIMATION_FOCUS = {
    "none": "对比首尾的排版和字体，最长的一行在目标尺寸可读。",
    "slide_in": "检查文字从下方进入、减速落位，移动中和落位后都没有裁字。",
    "slide_right": "检查文字从左侧进入，停稳时对齐左边界，整行可读。",
    "fade_in": "看文字从显现到清晰的过程，停稳后与背景对比足够。",
    "wipe_in": "观察揭示边缘经过首字和末字，停稳后整行完整。",
    "photo_reveal": "看揭示边缘经过图片时主体可辨，停稳后脸或产品细节都在画内。",
    "count_up": "看数字增长或递减至目标值，尾帧的符号和单位准确，标签位置稳定。",
    "custom_html": "补查场景设计的关键事件。先跳到后段再回到同一秒，两次的文字、位置和数值应一致。",
}


def _sample_times(times: list[float], last: float, offset: float = 0) -> list[float]:
    return sorted({round(offset + max(0, min(t, last)), 6) for t in times})


def recipe_check(recipe: str, data: dict, duration: float, fps: int, offset: float = 0) -> dict[str, Any]:
    # OpenFilm rounds the number of output frames to the nearest integer (half up).
    last = max(0, floor(duration * fps + 0.5) - 1) / fps
    starts = motion_item_starts(recipe, data.get("items", []))
    check: dict[str, Any] = {"recipe": recipe, "focus": _RECIPE_FOCUS.get(recipe, "在目标画幅确认入场、落位和尾帧可读。")}
    times = [0, last]
    if starts:
        check["item_starts_s"] = [round(offset + t, 6) for t in starts]
        # Sample the second entrance, when both the old and new information matter.
        visible = [t for t in starts if t < last]
        if visible:
            at = visible[min(1, len(visible) - 1)]
            times.extend((at - 1 / fps, at + 0.2, at + 0.7, visible[-1] + 0.7))
        if starts[-1] + 0.7 > last:
            check["timing_note"] = "尾项入点接近或超出最后可见帧。检查入场和读完的余量，必要时前移入点或延长本镜。"
    else:
        times.extend((duration * 0.25, duration * 0.6))
    if recipe == "number":
        unit = data.get("unit")
        check["target"] = f"{data['value']:,}{str(unit).strip() if unit is not None else ''}"
    frame_times = [floor(t * fps + 0.5) / fps for t in times]
    check["sample_times_s"] = _sample_times(frame_times, last, offset)
    return check


def motion_clip_review(result: dict, data: dict) -> dict[str, Any]:
    rendered = bool(result.get("mp4_path"))
    fps = result["fps"]
    check = recipe_check(result["recipe"], data, result["duration"], fps)
    times = check["sample_times_s"]
    entrances = [t for t in check.get("item_starts_s", []) if t <= times[-1]]
    at = entrances[min(1, len(entrances) - 1)] if entrances else times[len(times) // 2]
    if rendered:
        # The MP4 range is exclusive at the end. Include the last drawable frame.
        frame_count = round(times[-1] * fps) + 1
        end_ms = frame_count * 1000 // fps
        next_call = {"tool": "review_export", "arguments": {
            "media_path": result["mp4_path"], "start": round(max(0, at - 0.12), 6),
            "end": min(end_ms / 1000, round(at + 0.7, 6)), "max_frames": 12,
        }}
    else:
        next_call = {"tool": "look_openfilm", "arguments": {
            "project_path": result["project_path"], "start": min(times[-1], round(at + 0.2, 6)),
        }}
    return {
        "timebase": "镜头内秒", "status": "已渲染，待目检" if rendered else "已生成工程，待预览",
        **check, "next_call": next_call, "inspect": _MP4 if rendered else _PROJECT, "report": _REPORT,
    }


def animation_review(result: dict, entrance: float, value: int | None, unit: str) -> dict[str, Any]:
    # The project has continuous time; these are suggested seeks, not measured video frames.
    effect = result.get("motion", "custom_html")
    duration = result["duration"]
    last = duration - 1 / 30
    if effect == "custom_html":
        times = [0, duration * 0.25, duration * 0.6, last]
    elif effect == "none":
        times = [0, last]
    else:
        times = [0, entrance / 2, entrance, (duration + entrance) / 2, last]
    samples = _sample_times(times, last)
    return {
        "timebase": "镜头内秒", "status": "已生成工程，待预览", "effect": effect,
        "sample_times_s": samples, "focus": _ANIMATION_FOCUS.get(effect, "对照入场与稳定画面复核所选效果。"),
        **({"target": f"{value:,}{unit.strip()}"} if effect == "count_up" else {}),
        "next_call": {"tool": "look_openfilm", "arguments": {"project_path": result["project_path"], "start": samples[1]}},
        "inspect": _PROJECT, "report": _REPORT,
    }


_CAPTION_FOCUS = {
    "single": "看入场、停稳和句尾。带声核对原话，完整读完这一句。",
    "blocks": "看各块依次进入后的最挤画面，阅读顺序明确，块间留有空隙。",
    "words": "看短语前缀逐段累加，和原声重音对齐，停稳时没有重影。",
}


def _handoff(group: dict, index: int, beats: list[Beat], fps: int) -> dict:
    old, new = group["cues"][index - 1:index + 1]
    number = group["beat"]
    beat = beats[number - 1]
    at = new["start_frame"] / fps
    return {
        "beat": number, "old": old["text"], "new": new["text"], "at_s": round(at, 6),
        "start_s": round(max(beat.at, at - 0.12), 6),
        "end_s": round(min(beat.at + beat.seconds, at + 0.30), 6),
        "timing": group["timing"],
    }


def _caption_checks(groups: list[dict], beats: list[Beat], fps: int) -> list[dict]:
    by_mode: dict[str, list[dict]] = {}
    for group in groups:
        if group["cues"]:
            by_mode.setdefault(group["options"]["mode"], []).append(group)
    checks = []
    for mode, matching in by_mode.items():
        pairs = [(g, i) for g in matching for i in range(1, len(g["cues"]))
                 if g["cues"][i].get("caption_group") is not None
                 and g["cues"][i].get("caption_group") == g["cues"][i - 1].get("caption_group")] if mode in ("stack", "blocks") else []
        if pairs:
            first = _handoff(*pairs[0], beats, fps)
            last = _handoff(*pairs[-1], beats, fps)
            checks.append({
                "mode": mode, "handoffs": [first] if first == last else [first, last],
                "focus": "比较第二项入点前、中、后的连续帧。前项位置和字号恒定，新项完整显现，间距紧凑、文字清晰。",
            })
        else:
            group = matching[0]
            cue = max(group["cues"], key=lambda c: len(c["text"]))
            checks.append({
                "mode": mode, "beat": group["beat"], "text": cue["text"], "timing": group["timing"],
                "start_s": round(cue["start_frame"] / fps, 6), "end_s": round(cue["end_frame"] / fps, 6),
                "effective_mode": "single" if mode in ("stack", "blocks") else mode,
                "focus": "只有一句或缺少递进断点时按单句呈现，检查普通入场。" if mode in ("stack", "blocks") else _CAPTION_FOCUS[mode],
            })
    return checks


def episode_review(beats: list[Beat], raw_beats: list[dict], groups: list[dict], fps: int, *, project: bool) -> dict:
    priority = [
        i for i in range(1, len(beats))
        if raw_beats[i].get("chapter") or raw_beats[i].get("role") or beats[i].kind != beats[i - 1].kind
        or beats[i].layout.get("with") != beats[i - 1].layout.get("with")
    ]
    cuts = []
    if len(beats) > 1:
        candidates = list(dict.fromkeys([*priority, 1, len(beats) - 1]))[:2]
        for i in sorted(candidates):
            left, right = beats[i - 1], beats[i]
            cuts.append({
                "from_beat": i, "to_beat": i + 1, "at_s": round(right.at, 6),
                "start_s": round(max(left.at, right.at - 0.35), 6),
                "end_s": round(right.at + min(right.seconds, 0.35), 6),
            })
    if cuts:
        storyboard = {"cuts": cuts, "focus": "静音看切点两侧能否找到新主体，再带声核对原话、换气和动作结果。"}
    else:
        storyboard = {"start_s": 0, "end_s": round(beats[0].seconds, 6),
                      "focus": "本片只有一镜，检查首帧能否找到主体，末尾是否看见结果。"}
    result: dict[str, Any] = {
        "timebase": "成片秒", "status": "已编译，待目检",
        "storyboard": storyboard,
        "inspect": ("先用 look_openfilm 在 openfilm_project_path 预览。" if project else "")
                   + "导出后对实际交付的 MP4 调用 review_export，用 start、end 传短区间，读取 frames[].time_ms。",
        "report": _REPORT,
    }
    captions = _caption_checks(groups, beats, fps)
    if captions:
        result["captions"] = captions
    graphics = {}
    for i, beat in enumerate(beats):
        if beat.kind == "graphic":
            placement, card = "full", raw_beats[i]
        elif beat.layout.get("with", "").startswith("graphic:"):
            placement, card = "panel", raw_beats[i]["layout"]["with"]["graphic"]
        else:
            continue
        if placement not in graphics:
            graphics[placement] = {
                "beat": i + 1, "placement": placement,
                **({"panel": beat.layout["panel"]} if placement == "panel" else {}),
                **recipe_check(card["recipe"], card["data"], beat.seconds, fps, offset=beat.at),
            }
    if graphics:
        result["graphics"] = list(graphics.values())
    return result
