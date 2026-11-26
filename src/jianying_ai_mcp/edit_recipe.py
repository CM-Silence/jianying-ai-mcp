"""Freeze editorial choices and input identities before any delivery render."""

from __future__ import annotations

import hashlib
import json
import math
import os
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .draft_ops import _safe_name
from .editing_modules.layouts import read_interview_layout
from .episode_broll import plan_broll
from .jianying_audio_assets import _draft_file, resolve_jianying_audio
from .media_info import parse_media_info
from .motion_library import scene_html
from .picture_pacing import check_picture_pacing
from .production_graphics import ProductionError, absolute_file, create_output_folder, validate_canvas
from .production_styles import StylePreset
from .storyboard import MAX_BEATS, text_cues, transcript_cues
from .video_observer import analyze_video


Music = Literal["calm", "warm", "pulse"]


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PanelCard(_Record):
    recipe: str = Field(description="list_motion_recipes 返回的动效名，例如 steps、list、number。")
    data: dict[str, Any] = Field(description="该配方的文字、要点或数值。")
    kind: Literal["stacked", "interview_box"] = "stacked"
    main: Literal["top", "bottom"] = "bottom"
    split: float = Field(default=0.5, ge=0.30, le=0.65)

    def layout(self) -> dict:
        return {"kind": self.kind, "main": self.main, "split": self.split,
                "with": {"graphic": {"recipe": self.recipe, "data": self.data}}}


class SoundReference(_Record):
    draft_path: str
    material_id: str


class EditClip(_Record):
    source: str = Field(min_length=1, description="本地文件。相对路径以 media_root 为准。")
    start: float = Field(default=0, ge=0, description="视频源入点，秒。")
    end: float | None = Field(default=None, gt=0, description="视频源出点，省略时用到片尾。")
    hold_seconds: float | None = Field(default=None, gt=0, description="静帧停留秒数，默认 3。")
    sound: bool = Field(default=True, description="保留视频原声，关闭时也跳过自动转写。")
    caption: str | None = Field(default=None, description="定稿字幕。省略时读取原声字幕或本地转写，空串关闭。")
    chapter: str | None = Field(default=None, max_length=2000, description="小节编排标记，仅写入时间线元数据。")
    caption_mode: Literal["single", "stack", "blocks", "words"] | None = Field(
        default=None, description="默认按语义决定。普通陈述为单句，递进或列举逐项淡入。")
    caption_sfx: bool | SoundReference = Field(default=True, description="只在标注的重点字幕入点使用。可指定真实剪映音效或关闭。")
    highlights: list[str] = Field(default_factory=list, description="字幕中需要强调的原词。")
    panel: PanelCard | None = Field(default=None, description="可选分屏图卡，用内置配方随镜头呈现要点。")


class Source(_Record):
    original: str
    relative: str | None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Cut(_Record):
    source_id: str
    first: int = Field(ge=0)
    frames: int = Field(gt=0)
    sound: bool
    chapter: str | None
    panel: PanelCard | None = None
    caption_mode: Literal["single", "stack", "blocks", "words"] = "single"
    caption_sfx: bool | SoundReference = False  # Recipes saved before accent tracks stay silent.
    caption_sfx_relative: str | None = None


class Caption(_Record):
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    text: str = Field(min_length=1)
    highlights: list[str] = Field(default_factory=list)
    duck_bgm: bool = False
    caption_group: int | None = None


class BrollCut(_Record):
    source_id: str
    shot_id: str | None = None
    native_stock: bool = False
    source_start: float = Field(ge=0)
    at_frame: int = Field(ge=0)
    frames: int = Field(gt=0)
    layout: dict | None = None

    def placement(self, path: str, fps: int) -> dict:
        item = {"source": path, "source_start": self.source_start, "at": self.at_frame / fps,
                "duration": self.frames / fps}
        if self.native_stock:
            item.pop("source")
            item["shot_id"] = self.shot_id
        if self.layout:
            item["layout"] = self.layout
        return item


