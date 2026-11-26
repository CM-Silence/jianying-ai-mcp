from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context
from typing_extensions import NotRequired, TypedDict

from ..editorial_review import motion_clip_review
from ..broll_placement import place_broll_shots as place_selected
from ..motion_library import list_motion_recipes as list_recipes
from ..motion_library import render_motion
from ..production_styles import StylePreset
from ..production_graphics import absolute_file
from ..sound_design import list_sfx as list_sounds
from ..storyboard import compile_episode as compile_spec
from ..storyboard import read_episode_file
from ..storyboard import select_take as pick_take
from ..storyboard import transcript_pool as load_pool
from ..workflow import making_guide
from .common import run_tool


async def list_motion_recipes(ctx: Context) -> dict[str, Any]:
    """列出可用的 HTML 动效配方及其数据字段和默认时长。"""
    return await run_tool(ctx, ["读取动效配方"], lambda _settings: {
        **list_recipes(), "production_guidance": making_guide("motion"),
    })


async def create_motion_clip(
    recipe: str,
    data: dict[str, Any],
    ctx: Context,
    duration: float = 0,
    fps: int = 30,
    width: int = 1080,
    height: int = 1920,
    style_preset: StylePreset | None = None,
    render: bool = True,
) -> dict[str, Any]:
    """用内置动效配方生成一段可入轨的 MP4。duration 省略时使用配方默认时长。

    配方数据见 list_motion_recipes。render=false 只生成可预览的 HTML 工程，不调用渲染。
    """
    def create(settings):
        result = render_motion(
            settings, recipe, data, duration=duration, fps=fps, width=width, height=height,
            style_preset=style_preset, render=render,
        )
        return {**result, "review_guidance": motion_clip_review(result, data)}

    return await run_tool(ctx, ["校验动效参数", "生成 HTML 场景", "返回动效素材与复查时点"], create, threaded=True)


async def list_sound_cues(ctx: Context) -> dict[str, Any]:
    """列出本地合成的音效及建议使用场景。"""
    return await run_tool(ctx, ["读取音效目录"], lambda _settings: list_sounds())


async def list_episode_takes(ctx: Context, batch_id: str | None = None) -> dict[str, Any]:
    """列出已缓存转写的素材及其对白条数。传 analyze_media_batch 返回的 batch_id 可限定本次项目。"""
    return await run_tool(
        ctx, ["读取转写缓存"],
        lambda settings: {"sources": [
            {"source": item["source"], "duration_ms": item["duration_ms"], "cue_count": len(item["cues"])}
            for item in load_pool(settings, batch_id)
        ]},
        threaded=True,
    )


async def select_episode_take(line: str, ctx: Context, avoid: list[str] | None = None, min_score: float = 0.62,
                              batch_id: str | None = None) -> dict[str, Any]:
    """按台词选出 source 与起止秒。传 batch_id 时只使用该批次的转写。"""
    return await run_tool(
        ctx, ["比对台词与转写"],
        lambda settings: pick_take(settings, line, avoid=set(avoid or ()), min_score=min_score, batch_id=batch_id),
        threaded=True,
    )


def _episode_input(spec, spec_path, media_root):
    if (spec is None) == (spec_path is None) or (spec is not None and media_root is not None):
        raise ValueError("请提供 spec 或 spec_path 其中之一。media_root 与 spec_path 搭配使用。")
    return spec if spec is not None else read_episode_file(absolute_file(spec_path, "分镜 JSON"), media_root)


