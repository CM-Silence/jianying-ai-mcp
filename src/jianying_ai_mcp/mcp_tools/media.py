from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import Context

from ..asset_batch import analyze_media_batch as analyze_batch, get_media_batch_context as batch_context
from ..asset_batch import export_media_batch_transcript as export_transcript
from ..broll_library import (Availability, SourcePool, bind_stock_selection, get_broll_shot as get_shot, index_broll_shots as index_shots,
                             search_broll_shots as search_shots)
from ..jianying_stock import list_stock_videos
from ..audio_production import master_video_audio as make_audio_master
from ..audio_production import verify_audio_dialogue as verify_dialogue
from ..export_review import review_export as inspect_export
from ..media_info import parse_media_info as probe_media_file
from ..media_prepare import prepare_media_assets as prepare_assets
from ..video_observer import analyze_video, get_analysis_context, save_observations
from .common import run_tool


async def parse_media_info(media_path: str, ctx: Context) -> dict[str, Any]:
    """用 ffprobe 查询本地视频、音频或图片的格式、时长、尺寸与流信息。"""
    return await run_tool(
        ctx,
        ["检查本地素材", "读取媒体参数"],
        lambda settings: probe_media_file(settings, media_path),
    )


async def analyze_video_source(
    source: str,
    ctx: Context,
    detail: str = "efficient",
    start: str | float | int | None = None,
    end: str | float | int | None = None,
    max_frames: int | None = None,
    resolution: int = 512,
    subtitle_path: str | None = None,
    transcription: str = "auto",
    transcript_limit: int = 120,
    prefer_subtitles: bool = True,
) -> dict[str, Any]:
    """提取音频或视频的带时间戳对白和代表帧。detail 可选 transcript/efficient/balanced/dense，start/end 为秒。transcription 可选 auto/local/none/groq/openai，local 保持音频在本机。"""
    return await run_tool(
        ctx,
        ["检查视频与依赖", "提取字幕和代表帧", "生成视频观察报告"],
        lambda settings: analyze_video(
            settings,
            source,
            detail=detail,
            start=start,
            end=end,
            max_frames=max_frames,
            resolution=resolution,
            subtitle_path=subtitle_path,
            transcription=transcription,
            transcript_limit=transcript_limit,
            prefer_subtitles=prefer_subtitles,
        ),
        threaded=True,
    )