class EditRecipe(_Record):
    adaptive_captions: bool = False
    schema_version: Literal[1]
    producer_version: str
    name: str
    width: int
    height: int
    fps: int = Field(ge=1, le=120)
    style_preset: StylePreset
    min_scene_seconds: float | None = Field(default=0, ge=0)  # Replay older, already-approved recipes.
    music: Music | None
    sources: dict[str, Source]
    cuts: list[Cut] = Field(min_length=1, max_length=MAX_BEATS)
    captions: list[Caption]
    broll: list[BrollCut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def total_frames(self) -> int:
        return sum(cut.frames for cut in self.cuts)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _media_dir(media_root: str) -> Path:
    root = Path(media_root).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ProductionError("media_root 需要存在的本地绝对目录")
    return root.resolve()


def _under_root(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if Path(relative).anchor or not path.is_relative_to(root):
        raise ProductionError(f"素材相对路径越出 media_root: {relative}")
    return absolute_file(str(path), "素材")


def _source_frames(clip: EditClip, info: dict, fps: int) -> tuple[int, int]:
    if info["type"] == "image":
        if clip.start or clip.end is not None:
            raise ProductionError("图片时长请使用 hold_seconds")
        return 0, max(1, round((clip.hold_seconds or 3) * fps))
    duration = info.get("video_duration") or info["duration"]
    if info["type"] != "video" or not duration or clip.hold_seconds is not None:
        raise ProductionError("请选择可读的视频或静态图片。hold_seconds 仅用于图片")
    end = clip.end if clip.end is not None else duration
    if not clip.start < end <= duration + 0.001:
        raise ProductionError(f"源区间须位于 0–{duration:g} 秒内: {clip.source}")
    first = round(clip.start * fps)
    last = min(round(end * fps), math.floor((duration + 0.0005) * fps))
    if last <= first:
        raise ProductionError(f"源区间不足一个项目帧: {clip.source}")
    return first, last - first


def plan_edit(
    settings: Settings, name: str, clips: list[EditClip], *, media_root: str | None = None,
    style_preset: StylePreset = "editorial", music: Music | None = None,
    width: int = 1080, height: int = 1920, fps: int = 30,
    broll: list[dict[str, Any]] | None = None,
    min_scene_seconds: float | None = None,
) -> dict[str, Any]:
    name = _safe_name(name, "片名")
    validate_canvas(width, height)
    if width % 2 or height % 2 or not 1 <= fps <= 120 or not 1 <= len(clips) <= MAX_BEATS:
        raise ProductionError(f"画幅边长须为偶数，fps 为 1–120，镜头数为 1–{MAX_BEATS}")
    root = _media_dir(media_root) if media_root else None
    paths = [
        _under_root(root, clip.source) if root and not Path(clip.source).is_absolute()
        else absolute_file(clip.source, "素材") for clip in clips
    ]
    if root is None:
        try:
            root = Path(os.path.commonpath([path.parent for path in paths]))
        except ValueError:  # Sources on separate volumes can be rebound by ID.
            pass
    sources = {}
    catalog = {}
    for index, path in enumerate(dict.fromkeys(paths), 1):
        key = f"m{index:04d}"
        sources[key] = Source(original=str(path), sha256=_digest(path),
                              relative=path.relative_to(root).as_posix() if root and path.is_relative_to(root) else None)
        catalog[path] = key, parse_media_info(settings, str(path))["media_info"]

    cuts, captions, warnings = [], [], []
    transcripts: dict[Path, list] = {}
    cursor = 0
    for clip, path in zip(clips, paths, strict=True):
        key, info = catalog[path]
        first, frames = _source_frames(clip, info, fps)
        sound = clip.sound and info["has_audio"]
        chapter = (clip.chapter or "").strip() or None
        if clip.panel:
            box = read_interview_layout({"kind": clip.panel.kind, "main": clip.panel.main, "top_fraction": clip.panel.split,
                                         "top": {"source": "panel"}}, width, height, "panel")
            validate_canvas(*box.secondary_rect[2:])
            if not 0.6 <= frames / fps <= 30:
                raise ProductionError("带动效分屏的镜头需为 0.6–30 秒")
            scene_html(clip.panel.recipe, clip.panel.data, duration=frames / fps, style_preset=style_preset)
        accent, relative = clip.caption_sfx, None
        if isinstance(accent, SoundReference):
            source = Path(accent.draft_path)
            if root and not source.is_absolute():
                source = (root / source).resolve()
                if not source.is_relative_to(root):
                    raise ProductionError("相对音效草稿路径需位于 media_root 内")
            file = _draft_file(str(source))
            accent = accent.model_copy(update={"draft_path": str(file)})
            if resolve_jianying_audio(settings, accent.model_dump())["kind"] != "sound_effect":
                raise ProductionError("caption_sfx 需要剪映音效素材")
            if root and file.is_relative_to(root):
                relative = file.relative_to(root).as_posix()
        cuts.append(Cut(source_id=key, first=first, frames=frames, sound=sound, chapter=chapter, panel=clip.panel,
                        caption_mode=clip.caption_mode or "blocks", caption_sfx=accent, caption_sfx_relative=relative))
        options = dict(offset=cursor / fps, fps=fps, highlights=clip.highlights, duck=sound,
                       max_chars=14 if height >= width else 20, mode=clip.caption_mode or "blocks")
        if clip.caption is None and sound:
            if path not in transcripts:
                analysis = analyze_video(settings, str(path), detail="transcript", max_frames=0,
                                         transcription="local", transcript_limit=0)
                if analysis["transcript_status"] == "unavailable":
                    raise ProductionError(f"{path.name} 本地转写不可用: {analysis['transcript_warning']}")
                transcripts[path] = json.loads(Path(analysis["report_path"]).read_text(encoding="utf-8"))["transcript"]
            rows = transcript_cues(transcripts[path], source_start=first / fps,
                                   source_end=(first + frames) / fps, **options)
            if not rows:
                warnings.append(f"镜头 {len(cuts)} 的选段未识别到对白，请复听。")
        else:
            rows = text_cues(clip.caption or "", seconds=frames / fps, **options)
        if clip.caption_mode is None and not any(r.get("caption_group") is not None for r in rows):
            cuts[-1].caption_mode = "single"
        for row in rows:
            row["start_frame"] = max(row["start_frame"], captions[-1].end_frame if captions else 0)
            row["end_frame"] = min(row["end_frame"], cursor + frames)
            if row["end_frame"] > row["start_frame"]:
                captions.append(Caption(**row))
        cursor += frames
    inserts = []
    planned = []
    if broll is not None:
        placements = []
        for row in broll:
            if root and isinstance(row.get("source"), str) and not Path(row["source"]).is_absolute():
                row = {**row, "source": str(_under_root(root, row["source"]))}
            placements.append(row)
        source_ids = {Path(s.original): key for key, s in sources.items()}
        planned = plan_broll(settings, placements, fps=fps, total_frames=cursor, width=width, height=height)
        for row in planned:
            path = Path(row["source"])
            if path not in source_ids:
                key = f"m{len(sources) + 1:04d}"
                sources[key] = Source(original=str(path), sha256=_digest(path),
                                      relative=path.relative_to(root).as_posix() if root and path.is_relative_to(root) else None)
                source_ids[path] = key
            inserts.append(BrollCut(source_id=source_ids[path], shot_id=row.get("shot_id"),
                                    native_stock="native_size" in row,
                                    source_start=row["source_start"], at_frame=row["start_frame"],
                                    frames=row["end_frame"] - row["start_frame"], layout=row.get("layout")))
    check_picture_pacing([{"source": c.source_id, "source_start_frame": c.first, "frames": c.frames,
                           "_still_image": catalog[path][1]["type"] == "image",
                           "layout": c.panel.model_dump() if c.panel else None} for c, path in zip(cuts, paths)],
                         planned, fps, style_preset, min_scene_seconds)
    recipe = EditRecipe(schema_version=1, producer_version=version("jianying-ai-mcp"), name=name,
                        min_scene_seconds=min_scene_seconds, adaptive_captions=True,
                        width=width, height=height, fps=fps, style_preset=style_preset, music=music,
                        sources=sources, cuts=cuts, captions=captions, broll=inserts, warnings=warnings)
    dest = create_output_folder(settings, "recipes") / "edit.json"
    dest.write_text(recipe.model_dump_json(indent=2), encoding="utf-8")
    return {"recipe_path": str(dest), "media_root": str(root) if root else None, "total_frames": cursor,
            "duration_s": cursor / fps, "fps": fps, "cuts": [cut.model_dump() for cut in cuts],
            "caption_count": len(captions), "broll": [item.model_dump() for item in inserts],
            "warnings": warnings, "next_tool": "deliver_video_edit"}


def read_edit_inputs(
    path: str, media_root: str | None, replacements: dict[str, str] | None,
) -> tuple[EditRecipe, list[dict], list[dict]]:
    recipe = EditRecipe.model_validate_json(absolute_file(path, "剪辑配方").read_bytes())
    _safe_name(recipe.name, "片名")
    validate_canvas(recipe.width, recipe.height)
    root = _media_dir(media_root) if media_root else None
    replacements = replacements or {}
    if replacements.keys() - recipe.sources.keys():
        raise ProductionError("换路径时请使用配方中的素材 ID")
    paths = {}
    native_sources = {cut.source_id for cut in recipe.broll if cut.native_stock}
    for key, source in recipe.sources.items():
        if key in native_sources:
            actual = absolute_file(source.original, key)
            if key in replacements and absolute_file(replacements[key], key) != actual:
                raise ProductionError("原生素材绑定剪映共享缓存，在新设备上请重新绑定镜头并生成配方")
        elif key in replacements:
            actual = absolute_file(replacements[key], key)
        elif root:
            if source.relative is None:
                raise ProductionError(f"{key} 需要用 replacements 指定迁移后的素材")
            actual = _under_root(root, source.relative)
        else:
            actual = absolute_file(source.original, key)
        if _digest(actual) != source.sha256:
            raise ProductionError(f"{key} 素材内容已变化，请还原原素材或用新素材重新调用 plan_video_edit")
        paths[key] = str(actual)
    shots = []
    for cut in recipe.cuts:
        if root and cut.caption_sfx_relative and isinstance(cut.caption_sfx, SoundReference):
            file = _under_root(root, cut.caption_sfx_relative)
            cut.caption_sfx = cut.caption_sfx.model_copy(update={"draft_path": str(file)})
        if cut.source_id not in paths:
            raise ProductionError(f"镜头引用未知素材 {cut.source_id}")
        shots.append({"source": paths[cut.source_id], "frames": cut.frames, "source_start_frame": cut.first,
                      "volume": int(cut.sound), "chapter": cut.chapter or None})
    inserts = []
    for cut in recipe.broll:
        if cut.source_id not in paths:
            raise ProductionError(f"B-roll 引用未知素材 {cut.source_id}")
        inserts.append(cut.placement(paths[cut.source_id], recipe.fps))
    return recipe, shots, inserts


def read_edit(path: str, media_root: str | None, replacements: dict[str, str] | None) -> tuple[EditRecipe, list[dict]]:
    recipe, shots, _ = read_edit_inputs(path, media_root, replacements)
    return recipe, shots
