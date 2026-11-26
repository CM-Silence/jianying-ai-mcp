from __future__ import annotations

import unittest
from typing import get_args

from jianying_ai_mcp.editing_modules import EditingModuleCategory
from jianying_ai_mcp.editing_modules.catalog import list_editing_modules
from jianying_ai_mcp.server import mcp


class EditingModuleCatalogTests(unittest.TestCase):
    def test_index_lists_compact_entries_for_registered_modules(self) -> None:
        entries = list_editing_modules()["modules"]
        expected = {
            ("layout", "full"), ("layout", "stacked"), ("layout", "interview_box"),
            ("caption", "interview"), ("caption", "highlights"),
            ("callout", "person"), ("callout", "emphasis"), ("callout", "section"),
        }
        actual = {(entry["category"], entry["id"]) for entry in entries}
        self.assertTrue(expected.issubset(actual))
        self.assertEqual(len(entries), len(actual))
        self.assertTrue(all(set(entry) == {"category", "id", "summary"} for entry in entries))
        self.assertEqual(list_editing_modules("layout")["modules"], [
            entry for entry in entries if entry["category"] == "layout"
        ])

    def test_module_details_are_isolated_from_later_reads(self) -> None:
        original = list_editing_modules("layout", "interview_box")
        result = list_editing_modules(module_id="interview_box")
        self.assertEqual(result, original)
        result["modules"][0]["parameters"].clear()
        result["modules"][0]["examples"][0].clear()
        self.assertEqual(list_editing_modules("layout", "interview_box"), original)


    def test_unknown_module_suggests_browsing_the_index(self) -> None:
        with self.assertRaisesRegex(ValueError, "省略 module_id 可查看模块列表"):
            list_editing_modules("caption", "interview_box")


class EditingModuleMCPTests(unittest.TestCase):
    def test_mcp_registers_the_tool_with_optional_filters(self) -> None:
        tool = mcp._tool_manager.get_tool("list_video_editing_modules")
        self.assertIsNotNone(tool)
        schema = tool.parameters
        self.assertEqual(set(schema["properties"]), {"category", "module_id"})
        self.assertEqual(schema.get("required", []), [])
        self.assertEqual(schema["properties"]["category"]["anyOf"][0]["enum"], list(get_args(EditingModuleCategory)))
