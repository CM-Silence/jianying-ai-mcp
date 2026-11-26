from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context

from ..workflow import WorkflowDetail, WorkflowProfile, WorkflowSection, load_video_workflow
from .common import run_tool
from ..delivery import deliver_edit
from ..edit_recipe import EditClip, Music, plan_edit
from ..production_styles import StylePreset
from ..video_script import execute_script, plan_script


async def plan_video_script(brief: str, spec: dict[str, Any], ctx: Context, media_root: str | None = None) -> dict[str, Any]:
    """收到制作需求后先写视听脚本。保存用户可读的带时码表和可执行分镜，并主动搜索剪映库存候选。

    brief 写受众和本期观点。spec={name,beats:[...]}。每拍写 intent（画面目的）、text 或原声选段、
    source,start,end。实拍承载主讲；列举观点可用 layout.with.graphic={recipe,data}；
    丰富场景时在 talk 拍写 stock={query:"可见主体和动作",layout?:{kind:"stacked",main:"bottom",split:0.48}}。
    默认全屏库存。常规一镜 5–8 秒。自然分句、列举、递进才组合字幕，短项横排、过宽纵排，整项淡入。
    给出确切入点时可写 caption={mode:"blocks",cues:[{at:0,text:"先核对货号"},{at:2,text:"再清点数量"}]}。
    只规划、搜素材，无需在本步渲染。返回 script_path 和每镜真实候选，供 Agent 选择后执行。
    """
    return await run_tool(ctx, ["编排视听脚本", "按分镜检索剪映库存", "保存脚本和候选"],
                          lambda s: plan_script(s, brief, spec, media_root), threaded=True)


async def execute_video_script(script_path: str, ctx: Context, selections: dict[str, str] | None = None,
                               media_root: str | None = None, dry_run: bool = False,
                               openfilm_project: bool = False) -> dict[str, Any]:
    """按已保存脚本剪辑并回读新草稿。selections 用场景编号字符串映射到所选官方 shot_id。

    Agent 按画面描述或抽帧选镜，工具逐条获取、核对并调用入轨逻辑。没有合适镜头且决定延续主讲时，
    该镜填 keep_camera 并向用户说明。还未选镜时返回待处理的场景，不会发布半成品。
    media_root 可绑定本期片源目录。dry_run=true 只试编译。正式发布使用新片名，保留源草稿。
    需要后台 MP4 时传 openfilm_project=true，按返回的 render_call 渲染同一时间线。剪映专用原生引用需由剪映渲染。
    """
    return await run_tool(ctx, ["复核脚本选材", "按脚本编译构图和字幕", "回读剪映轨道"],
                          lambda s: execute_script(s, script_path, selections or {}, media_root, dry_run,
                                                   openfilm_project), threaded=True)


async def get_video_workflow(
    ctx: Context, profile: WorkflowProfile = "general", detail: WorkflowDetail = "brief", section: WorkflowSection | None = None,
) -> dict[str, Any]:
    """返回剪辑工作流。分镜制作查 section="edit"，其他环节可按 section 或 detail 查询。"""
    return await run_tool(
        ctx,
        ["读取素材与编辑说明"],
        lambda _settings: load_video_workflow(profile, detail=detail, section=section),
    )


async def plan_video_edit(
    name: str, clips: list[EditClip], ctx: Context, media_root: str | None = None,
    style_preset: StylePreset = "editorial", music: Music | None = None,
    width: int = 1080, height: int = 1920, fps: int = 30,
    broll: list[dict[str, Any]] | None = None,
    min_scene_seconds: float | None = None,
) -> dict[str, Any]:
    """按给定镜头顺序锁定选段、字幕、风格与素材校验值，返回可迁移的 recipe_path。

    最少只需片名与 clips=[{"source":"素材绝对路径"}]。原声音轨默认本地取字幕，
    caption 填定稿文字，空字符串跳过。chapter 仅作编排标记。重点词用 highlights，可用 caption_sfx 指定剪映音效。
    broll 每项用 shot_id 或 source 选镜头，at、duration 为成片秒数。默认全屏覆盖，
    layout={kind:"stacked",main:"bottom",split:0.48} 表示上方素材、下方原镜。两种方式均保留主镜原声。
    min_scene_seconds 设置可见分镜的最短停留，seller_reference 默认为 4 秒。
    """
    def plan(settings):
        result = plan_edit(settings, name, clips, media_root=media_root, style_preset=style_preset,
                           music=music, width=width, height=height, fps=fps, broll=broll, min_scene_seconds=min_scene_seconds)
        guides = []
        if len(result["cuts"]) > 1:
            guides.append({"tool": "get_video_workflow", "arguments": {"section": "edit"}})
        if result["caption_count"]:
            guides.append({"tool": "list_video_editing_modules", "arguments": {"category": "caption"}})
        if any(cut.get("panel") for cut in result["cuts"]):
            guides.append({"tool": "list_motion_recipes", "arguments": {}})
        if guides:
            result["optional_guide_tools"] = guides
        return result

    return await run_tool(ctx, ["读取素材与字幕", "保存剪辑配方"], plan, threaded=True)


async def deliver_video_edit(
    recipe_path: str, ctx: Context, media_root: str | None = None, replacements: dict[str, str] | None = None,
    reference_review_path: str | None = None,
) -> dict[str, Any]:
    """从配方生成剪映首页可打开的草稿，尝试剪映原生导出，成功后验收 MP4。

    在另一台机器传 media_root，保留素材相对目录；单独搬移的素材可用 replacements={素材ID:绝对路径}。
    复刻时可传上一版的 reference_review_path，核对解码画面、音频和 SRT 是否完全相同。
    若 status=manual_export_required，请将 draft_name、draft_path、失败原因和 user_message 告知用户并结束导出。
    """
    return await run_tool(ctx, ["核对配方与素材", "生成草稿并尝试剪映原生导出"], lambda s: deliver_edit(
        s, recipe_path, media_root=media_root, replacements=replacements, reference_review_path=reference_review_path,
    ), threaded=True)


TOOLS = (get_video_workflow, plan_video_script, execute_video_script, plan_video_edit, deliver_video_edit)
