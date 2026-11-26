import unittest
from typing import get_args

import pyJianYingDraft as draft

from jianying_ai_mcp.presets import PresetCategory, list_presets


class PresetTests(unittest.TestCase):
    def test_preset_categories_use_installed_catalog(self):
        for category in get_args(PresetCategory):
            with self.subTest(category=category):
                result = list_presets(category, limit=1)
                self.assertEqual(result["source"], "pyJianYingDraft")
                self.assertEqual(result["category"], category)
                self.assertEqual(len(result["presets"]), 1)
                self.assertGreater(result["total_matches"], 0)
                self.assertEqual(result["truncated"], result["total_matches"] > 1)
                no_matches = list_presets(category, keyword="does-not-exist-98371")
                self.assertEqual(no_matches["total_matches"], 0)

    def test_font_search_returns_manifest_name_and_resource_ids(self):
        font = draft.FontType.Alice_Regular
        result = list_presets("font", keyword="ＡＬＩＣＥ regular", is_vip=False)
        self.assertEqual(result["presets"][0], {
            "name": font.name,
            "display_name": font.value.name,
            "is_vip": False,
            "resource_id": font.value.resource_id,
            "effect_id": font.value.effect_id,
            "default_duration_us": None,
            "parameters": [],
        })
        matched_by_id = list_presets("font", keyword=font.value.resource_id)
        self.assertEqual(matched_by_id["presets"][0]["name"], font.name)
        filtered = list_presets("font", keyword="Alice-Regular", is_vip=True)
        self.assertEqual(filtered["presets"], [])

    def test_animation_and_transition_keep_microsecond_duration(self):
        animation = list_presets("video_intro", keyword="上下抖动")["presets"][0]
        transition = list_presets("transition", keyword="上下翻页")["presets"][0]
        self.assertEqual(animation["display_name"], "上下抖动")
        self.assertEqual(animation["default_duration_us"], 500_000)
        self.assertEqual(transition["default_duration_us"], 1_000_000)

    def test_effect_preserves_parameter_order_and_ranges(self):
        effect = next(member for member in draft.VideoSceneEffectType if member.value.params)
        preset = list_presets("video_effect", keyword=effect.name, limit=1)["presets"][0]
        self.assertEqual(preset["name"], effect.name)
        self.assertEqual(preset["parameters"], [
            {
                "name": parameter.name,
                "default_value": parameter.default_value,
                "min_value": parameter.min_value,
                "max_value": parameter.max_value,
            }
            for parameter in effect.value.params
        ])

    def test_mask_notice_and_vip_unknown(self):
        mask = list_presets("mask", limit=1)
        self.assertTrue(mask["experimental"])
        self.assertIn("实验", mask["notice"])
        self.assertIsNone(mask["presets"][0]["is_vip"])
        filtered = list_presets("mask", is_vip=False)
        self.assertEqual(filtered["total_matches"], 0)
        self.assertFalse(filtered["truncated"])

    def test_preset_limit_is_bounded(self):
        for limit in (0, 101):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "limit"):
                list_presets("filter", limit=limit)
