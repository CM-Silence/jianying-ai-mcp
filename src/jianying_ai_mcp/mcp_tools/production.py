from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context

from ..editing_modules import EditingModuleCategory
from ..editing_modules.catalog import list_editing_modules
from ..production import CaptionTheme, SrtMode, StylePreset, compile_video_timeline as compile_timeline
from ..production_graphics import GraphicKind, GraphicPreset, create_video_graphic as create_graphic
from ..production_styles import list_video_style_presets as list_styles
from ..workflow import making_guide
from .common import run_tool


async def list_video_style_presets(ctx: Context) -> dict[str, Any]:
    """列出可用于剪辑、图卡和动画的内置制作风格及剪映/OpenFilm 对应样式。"""
    return await run_tool(ctx, ["读取制作风格目录"], lambda _settings: list_styles())


async def list_video_editing_modules(
    ctx: Context, category: EditingModuleCategory | None = None, module_id: str | None = None,
) -> dict[str, Any]:
    """简列构图、字幕和文字标注模块。字幕制作查 category="caption"，传 module_id 查询参数和时间线示例。"""
    def catalog(_settings):
        result = list_editing_modules(category, module_id)
        if category == "caption" or (module_id is not None and result["modules"][0]["category"] == "caption"):
            result["production_guidance"] = making_guide("captions")
        return result

    return await run_tool(ctx, ["查询剪辑模块"], catalog)


async def create_video_graphic(
    kind: GraphicKind,
    title: str,
    ctx: Context,
    subtitle: str | None = None,
    eyebrow: str | None = None,
    items: list[dict[str, str]] | None = None,
    visible_items: int | None = None,
    image_path: str | None = None,
    image_focus: tuple[float, float] = (0.5, 0.5),
    preset: GraphicPreset = "editorial",
    colors: dict[str, str] | None = None,
    width: int = 1080,
    height: int = 1920,
    font_path: str | None = None,
    style_preset: StylePreset | None = None,
) -> dict[str, Any]:
    """本地制作封面、标题、数据卡、片尾或透明角标，返回可用 PNG 素材及紧凑 JPEG 预览。

    kind 可选 cover/title/data/end/lower_third。items 为最多六项 {label,value?,detail?}，
    visible_items 指定显示前几项并高亮最新项。image_focus 为横纵裁切重心，取 0–1。
    preset 可选 editorial/midnight/clean。style_preset 提供配色与版型，colors 可单独覆盖颜色，格式为 #RRGGBB。
    自定义图片、字体必须用本地绝对路径。图卡文字已渲染，需在剪映编辑的字直接用时间线文字叠加。
    """
    return await run_tool(
        ctx,
        ["检查图卡文字与素材", "绘制图卡并生成预览"],
        lambda settings: create_graphic(
            settings, kind, title, subtitle=subtitle, eyebrow=eyebrow, items=items, visible_items=visible_items,
            image_path=image_path, image_focus=image_focus, preset=preset, colors=colors, width=width, height=height,
            font_path=font_path, style_preset=style_preset,
        ),
        threaded=True,
    )


async def compile_video_timeline(
    name: str,
    shots: list[dict[str, Any]],
    ctx: Context,
    fps: int = 30,
    width: int = 1080,
    height: int = 1920,
    narration: list[dict[str, Any]] | None = None,
    audio: list[dict[str, Any]] | None = None,
    captions: list[dict[str, Any]] | None = None,
    bgm: dict[str, Any] | None = None,
    srt_mode: SrtMode = "text",
    caption_style: dict[str, Any] | None = None,
    caption_clip: dict[str, Any] | None = None,
    preview_frames: int = 0,
    mixdown: bool = False,
    include_manifest: bool = False,
    openfilm_project: bool = False,
    style_preset: StylePreset | None = None,
    caption_decoration: dict[str, Any] | None = None,
    portable_caption_animation: str | dict[str, Any] | None = None,
    caption_theme: CaptionTheme | None = None,
    broll: list[dict[str, Any]] | None = None,
    min_scene_seconds: float | None = None,
) -> dict[str, Any]:
    """用一条帧时间线编译剪映清单、SRT、可选配乐和素材预览。

    所有帧号以项目 fps 为准。shots 按顺序首尾相接，每项 {source,frames,source_start_frame?,
    source_frames?,volume?,transition?,clip?,overlays?,transcript_srt_path?}。省略 source_frames 则按 1 倍速使用对应时长。
    单镜可用 layout.kind 选择构图。调用 list_video_editing_modules(category="layout") 列出构图，传 module_id 查看参数和示例。
    transcript_srt_path 用源素材的绝对时间戳，随裁切和变速自动映射。时间冲突优先使用显式字幕和旁白字幕。
    overlays 每项写 {offset_frame?,frames?,source 或 text,clip?,style?}，用于独立图片或可编辑文字轨。
    broll 在成片时间上叠加静音镜头。每项 {shot_id 或 source, at, duration, source_start?, layout?}，
    at/duration/source_start 均以秒计。默认全屏，layout={kind:"stacked",main:"bottom",split:0.48} 可分屏。
    文字可选 role，生成可调整的样式和独立入场、退场。标注模块查 category="callout"。
    narration/audio 每项写 {source 或 jianying_audio,start_frame,frames?,source_start_frame?,source_frames?,volume?}。
    旁白可加 text 和 subtitle_end_frame 自动生成字幕。额外 captions 接收 {start_frame,end_frame,
    text,highlights?,duck_bgm?}，不得重叠。字幕模块查 category="caption"。
    bgm 接收 {source 或 jianying_audio,volume?,duck_volume?,loop?}，根据旁白和标记字幕压低音量。
    从 list_jianying_audio_assets 取素材，生成 jianying_audio={draft_path,material_id}。混音或 OpenFilm 导出需要本地可用素材。
    preview_frames 选择各镜头基础素材生成一张预览拼图，供检查构图和覆盖。mixdown 输出试听 WAV，清单仍保留独立轨。
    openfilm_project=true 额外生成可直接渲染的本地 OpenFilm 项目，包含混音、字幕与文字页面。
    style_preset 配置字幕与转场，shots[].chapter 记录小节起点。
    min_scene_seconds 设置可见分镜的最短停留，seller_reference 默认为 4 秒。
    可通过 shots[].transition 指定剪映转场，通过 portable_transition={kind,frames,color?} 指定 MP4 对应的 dip/wipe_left/wipe_right。
    caption_decoration 可指定剪映字体、气泡、花字、动画、描边、阴影或底板。
    可移植字幕入场支持 rise/fade/slide_right/wipe_in，也可用 none 关闭。
    可用 openfilm_project_path、fps 与 design.chapter_starts_s 检查渲染及段落起点。
    默认只返回文件路径及计数，include_manifest=true 才展开完整清单。
    """
    return await run_tool(
        ctx,
        ["校验帧时间线和本地素材", "编译字幕、音轨与剪映清单"],
        lambda settings: compile_timeline(
            settings, name, shots, fps=fps, width=width, height=height, narration=narration, audio=audio,
            captions=captions, bgm=bgm, srt_mode=srt_mode, caption_style=caption_style, caption_clip=caption_clip,
            preview_frames=preview_frames, mixdown=mixdown, include_manifest=include_manifest, openfilm_project=openfilm_project,
            style_preset=style_preset, caption_decoration=caption_decoration, portable_caption_animation=portable_caption_animation,
            caption_theme=caption_theme, broll=broll, min_scene_seconds=min_scene_seconds,
        ),
        threaded=True,
    )


TOOLS = (list_video_style_presets, list_video_editing_modules, create_video_graphic, compile_video_timeline)
