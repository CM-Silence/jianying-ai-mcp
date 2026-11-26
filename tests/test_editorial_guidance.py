from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image

from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.editorial_review import motion_clip_review, recipe_check
from jianying_ai_mcp.mcp_tools.workflow import plan_video_edit
from jianying_ai_mcp.motion_library import scene_html
from jianying_ai_mcp.storyboard import compile_episode


class GuidanceRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_plans_suggest_only_guides_for_their_cuts(self):
        cases = [
            ([{}], 0, []),
            ([{}, {}], 0, ["get_video_workflow"]),
            ([{"panel": {"recipe": "steps"}}], 1, ["list_video_editing_modules", "list_motion_recipes"]),
        ]
        for cuts, count, expected in cases:
            with self.subTest(expected=expected), patch("jianying_ai_mcp.mcp_tools.workflow.plan_edit", return_value={
                "cuts": cuts, "caption_count": count, "next_tool": "deliver_video_edit",
            }):
                result = await plan_video_edit("定剪", [], ctx=AsyncMock())
                self.assertEqual([g["tool"] for g in result.get("optional_guide_tools", [])], expected)
                self.assertNotIn("production_guidance", result)
                self.assertEqual(result["next_tool"], "deliver_video_edit")

    async def test_discovery_and_public_results(self):
        # Use the interpreter's installed package, including when testing an isolated wheel.
        with tempfile.TemporaryDirectory() as work:
            server = StdioServerParameters(command=sys.executable, args=["-B", "-m", "jianying_ai_mcp"], cwd=work,
                                          env={"PYTHONPATH": os.pathsep.join(sys.path), "JIANYING_MCP_WORK_ROOT": work})
            async with stdio_client(server) as streams, ClientSession(*streams) as client:
                initialized = await client.initialize()
                tools = await client.list_tools()
                discovery = initialized.model_dump_json() + tools.model_dump_json()
                titles = ["分镜制作指引", "字幕动画制作指引", "HTML 动效制作指引"]
                for title in titles:
                    self.assertNotIn(title, discovery)

                async def call(name, args, *, success=True):
                    response = await client.call_tool(name, args)
                    self.assertFalse(response.isError)
                    payload = response.structuredContent
                    self.assertEqual(payload, json.loads(response.content[0].text))
                    self.assertEqual(payload["success"], success, payload.get("error"))
                    return payload

                cases = [
                    ("get_video_workflow", {"section": "edit"}, 0),
                    ("list_video_editing_modules", {"category": "caption"}, 1),
                    ("list_video_editing_modules", {"module_id": "highlights"}, 1),
                    ("list_motion_recipes", {}, 2),
                    ("get_video_workflow", {}, None),
                    ("get_video_workflow", {"detail": "full"}, None),
                    ("get_video_workflow", {"section": "review"}, None),
                    ("list_video_editing_modules", {}, None),
                    ("list_video_editing_modules", {"module_id": "stacked"}, None),
                    ("list_video_editing_modules", {"category": "callout"}, None),
                    ("list_sound_cues", {}, None),
                ]
                for tool, args, selected in cases:
                    with self.subTest(tool=tool, args=args):
                        payload = await call(tool, args)
                        guide = payload.get("production_guidance", "")
                        self.assertEqual([t for t in titles if t in guide], [] if selected is None else [titles[selected]])
                        self.assertEqual("production_guidance" in payload, selected is not None)
                error = await call("list_video_editing_modules", {"category": "caption", "module_id": "full"}, success=False)
                self.assertNotIn("production_guidance", error)

                project = await call("create_motion_clip", {
                    "recipe": "steps", "data": {"items": ["保存草稿", {"title": "生成预览", "at": 1.4}]}, "render": False,
                })
                self.assertNotIn("mp4_path", project)
                self.assertEqual(project["review_guidance"]["item_starts_s"], [0.1, 1.4])
                self.assertEqual(project["review_guidance"]["next_call"], {
                    "tool": "look_openfilm", "arguments": {"project_path": project["project_path"], "start": 1.6},
                })
                self.assertIn("待预览", project["review_guidance"]["status"])
                self.assertNotIn("production_guidance", project)
                stock = await call("create_video_animation", {"title": "章节", "style_preset": "broadcast", "entrance": 1.2})
                self.assertEqual(stock["review_guidance"]["effect"], "slide_right")
                self.assertIn(1.2, stock["review_guidance"]["sample_times_s"])
                self.assertEqual(stock["review_times_s"], [0, 0.6, 2.1])
                custom = await call("create_video_animation", {"scene_html": "<script>window.film={frame(t){}}</script>", "entrance": 1.2})
                self.assertEqual(custom["review_guidance"]["effect"], "custom_html")
                self.assertNotIn(1.2, custom["review_times_s"])
                self.assertEqual(custom["review_guidance"]["next_call"]["arguments"]["start"], 0.75)

                picture = Path(work) / "still.png"
                Image.new("RGB", (320, 240)).save(picture)
                spec = {"name": "单镜", "beats": [
                    {"kind": "media", "source": str(picture), "seconds": 1.0, "captions": False, "sfx": False},
                ]}
                built = await call("compile_episode", {"spec": spec})
                self.assertEqual(built["review_guidance"]["storyboard"]["end_s"], 1.0)
                self.assertNotIn("captions", built["review_guidance"])
                self.assertNotIn("graphics", built["review_guidance"])


class EditorialTimeTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.picture = root / "still.png"
        Image.new("RGB", (320, 240)).save(self.picture)
        self.settings = Settings(root / "drafts", root / "work", None)

    def compile(self, beats):
        media = {"kind": "media", "source": str(self.picture), "seconds": 2, "sfx": False}
        return compile_episode(self.settings, {
            "name": "审片", "fps": 25, "width": 480, "height": 800, "caption_zone": "bottom",
            "beats": [{**media, **beat} for beat in beats],
        }, include_review_guidance=True)["review_guidance"]

    def test_cuts_handoffs_and_per_beat_mode_overrides(self):
        line = "先保存草稿。再生成预览。最后看结果。"
        guide = self.compile([
            {"seconds": 0.99}, {"seconds": 3, "caption": {"mode": "stack"}, "text": line},
            {"seconds": 3, "caption": {"mode": "words"}, "text": line},
        ])
        self.assertEqual([item["at_s"] for item in guide["storyboard"]["cuts"]], [1.0, 4.0])
        stack, words = guide["captions"]
        self.assertEqual([stack["mode"], words["mode"]], ["stack", "words"])
        first, last = stack["handoffs"]
        self.assertEqual((first["beat"], first["old"], first["new"]), (2, "先保存草稿", "再生成预览"))
        self.assertLess(first["at_s"], last["at_s"])
        self.assertAlmostEqual(first["start_s"], first["at_s"] - 0.12)
        self.assertAlmostEqual(first["end_s"], first["at_s"] + 0.30)
        self.assertIn("估时", first["timing"])
        self.assertNotIn("handoffs", words)

        singleton = self.compile([{"caption": {"mode": "stack"}, "text": "只读一句。"}])
        self.assertIn("只有一句", singleton["captions"][0]["focus"])
        self.assertNotIn("handoffs", singleton["captions"][0])
        plain = self.compile([{"text": "一次说完。"}, {"caption": {"mode": "blocks"}, "text": line}])
        self.assertEqual([item["mode"] for item in plain["captions"]], ["single", "blocks"])
        self.assertNotIn("堆叠", json.dumps(plain, ensure_ascii=False))

    def test_panel_cues_are_in_program_time_and_share_the_render_schedule(self):
        data = {"items": ["锁定镜头", {"title": "检查成片", "at": 2.4}, "确认结果"]}
        self.assertIn("const starts=[0.1, 2.4, 2.95]", scene_html("steps", data))
        with patch("jianying_ai_mcp.storyboard.render_motion", return_value={"mp4_path": str(self.picture)}):
            guide = self.compile([{"seconds": 5}, {"seconds": 4, "layout": {
                "kind": "stacked", "main": "top", "with": {"graphic": {"recipe": "steps", "data": data}},
            }}])
        panel = guide["graphics"][0]
        self.assertEqual(panel["placement"], "panel")
        self.assertEqual(panel["item_starts_s"], [5.1, 7.4, 7.95])
        self.assertEqual(panel["sample_times_s"][-1], 8.96)
        self.assertNotIn("timing_note", panel)

    def test_short_clips_have_only_reachable_sample_times(self):
        late = recipe_check("checklist", {"items": ["第一项", {"title": "第二项", "at": 7}]}, 0.9, 1)
        self.assertEqual(late["sample_times_s"], [0])
        self.assertIn("timing_note", late)
        result = {"recipe": "number", "duration": 1.5, "fps": 24, "mp4_path": "cached.mp4"}
        review = motion_clip_review(result, {"value": -12, "unit": "秒"})
        self.assertEqual(review["target"], "-12秒")
        self.assertIn("已渲染，待目检", review["status"])
        self.assertIn("review_export", review["inspect"])
        self.assertEqual(review["next_call"]["arguments"]["media_path"], "cached.mp4")
        self.assertLessEqual(review["next_call"]["arguments"]["end"], result["duration"])
