from __future__ import annotations

from importlib.resources import files
from typing import Literal, get_args


WorkflowProfile = Literal["general", "dialogue", "tutorial", "promo", "reference", "template", "animation"]
WorkflowDetail = Literal["brief", "profile", "full"]
WorkflowSection = Literal["plan", "sources", "edit", "finish", "review"]


def making_guide(topic: Literal["storyboard", "captions", "motion"]) -> str:
    return files("jianying_ai_mcp").joinpath("workflow_guides", f"making-{topic}.md").read_text(encoding="utf-8").strip()

_ROUTES = {
    "dialogue": "可查原声和重复口播，并从实际成片复核关键句。",
    "tutorial": "可局部抽帧查看按钮、代码、步骤和画面结果。",
    "promo": "可组合实拍、图卡、旁白、配乐和自定义动效。",
    "reference": "用 detail=profile 查看三个卖家口播草稿的带时码拆解、字体动画和分镜停留方式。",
    "template": "build_from_reference 和 batch_from_template 支持复用草稿及换料。",
    "animation": "list_motion_recipes 提供可填写的图卡配方，create_motion_clip 生成可直接入轨的素材。",
}

_BRIEF = (
    "先据实拍原话把需求写成带时码的视听脚本，用 plan_video_script 保存。每镜写原话、意图和来源：实拍讲主体，HTML 列举观点，"
    "需要场景画面就写 stock.query 主动检索。Agent 选镜后用 execute_video_script 按脚本生成并回读新草稿。"
    "已有定剪选段用 plan_video_edit(name, clips) 返回 recipe_path。"
    "deliver_video_edit(recipe_path) 保存剪映草稿并尝试原生导出。遇到 manual_export_required，向用户转达草稿名、路径和原因。复刻时可传 media_root。"
    "用 analyze_media_batch 查原话，search_broll_shots 查画面。卖家口播用 seller_reference。动效、全屏补画面和分屏查 section=edit。"
)


def load_video_workflow(
    profile: WorkflowProfile = "general", *, detail: WorkflowDetail = "full", section: WorkflowSection | None = None,
) -> dict[str, str | None]:
    if profile not in get_args(WorkflowProfile):
        raise ValueError(f"未知制作流程方向 {profile!r}。请选择已列出的方向。")

    if detail not in get_args(WorkflowDetail) or (section is not None and section not in get_args(WorkflowSection)):
        raise ValueError("未知工作流 detail 或 section")
    if section is None and (detail == "brief" or (detail == "profile" and profile == "general")):
        return {"profile": profile, "workflow": _BRIEF, "profile_notes": _ROUTES.get(profile)}

    guides = files("jianying_ai_mcp").joinpath("workflow_guides")
    if section is not None:
        shared = guides.joinpath("production.md").read_text(encoding="utf-8")
        marker = f"<!-- section: {section} -->"
        if shared.count(marker) != 1:
            raise ValueError(f"工作流指南缺少唯一章节标记 {section}")
        content = shared.partition(marker)[2].partition("<!-- section:")[0].strip()
        result = {"profile": profile, "workflow": content, "profile_notes": None}
        if section == "edit":
            result["production_guidance"] = making_guide("storyboard")
        return result
    if detail == "profile" and profile != "general":
        return {"profile": profile, "workflow": None, "profile_notes": guides.joinpath(f"{profile}.md").read_text(encoding="utf-8")}
    return {
        "profile": profile,
        "workflow": guides.joinpath("production.md").read_text(encoding="utf-8"),
        "profile_notes": (
            guides.joinpath(f"{profile}.md").read_text(encoding="utf-8") if profile != "general" else None
        ),
    }
