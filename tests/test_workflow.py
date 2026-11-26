from __future__ import annotations

import unittest
from importlib.resources import files
from typing import get_args
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP

from jianying_ai_mcp.mcp_tools.workflow import get_video_workflow
from jianying_ai_mcp.workflow import WorkflowProfile, load_video_workflow


class WorkflowTests(unittest.TestCase):
    def test_general_and_specific_profiles_return_the_bundled_text(self) -> None:
        guides = files("jianying_ai_mcp").joinpath("workflow_guides")
        shared = guides.joinpath("production.md").read_text(encoding="utf-8")
        self.assertTrue(shared.strip())
        self.assertEqual(load_video_workflow(), {"profile": "general", "workflow": shared, "profile_notes": None})

        for profile in get_args(WorkflowProfile)[1:]:
            with self.subTest(profile=profile):
                notes = guides.joinpath(f"{profile}.md").read_text(encoding="utf-8")
                self.assertTrue(notes.strip())
                self.assertEqual(
                    load_video_workflow(profile),
                    {"profile": profile, "workflow": shared, "profile_notes": notes},
                )

    def test_unknown_profile_is_rejected_before_accessing_resources(self) -> None:
        with patch("jianying_ai_mcp.workflow.files") as packaged_files:
            with self.assertRaisesRegex(ValueError, "未知制作流程方向"):
                load_video_workflow("../production")
        packaged_files.assert_not_called()

    def test_brief_requires_no_guide_reads_and_detail_can_be_scoped(self) -> None:
        with patch("jianying_ai_mcp.workflow.files") as packaged_files:
            brief = load_video_workflow("animation", detail="brief")
        packaged_files.assert_not_called()
        full = load_video_workflow("animation", detail="full")
        self.assertLess(len(brief["workflow"]), len(full["workflow"]) // 4)
        self.assertIn("analyze_media_batch", brief["workflow"])
        self.assertIsNone(load_video_workflow("animation", detail="profile")["workflow"])
        self.assertIn("create_motion_clip", full["profile_notes"])
        self.assertIn("plan_video_edit", brief["workflow"])
        self.assertIn("deliver_video_edit", brief["workflow"])
        review = load_video_workflow(section="review")["workflow"]
        self.assertIn("review_export", review)
        self.assertNotIn("## 4.", review)


class WorkflowToolTests(unittest.TestCase):
    def test_mcp_exposes_scoped_guides_with_compact_default(self) -> None:
        mcp = FastMCP("test-workflow")
        mcp.add_tool(get_video_workflow)
        tool = mcp._tool_manager.get_tool("get_video_workflow")
        self.assertIsNotNone(tool)
        schema = tool.parameters["properties"]
        self.assertEqual(set(schema), {"profile", "detail", "section"})
        self.assertEqual(schema["profile"]["enum"], list(get_args(WorkflowProfile)))
        self.assertEqual(schema["profile"]["default"], "general")
        self.assertEqual(schema["detail"]["default"], "brief")