async def save_video_observations(
    analysis_id: str,
    observations: list[dict[str, Any]],
    ctx: Context,
    edit_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """保存 Agent 对代表帧的结构化理解，供后续剪辑规划复用。

    observations 每项为 {time_ms, description, tags?, confidence?}；edit_candidates 每项为
    {start_ms, end_ms, reason, confidence?}。只保存模型已经实际读取过的画面结论。
    """
    return await run_tool(
        ctx,
        ["校验画面观察", "保存语义与候选片段"],
        lambda settings: save_observations(settings, analysis_id, observations, edit_candidates),
    )


async def get_video_analysis_context(
    analysis_id: str,
    ctx: Context,
    start_ms: int | None = None,
    end_ms: int | None = None,
    offset: int = 0,
    limit: int = 120,
) -> dict[str, Any]:
    """按时间范围和分页读取已缓存的视频字幕、代表帧及观察路径。

    长视频不要一次加载完整字幕；从 offset=0 开始，根据 next_offset 继续。指定
    start_ms/end_ms 可只取得候选区间，避免重复抽帧和浪费上下文。
    """
    return await run_tool(
        ctx,
        ["读取缓存视频报告", "筛选时间范围与字幕页"],
        lambda settings: get_analysis_context(
            settings,
            analysis_id,
            start_ms=start_ms,
            end_ms=end_ms,
            offset=offset,
            limit=limit,
        ),
    )


async def list_jianying_stock_videos(ctx: Context, offset: int = 0, limit: int = 50) -> dict[str, Any]:
    """列出剪映共享素材缓存及下载目录里的可读视频。needs_editor_preview 需在官方素材面板核对。"""
    return await run_tool(ctx, ["核对剪映素材目录", "筛选可剪辑的视频"],
                          lambda s: list_stock_videos(s, offset=offset, limit=limit), threaded=True)


async def index_broll_shots(
    ctx: Context, shots: list[dict[str, Any]] | None = None, analysis_id: str | None = None,
    source_pool: SourcePool = "jianying",
) -> dict[str, Any]:
    """把看过画面的剪映库存视频选段存入跨项目素材库。先 list_jianying_stock_videos 取源文件。

    shots 每项需要 source（本地绝对路径）、start_ms、end_ms、description。
    可加 tags、use_case、asset_title、asset_id、source_url、license、attribution。
    对 needs_editor_preview 中已在剪映预览的官方视频，可填 editor_verified_ms（已确认的可用时长）。
    自摄素材需要显式指定 source_pool="personal"。参考草稿只供分析设计。
    也可传 analysis_id，导入 save_video_observations 保存的带区间候选与画面描述。
    """
    return await run_tool(ctx, ["核对源视频与区间", "保存可检索的镜头"],
                          lambda s: index_shots(s, shots, analysis_id=analysis_id, source_pool=source_pool), threaded=True)


async def bind_jianying_stock_selection(ctx: Context, selection: str, shot_ids: list[str]) -> dict[str, Any]:
    """从仅含官方视频的专用采集时间线绑定已入库镜头。保留剪映资源身份，绑定后可跨项目自动入轨。"""
    return await run_tool(ctx, ["核对官方素材来源", "绑定原生镜头记录"],
                          lambda s: bind_stock_selection(s, selection, shot_ids), threaded=True)


async def search_broll_shots(
    query: str, ctx: Context, min_duration_ms: int = 0, limit: int = 10,
    source_pool: SourcePool = "jianying",
    availability: Availability = "any",
) -> dict[str, Any]:
    """按画面描述检索已入库的剪映素材。自有库需显式指定 personal。时长筛选以毫秒计。

    自动入轨用 availability="timeline_ready"，取 shot_id 传给 plan_video_edit 或 compile_episode 的 broll。
    native_draft 保留剪映素材身份，使用剪映预览和导出。local_video 也可用 OpenFilm 渲染。
    editor_only 返回已在剪映核对的库存选段、源时间和出处，用于在官方面板选镜。
    """
    return await run_tool(ctx, ["检索素材描述和标签", "核对可用源区间"],
                          lambda s: search_shots(s, query, min_duration_ms=min_duration_ms, limit=limit,
                                                 source_pool=source_pool, availability=availability),
                          threaded=True)


async def get_broll_shot(shot_id: str, ctx: Context) -> dict[str, Any]:
    """获取指定库存选段，复核源文件、可用时间和素材身份。输入来自 search_broll_shots。

    local_video 返回实际可读文件；native_draft 返回供剪映入轨的原生引用。editor_only 返回
    timeline_ready=false、media=null。此工具读取已索引资源，不能从剪映云端下载新的镜头。
    """
    return await run_tool(ctx, ["复核选定镜头", "返回媒体与源片时间范围"],
                          lambda settings: get_shot(settings, shot_id), threaded=True)


async def review_export(
    media_path: str,
    ctx: Context,
    start: float | None = None,
    end: float | None = None,
    max_frames: int = 12,
    target_loudness: float | None = None,
) -> dict[str, Any]:
    """抽检本地成片，返回带时间戳的帧、全区间疑似纯绿帧、黑场和音频响度。时间以秒计。"""
    return await run_tool(
        ctx,
        ["检查本地成片", "返回抽帧、黑场和响度"],
        lambda settings: inspect_export(
            settings, media_path, start=start, end=end, max_frames=max_frames, target_loudness=target_loudness,
        ),
        threaded=True,
    )


async def master_video_audio(
    media_path: str, ctx: Context, target_loudness: float = -16.0, true_peak: float = -1.5,
    loudness_range: float = 11.0,
) -> dict[str, Any]:
    """两遍 EBU R128 校正本地成片的首条音轨。输出新的 MP4，视频流直接复制，返回复编码后的实测响度和真峰。"""
    return await run_tool(
        ctx,
        ["测量成片源音轨", "输出母版并复测实际响度"],
        lambda settings: make_audio_master(
            settings, media_path, target_loudness=target_loudness, true_peak=true_peak, loudness_range=loudness_range,
        ),
        threaded=True,
    )


async def analyze_media_batch(
    sources: list[str], ctx: Context, recursive: bool = False,
    transcription: Literal["local", "none", "groq", "openai"] = "local",
    max_preview_frames: int = 12, transcript_chars: int = 3500,
) -> dict[str, Any]:
    """盘点本地素材，返回对白摘录、预览和 transcript_path。该 UTF-8 文本包含整批逐条带原片时码的对白。"""
    return await run_tool(ctx, ["盘点素材并提取对白", "返回主题线索与预览"], lambda settings: analyze_batch(
        settings, sources, recursive=recursive, transcription=transcription,
        max_preview_frames=max_preview_frames, transcript_chars=transcript_chars,
    ), threaded=True)


async def get_media_batch_context(
    batch_id: str, ctx: Context, query: str | None = None, source_id: str | None = None,
    start_ms: int | None = None, end_ms: int | None = None, offset: int = 0, limit: int = 20,
) -> dict[str, Any]:
    """按关键词、素材编号或毫秒时间范围读取批次对白。未指定筛选条件时分页读取素材目录。"""
    return await run_tool(ctx, ["读取批次证据"], lambda settings: batch_context(
        settings, batch_id, query=query, source_id=source_id, start_ms=start_ms, end_ms=end_ms, offset=offset, limit=limit,
    ), threaded=True)


async def export_media_batch_transcript(batch_id: str, ctx: Context) -> dict[str, Any]:
    """将已有批次的完整对白按素材顺序导出成带原片时码的 UTF-8 文本，复用缓存并列出缺失项。"""
    return await run_tool(ctx, ["整理整批原片对白"], lambda settings: export_transcript(settings, batch_id), threaded=True)


async def prepare_media_assets(
    sources: list[str], ctx: Context, recursive: bool = False, normalize: bool = True, extract_audio: bool = False,
    width: int | None = None, height: int | None = None, fps: int | None = None,
    fit: Literal["contain", "cover"] = "contain", audio_rate: Literal[16000, 44100, 48000] = 16000,
) -> dict[str, Any]:
    """批量生成剪映易读的本地媒体副本。烘焙视频旋转和画幅，可单独提取 WAV 或设置宽高与帧率。"""
    return await run_tool(ctx, ["处理待剪素材", "返回可编辑副本"], lambda settings: prepare_assets(
        settings, sources, recursive=recursive, normalize=normalize, extract_audio=extract_audio,
        width=width, height=height, fps=fps, fit=fit, audio_rate=audio_rate,
    ), threaded=True)


async def verify_audio_dialogue(
    media_path: str, expected: list[dict[str, Any]], ctx: Context,
    transcription: Literal["local", "groq", "openai"] = "local",
) -> dict[str, Any]:
    """对成片或混音重新转写。expected 各项为 {text,start_ms?,end_ms?}，近似文字和同音另标为人工复核。时间为转写语句范围。"""
    return await run_tool(ctx, ["转写实际混音", "核对关键句"], lambda settings: verify_dialogue(
        settings, media_path, expected, transcription=transcription,
    ), threaded=True)


TOOLS = (
    parse_media_info, analyze_media_batch, get_media_batch_context, export_media_batch_transcript, analyze_video_source, save_video_observations,
    list_jianying_stock_videos, index_broll_shots, bind_jianying_stock_selection, search_broll_shots, get_broll_shot,
    get_video_analysis_context, prepare_media_assets, review_export, master_video_audio, verify_audio_dialogue,
)
