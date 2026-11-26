from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import Context

from ..audio_production import create_music_bed as make_bed
from ..config import Settings
from ..editorial_review import animation_review
from ..narration import list_narration_voices as list_voices, synthesize_narration as synthesize
from ..production_graphics import absolute_file, create_output_folder, write_contact_sheet
from ..production_styles import AnimationMotion, StylePreset
from ..video_animation import create_video_animation as create_animation
from .common import run_tool


async def list_narration_voices(
    ctx: Context, backend: Literal["sapi", "edge"] = "sapi", language: str | None = None,
) -> dict[str, Any]:
    """查询本机 SAPI 或显式选择的 Edge 语音。language 可用 zh 或 zh-CN 等。"""
    return await run_tool(ctx, ["查询语音"], lambda _settings: list_voices(backend, language), threaded=True)


async def synthesize_narration(
    segments: list[str], ctx: Context, backend: Literal["sapi", "edge"] = "sapi", voice: str | None = None,
    language: str | None = None, rate: int = 0,
) -> dict[str, Any]:
    """批量将旁白生成为带准确时长的本地 WAV。SAPI 默认离线。显式选择 edge 会把文案发给在线服务。"""
    return await run_tool(ctx, ["合成旁白", "返回 WAV 和准确时长"], lambda settings: synthesize(
        settings, segments, backend=backend, voice=voice, language=language, rate=rate,
    ), threaded=True)


async def create_music_bed(
    duration: float, ctx: Context, preset: Literal["calm", "warm", "pulse"] = "calm", bpm: int = 92, root_note: int = 60,
) -> dict[str, Any]:
    """生成可选的原创配乐 WAV。calm 平缓，warm 温暖，pulse 节拍较明显。duration 为秒。"""
    return await run_tool(ctx, ["合成配乐"], lambda settings: make_bed(settings, duration, preset=preset, bpm=bpm, root_note=root_note), threaded=True)


async def create_video_animation(
    ctx: Context, title: str = "", subtitle: str = "", eyebrow: str = "", image_path: str | None = None,
    image_focus: list[float] | None = None, preset: Literal["editorial", "midnight", "clean"] = "editorial",
    colors: dict[str, str] | None = None, motion: AnimationMotion | None = None,
    value: int | None = None, unit: str = "", width: int = 1080, height: int = 1920,
    duration: float = 3, entrance: float = 0.65, scene_html: str | None = None, assets: dict[str, str] | None = None,
    style_preset: StylePreset | None = None,
) -> dict[str, Any]:
    """创建 OpenFilm 浏览器动画。可传原创 scene_html 做镜头衔接、叠层或动态图形。HTML 提供 window.film.frame(t) 供渲染。style_preset 可选。"""
    def create(settings: Settings) -> dict[str, Any]:
        result = create_animation(
            settings, title=title, subtitle=subtitle, eyebrow=eyebrow, image_path=image_path, image_focus=image_focus,
            preset=preset, colors=colors, motion=motion, value=value, unit=unit, width=width, height=height,
            duration=duration, entrance=entrance, scene_html=scene_html, assets=assets, style_preset=style_preset,
        )
        review = animation_review(result, entrance, value, unit)
        return {**result, "review_guidance": review}

    return await run_tool(ctx, ["创建浏览器动画", "返回 OpenFilm 项目和复查时刻"], create, threaded=True)


async def create_media_contact_sheet(
    images: list[str], ctx: Context, labels: list[str] | None = None,
) -> dict[str, Any]:
    """合并 1–24 张本地关键帧、图卡或截图成一张带标签的预览图。"""
    def create(settings: Settings) -> dict[str, Any]:
        if not 1 <= len(images) <= 24 or (labels is not None and len(labels) != len(images)):
            raise ValueError("images 需要 1–24 张图片，labels 需要与图片数量相同")
        inputs = [(absolute_file(path, "预览图片"), labels[index][:45] if labels is not None else str(index + 1))
                  for index, path in enumerate(images)]
        output = create_output_folder(settings, "contact_sheet") / "preview.jpg"
        write_contact_sheet(inputs, output)
        return {"preview_sheet_path": str(output), "count": len(inputs)}

    return await run_tool(ctx, ["拼接关键帧预览"], create, threaded=True)


TOOLS = (list_narration_voices, synthesize_narration, create_music_bed, create_video_animation, create_media_contact_sheet)