class BrollPlacement(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    shot_id: str
    at: float
    duration: float
    source_start: NotRequired[float]
    layout: NotRequired[dict[str, Any]]


async def place_broll_shots(
    placements: list[BrollPlacement], ctx: Context, spec: dict[str, Any] | None = None,
    spec_path: str | None = None, media_root: str | None = None, dry_run: bool = True,
) -> dict[str, Any]:
    """将 get_broll_shot 取得的镜头在后台写入一个新剪映草稿。保留分镜原声与原有包装。

    spec 填 {name,beats,...} 或传 spec_path。placements 每项需 shot_id、成片起点 at、duration（秒），
    可指定原片起点 source_start。默认全屏，layout={kind:"stacked",main:"bottom",split:0.48} 为上素材下人物。
    默认 seller_reference 样式。dry_run=true 试编译，false 发布新草稿并从文件回读实际轨道、时码、素材和音量。
    使用新片名，已有草稿不会被覆盖。仅可在剪映预览的候选返回 not_timeline_ready。
    """
    return await run_tool(ctx, ["核对选段与分镜", "编排静音补充画面", "返回草稿校验结果"],
                          lambda settings: place_selected(settings, _episode_input(spec, spec_path, media_root),
                                                          placements, dry_run=dry_run), threaded=True)


async def compile_episode(
    spec: dict[str, Any] | None = None,
    ctx: Context = None,
    preview_frames: int = 0,
    mixdown: bool = False,
    include_manifest: bool = False,
    openfilm_project: bool = False,
    spec_path: str | None = None,
    media_root: str | None = None,
) -> dict[str, Any]:
    """把分镜节拍编译成剪映清单、字幕、音效与配乐。

    卖家口播使用 style_preset="seller_reference"。按原话落字可设 beat.caption.cues=[{at,text},...]，
    at 为相对本拍开始的秒数。beat.circle_mask={at,duration,center,diameter} 可在转折处短暂聚焦。

    beat 为 {kind:"talk",source,start,end,text?}（口播）、
    {kind:"media",source,start/end 或 seconds}（B-roll、录屏、生成视频，默认静音）或
    {kind:"graphic",recipe,data,seconds}（全屏动效卡），可选 chapter、transition、sfx、role。
    talk/media 可加 layout={kind:"stacked"|"interview_box", main:"bottom"|"top", split?,
    with:{graphic:{recipe,data}} | {media:{source,start?,focus?,fit?}}} 分屏，动效卡按栏位尺寸渲染。
    spec.min_scene_seconds 设置可见分镜最短停留（seller_reference 默认 4 秒）。
    spec.broll 可用 {shot_id 或 source,at,duration,source_start?,layout?} 在成片指定秒数叠加静音镜头，
    保留原声。layout 默认全屏，可用 {kind:"stacked",main:"bottom",split:0.48} 让人物与素材分屏。
    talk beat 的 chapter 是剪点标记。小节切换由口播或镜头交代，图卡标题用于内容。
    全屏复述卡（list/steps/checklist/compare）只能在 role="summary" 之后。总结段可用
    voice={source,start,end,text?} 播放主讲原声，不必切回实拍。
    字幕：caption_mode（single/stack/blocks/words）、caption_size、caption_zone（auto/top/upper/lower/bottom）、
    caption_keywords；按拍用 caption={mode,size,zone,y,keywords} 覆盖。重点词自动标黄。
    也可传本地分镜 JSON 的 spec_path。相对素材路径以文件目录或显式 media_root 为准。
    自动字幕落位会按实拍或裁切后的人物栏位避让检测到的脸；caption.avoid 可标明需要额外保留的
    成片区域（归一化矩形数组，每项 [左,上,右,下]）。新视频先用 plan_video_script 编排。
    spec 可设 caption_punctuation（spaced/keep/strip）、caption_max_chars、transitions（auto/all/none）、
    music、source_proxy。
    """
    def compile_input(settings):
        value = _episode_input(spec, spec_path, media_root)
        return compile_spec(settings, value, preview_frames=preview_frames, mixdown=mixdown,
                            include_manifest=include_manifest, openfilm_project=openfilm_project,
                            include_review_guidance=True)
    return await run_tool(
        ctx,
        ["校验分镜", "渲染动效并排帧", "编译清单、字幕与音效"],
        compile_input,
        threaded=True,
    )


TOOLS = (
    list_motion_recipes, create_motion_clip, list_sound_cues,
    list_episode_takes, select_episode_take, compile_episode, place_broll_shots,
)
