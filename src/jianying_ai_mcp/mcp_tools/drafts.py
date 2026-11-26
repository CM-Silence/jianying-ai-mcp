from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context

from ..draft_ops import (
    ManifestError,
    analyze_reference_draft as analyze_reference,
    batch_from_template as run_template_batch,
    build_from_reference as run_reference_build,
    build_manifest,
    repair_caption_references as run_caption_repair,
)
from ..presets import PresetCategory, list_presets
from .common import run_tool


async def list_jianying_presets(
    category: PresetCategory,
    ctx: Context,
    keyword: str | None = None,
    is_vip: bool | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    """查询本地草稿库的预设名称。可按名称关键词和 VIP 标记筛选。"""
    return await run_tool(
        ctx,
        ["读取本地预设", "筛选预设名称"],
        lambda _settings: list_presets(category, keyword=keyword, is_vip=is_vip, limit=limit),
    )


async def analyze_reference_draft(
    draft_name: str,
    ctx: Context,
    include_text: bool = False,
    include_edit_details: bool = False,
) -> dict[str, Any]:
    """提取参考草稿蓝图。type_index 可直接用于批量替换的 track_index。

    include_edit_details 增加源时间、倍速、音量、构图、文字样式、入场动画及关键帧。
    时间单位为微秒。include_text 单独控制是否返回字幕正文。
    """
    return await run_tool(
        ctx,
        ["读取参考草稿", "提取轨道与节奏", "保存剪辑蓝图"],
        lambda settings: analyze_reference(
            settings, draft_name, include_text=include_text, include_edit_details=include_edit_details,
        ),
    )


async def build_draft(
    manifest: dict[str, Any] | None = None,
    ctx: Context = None,
    dry_run: bool = True,
    allow_experimental_effects: bool = False,
    manifest_path: str | None = None,
) -> dict[str, Any]:
    """一次提交紧凑编辑清单并生成新草稿。

    manifest 和本地绝对路径 manifest_path 二选一。manifest 结构：
    - draft: {name, width?, height?, fps?}
    - tracks: [{type, name, segments:[...]}]
    - srt_imports?: [{source, track_name?, style?, clip?, mode?, language?, time_offset?}]

    轨道 type 支持 video/audio/text/sticker/effect/filter。时间字段使用 start、duration；
    媒体可加 source_start/source_duration、speed。具体字段见 Skill 的 manifest-schema.md。
    默认 dry_run=true，只在暂存区完整构建和校验；确认后传 false 发布到剪映草稿目录。
    srt_imports[].mode 默认 text，可传 native 生成实验性剪映原生字幕轨。
    srt_imports[].highlights 可设 [{cue,ranges}]，按导入顺序从 1 计数，ranges 为字幕正文中的半开字符区间。
    蒙版、背景填充和音量关键帧需要显式传 allow_experimental_effects=true；显示效果需在目标剪映版本验收。
    """
    return await run_tool(
        ctx,
        ["校验编辑清单", "构建暂存草稿", "校验草稿结构", "发布草稿" if not dry_run else "完成试运行"],
        lambda settings: build_manifest(
            settings,
            _load_manifest(manifest, manifest_path),
            dry_run=dry_run,
            allow_experimental_effects=allow_experimental_effects,
        ),
    )


def _load_manifest(manifest: dict[str, Any] | None, manifest_path: str | None) -> dict[str, Any]:
    if (manifest is None) == (manifest_path is None):
        raise ManifestError("必须且只能指定 manifest 或 manifest_path")
    if manifest is not None:
        return manifest
    path = Path(manifest_path).expanduser()
    if not path.is_absolute() or path.drive.startswith("\\\\") or path.suffix.lower() != ".json":
        raise ManifestError("manifest_path 必须是本地 JSON 文件的绝对路径")
    path = path.resolve()
    if path.drive.startswith("\\\\") or not path.is_file():
        raise ManifestError("manifest_path 没有指向可读取的本地 JSON 文件")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ManifestError("编辑清单 JSON 必须是对象")
    return value


async def build_from_reference(
    reference_draft: str,
    new_draft_name: str,
    ctx: Context,
    replacements: list[dict[str, Any]] | None = None,
    text_replacements: list[dict[str, Any]] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """复制明文或可解密参考草稿的剪辑蓝图，并按轨道与片段下标替换素材或文本。

    replacements 项：{track_type, track_name? 或 track_index?, segment_index, source,
    source_start?, source_duration?}。text_replacements 项：{track_name? 或 track_index?,
    segment_index, text, recalc_style?}。
    """
    return await run_tool(
        ctx,
        ["读取参考草稿", "复制剪辑蓝图", "替换素材与文本", "校验新草稿", "发布新草稿" if not dry_run else "完成试运行"],
        lambda settings: run_reference_build(
            settings,
            reference_draft,
            new_draft_name,
            replacements,
            text_replacements,
            dry_run=dry_run,
        ),
    )


async def repair_caption_references(
    draft_path: str,
    reference_ids: list[str],
    ctx: Context,
    dry_run: bool = True,
) -> dict[str, Any]:
    """修复明文草稿中指定的无效文本素材引用。默认预览，false 将修复后的 JSON 写入 MCP 工作目录。"""
    return await run_tool(
        ctx,
        ["核对文本引用", "输出修复预览" if dry_run else "保存修复后的草稿 JSON"],
        lambda settings: run_caption_repair(settings, draft_path, reference_ids, dry_run=dry_run),
    )


async def batch_from_template(
    template_draft: str,
    jobs: list[dict[str, Any]],
    ctx: Context,
    dry_run: bool = True,
) -> dict[str, Any]:
    """基于一个明文模板批量生成草稿。每个 job 包含 name、replacements、text_replacements。"""
    return await run_tool(
        ctx,
        ["校验批量任务", "逐份复制与替换", "校验批量草稿", "发布批量结果" if not dry_run else "完成批量试运行"],
        lambda settings: run_template_batch(settings, template_draft, jobs, dry_run=dry_run),
    )


TOOLS = (list_jianying_presets, analyze_reference_draft, build_draft, build_from_reference, repair_caption_references, batch_from_template)
