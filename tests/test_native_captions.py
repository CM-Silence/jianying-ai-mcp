from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pyJianYingDraft as draft

from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.draft_ops import ManifestError, analyze_reference_draft, build_from_reference, build_manifest


ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT / "test_assets" / "compat_test_video.mp4"
CAPTIONS = (
    "1\n00:00:00,123 --> 00:00:00,876\n第一行😀\n第二行 <i>标注</i> &amp; 支持\n\n"
    "2\n00:00:00,876 --> 00:00:01,765\nSecond cue\n"
)


class NativeCaptionsTests(unittest.TestCase):
    def setUp(self) -> None:
        build_root = ROOT / ".build"
        build_root.mkdir(exist_ok=True)
        workspace = tempfile.TemporaryDirectory(dir=build_root, prefix="captions-test-")
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.settings = Settings(self.root / "drafts", self.root / "work", None)
        self.srt = self.root / "字幕.srt"
        self.srt.write_text(CAPTIONS, encoding="utf-8-sig")

    def manifest(self, *imports: dict) -> dict:
        return {
            "draft": {"name": "原生字幕验证"},
            "tracks": [{
                "type": "video",
                "name": "主画面",
                "segments": [{"source": str(VIDEO), "duration": "3s"}],
            }],
            "srt_imports": list(imports),
        }

    def native(self, **options) -> dict:
        return {"source": str(self.srt), "mode": "native", **options}

    def test_native_captions_keep_timing_style_and_task_references(self) -> None:
        manifest = self.manifest(
            self.native(
                time_offset="0.25s",
                style={"size": 9, "align": 2, "color": [0.25, 0.5, 0.75], "bold": True},
                clip={"transform_y": -0.6},
            ),
            {"source": str(self.srt), "clip": {"transform_y": 0.3}},
            self.native(track_name="English", language="EN-us", time_offset=-100_000),
        )
        result = build_manifest(self.settings, manifest)
        content = json.loads((Path(result["draft_path"]) / "draft_content.json").read_text(encoding="utf-8"))
        tracks = {track["name"]: track for track in content["tracks"]}
        texts = {text["id"]: text for text in content["materials"]["texts"]}
        tasks = content["config"]["subtitle_taskinfo"]
        self.assertEqual(content["duration"], 3_000_000)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(len({task["id"] for task in tasks}), 2)
        self.assertEqual([task["language"] for task in tasks], ["zh-CN", "en-US"])
        self.assertTrue(all(task["type"] == 10 and not task["is_local_asr"] for task in tasks))
        self.assertEqual(tracks["字幕"]["flag"], 0)
        self.assertNotIn("recognize_task_id", texts[tracks["字幕"]["segments"][0]["material_id"]])

        first, second = tracks["原生字幕"]["segments"]
        self.assertEqual(tracks["原生字幕"]["type"], "text")
        self.assertEqual(tracks["原生字幕"]["flag"], 1)
        self.assertEqual(first["target_timerange"], {"start": 373_000, "duration": 753_000})
        self.assertEqual(second["target_timerange"], {"start": 1_126_000, "duration": 889_000})
        self.assertIsNone(first["source_timerange"])
        self.assertEqual(first["extra_material_refs"], [])
        self.assertEqual(second["extra_material_refs"], [])
        self.assertEqual(first["clip"]["transform"]["y"], -0.6)
        chinese = [texts[segment["material_id"]] for segment in (first, second)]
        self.assertTrue(all(text["type"] == "subtitle" and text["add_type"] == 1 for text in chinese))
        self.assertTrue(all(text["recognize_task_id"] == tasks[0]["id"] for text in chinese))
        self.assertEqual(chinese[0]["group_id"], chinese[1]["group_id"])
        self.assertEqual(chinese[0]["recognize_text"], "第一行😀\n第二行 标注 & 支持")
        self.assertEqual(chinese[0]["alignment"], 2)
        glyph = json.loads(chinese[0]["content"])["styles"][0]
        self.assertEqual(glyph["size"], 9)
        self.assertTrue(glyph["bold"])
        self.assertEqual(glyph["fill"]["content"]["solid"]["color"], [0.25, 0.5, 0.75])
        self.assertAlmostEqual(glyph["strokes"][0]["width"], 0.08)
        self.assertEqual(glyph["range"], [0, len(chinese[0]["recognize_text"])])

        english = tracks["English"]["segments"][0]
        self.assertEqual(english["target_timerange"]["start"], 23_000)
        self.assertEqual(english["clip"]["transform"]["y"], -0.8)
        self.assertEqual(texts[english["material_id"]]["recognize_task_id"], tasks[1]["id"])

    def test_native_captions_dry_run_checks_structure_without_publishing(self) -> None:
        result = build_manifest(self.settings, self.manifest(self.native()), dry_run=True)
        self.assertIsNone(result["draft_path"])
        self.assertEqual(result["summary"]["track_types"], ["video", "text"])
        self.assertEqual(result["summary"]["segments"], 3)
        self.assertEqual(list(self.settings.draft_root.iterdir()), [])
        self.assertEqual(list(self.settings.work_root.iterdir()), [])

    def test_both_srt_modes_decorate_each_cue_without_changing_timing(self) -> None:
        # The bubble and flower IDs are the upstream SDK demo's actual Jianying resources.
        # https://github.com/GuanYixuan/pyJianYingDraft/blob/main/demo.py
        bubble = {"effect_id": "361595", "resource_id": "6742029398926430728"}
        flower = "7296357486490144036"
        decoration = {
            "font": "Alice_Regular", "bubble": bubble, "flower_text_resource_id": flower,
            "border": {"color": [0, 0, 0], "width": 20},
            "background": {"color": "#224433", "alpha": 0.6},
            "shadow": {"alpha": 0.7},
            "animations": [
                {"category": "text_loop", "name": "VHS"},
                {"category": "text_intro", "name": "冲屏位移", "duration": "0.45s"},
                {"category": "text_outro", "name": "右上弹出", "duration": "0.4s"},
            ],
        }
        manifest = self.manifest(
            {"source": str(self.srt), "decoration": decoration},
            self.native(decoration=decoration),
        )
        manifest["tracks"].append({
            "type": "text", "name": "字幕", "segments": [{"text": "已有文字", "start": "2s", "duration": "0.5s"}],
        })
        result = build_manifest(self.settings, manifest)
        path = Path(result["draft_path"]) / "draft_content.json"
        content = json.loads(path.read_text(encoding="utf-8"))
        tracks = {track["name"]: track for track in content["tracks"]}
        effects = {item["id"]: item for item in content["materials"]["effects"]}
        animations = {item["id"]: item for item in content["materials"]["material_animations"]}
        texts = {item["id"]: item for item in content["materials"]["texts"]}
        self.assertEqual(tracks["字幕"]["segments"][0]["extra_material_refs"], [])
        for name, segments in (("字幕", tracks["字幕"]["segments"][1:]), ("原生字幕", tracks["原生字幕"]["segments"])):
            with self.subTest(mode=name):
                self.assertEqual([segment["target_timerange"] for segment in segments], [
                    {"start": 123_000, "duration": 753_000}, {"start": 876_000, "duration": 889_000},
                ])
                for index, segment in enumerate(segments):
                    refs = segment["extra_material_refs"]
                    self.assertEqual(len(refs), 3)
                    text = texts[segment["material_id"]]
                    glyph = json.loads(text["content"])["styles"][0]
                    self.assertEqual(glyph["font"]["id"], draft.FontType.Alice_Regular.value.resource_id)
                    self.assertAlmostEqual(glyph["strokes"][0]["width"], 0.04)
                    self.assertEqual(text["background_color"], "#224433")
                    self.assertEqual(text["check_flag"] & 32, 32)
                    decorations = {effects[ref]["type"]: effects[ref] for ref in refs if ref in effects}
                    self.assertEqual(decorations["text_shape"]["effect_id"], bubble["effect_id"])
                    self.assertEqual(decorations["text_shape"]["resource_id"], bubble["resource_id"])
                    self.assertEqual(decorations["text_effect"]["resource_id"], flower)
                    timing = next(animations[ref]["animations"] for ref in refs if ref in animations)
                    for item in timing:
                        self.assertLessEqual(item["start"] + item["duration"], segment["target_timerange"]["duration"])
                    self.assertEqual(timing[0]["start"], 0)
                    if index == 0:
                        self.assertEqual([item["type"] for item in timing], ["in", "out"])
                        self.assertEqual(timing[0]["duration"], timing[1]["start"])
                    else:
                        self.assertEqual([item["type"] for item in timing], ["in", "out", "loop"])
                        self.assertEqual(timing[2]["start"], 450_000)
                        self.assertEqual(timing[2]["duration"], 39_000)
                    if name == "原生字幕":
                        self.assertEqual(text["recognize_task_id"], content["config"]["subtitle_taskinfo"][0]["id"])

    def test_invalid_subtitle_decorations_are_rejected(self) -> None:
        specs = [
            ({"bubble": {"effect_id": "361595"}}, "resource_id"),
            ({"font": "not_a_real_font"}, "未知字体"),
            ({"animations": [{"category": "text_intro", "name": "冲屏位移", "duration": 0}]}, "大于 0"),
            ({"animations": [{"category": "text_loop", "name": "VHS", "duration": "0.1s"}]}, "不接受 duration"),
            ({"misspelled": 1}, "未知 字幕 decoration 参数"),
        ]
        for decoration, error in specs:
            with self.subTest(decoration=decoration):
                with self.assertRaisesRegex(ManifestError, error):
                    build_manifest(self.settings, self.manifest(self.native(decoration=decoration)), dry_run=True)

    def test_both_srt_modes_highlight_editable_codepoint_ranges(self) -> None:
        base_color = [0.25, 0.5, 0.75]
        highlights = [{"cue": 1, "ranges": [[10, 11], [3, 4], [9, 10], [9, 11]]}]
        decoration = {
            "font": "Alice_Regular",
            "border": {"color": [0, 0, 0], "width": 20},
            "shadow": {"alpha": 0.7},
        }
        options = {"style": {"color": base_color, "bold": True}, "decoration": decoration, "highlights": highlights}
        manifest = self.manifest(
            {"source": str(self.srt), **options},
            self.native(highlight_color=[0, 1, 0.5], **options),
        )
        manifest["tracks"].append({
            "type": "text", "name": "字幕", "segments": [{"text": "已有文字", "start": "2s", "duration": "0.5s"}],
        })

        result = build_manifest(self.settings, manifest)
        path = Path(result["draft_path"]) / "draft_content.json"
        content = json.loads(path.read_text(encoding="utf-8"))
        tracks = {track["name"]: track for track in content["tracks"]}
        materials = {material["id"]: material for material in content["materials"]["texts"]}
        plain = json.loads(materials[tracks["字幕"]["segments"][0]["material_id"]]["content"])
        self.assertEqual(plain["text"], "已有文字")
        self.assertEqual(len(plain["styles"]), 1)
        for name, offset, color in (("字幕", 1, [1, 0.86, 0.30]), ("原生字幕", 0, [0, 1, 0.5])):
            with self.subTest(track=name):
                first, second = tracks[name]["segments"][offset:]
                first_material = materials[first["material_id"]]
                body = json.loads(first_material["content"])
                self.assertEqual(body["text"], "第一行😀\n第二行 标注 & 支持")
                self.assertEqual([run["range"] for run in body["styles"]], [[0, 3], [3, 4], [4, 9], [9, 11], [11, 16]])
                self.assertEqual([body["text"][start:end] for start, end in ((3, 4), (9, 11))], ["😀", "标注"])
                for run, fill in zip(body["styles"], [base_color, color, base_color, color, base_color], strict=True):
                    self.assertEqual(run["fill"]["content"]["solid"]["color"], fill)
                    self.assertEqual(run["font"]["id"], draft.FontType.Alice_Regular.value.resource_id)
                    self.assertTrue(run["bold"])
                    self.assertEqual(run["strokes"], body["styles"][0]["strokes"])
                    self.assertEqual(run["shadows"], body["styles"][0]["shadows"])
                    self.assertAlmostEqual(run["strokes"][0]["width"], 0.04)
                    self.assertTrue(run["shadows"])
                other = json.loads(materials[second["material_id"]]["content"])
                self.assertEqual(other["text"], "Second cue")
                self.assertEqual(len(other["styles"]), 1)
                self.assertEqual(other["styles"][0]["fill"]["content"]["solid"]["color"], base_color)
                self.assertEqual(first["target_timerange"], {"start": 123_000, "duration": 753_000})
                if name == "原生字幕":
                    self.assertEqual(tracks[name]["flag"], 1)
                    self.assertEqual(first_material["recognize_text"], body["text"])
                    self.assertEqual(first_material["recognize_task_id"], content["config"]["subtitle_taskinfo"][0]["id"])

    def test_invalid_highlights_fail_before_publishing(self) -> None:
        cases = [
            ({"highlights": None}, "highlights 必须"),
            ({"highlights": [{"cue": 1, "typo": []}]}, "必须包含 cue 和 ranges"),
            ({"highlights": [{"cue": 0, "ranges": [[0, 1]]}]}, "cue 必须"),
            ({"highlights": [{"cue": 3, "ranges": [[0, 1]]}]}, "cue 必须"),
            ({"highlights": [{"cue": True, "ranges": [[0, 1]]}]}, "cue 必须"),
            ({"highlights": [{"cue": 1, "ranges": {}}]}, "ranges 必须"),
            ({"highlights": [{"cue": 1, "ranges": [[False, 1]]}]}, "highlights 范围"),
            ({"highlights": [{"cue": 1, "ranges": [[8, 8]]}]}, "highlights 范围"),
            ({"highlights": [{"cue": 1, "ranges": [[0, 17]]}]}, "highlights 范围"),
            ({"highlights": [], "highlight_color": [1, 0.5, float("nan")]}, "highlight_color 必须"),
        ]
        for mode in ("native", "text"):
            for options, error in cases:
                with self.subTest(mode=mode, options=options):
                    with self.assertRaisesRegex(ManifestError, error):
                        build_manifest(self.settings, self.manifest(self.native(mode=mode, **options)))
        self.assertEqual(list(self.settings.draft_root.iterdir()), [])
        self.assertEqual(list(self.settings.work_root.iterdir()), [])

    def test_invalid_native_imports_fail_before_publishing(self) -> None:
        cases = [
            ({"time_offset": "-0.2s"}, "超出草稿范围"),
            ({"time_offset": "2s"}, "超出草稿范围"),
            ({"time_offset": "2"}, "带 h、m、s 单位"),
            ({"source": "字幕.srt"}, "绝对路径"),
            ({"source": str(self.root / "字幕.vtt")}, "绝对路径"),
            ({"track_name": "主画面"}, "已有轨道重复"),
            ({"language": "english"}, "语言代码"),
            ({"mode": "unknown"}, "text 或 native"),
        ]
        for options, error in cases:
            with self.subTest(options=options):
                with self.assertRaisesRegex(ManifestError, error):
                    build_manifest(self.settings, self.manifest(self.native(**options)))
        self.assertEqual(list(self.settings.draft_root.iterdir()), [])
        self.assertEqual(list(self.settings.work_root.iterdir()), [])

    def test_srt_requires_text_valid_timestamps_and_nonoverlapping_cues(self) -> None:
        cases = [
            ("", "不含字幕"),
            ("1\n00:61:00,000 --> 00:00:01,000\nText", "时间戳无效"),
            ("1\n00:00:01,000 --> 00:00:01,000\nText", "结束时间必须晚于开始时间"),
            (CAPTIONS.replace("00:00:00,876 -->", "00:00:00,750 -->"), "重叠或时间顺序"),
        ]
        for srt, error in cases:
            with self.subTest(srt=srt):
                self.srt.write_text(srt, encoding="utf-8")
                with self.assertRaisesRegex(ManifestError, error):
                    build_manifest(self.settings, self.manifest(self.native()), dry_run=True)

    def test_reference_replacement_updates_only_targeted_native_material_and_matching_twin(self) -> None:
        manifest = self.manifest(
            self.native(style={"size": 9, "bold": True}),
            {"source": str(self.srt), "clip": {"transform_y": 0.3}},
        )
        reference = build_manifest(self.settings, manifest)
        reference_path = Path(reference["draft_path"])
        source_bytes = (reference_path / "draft_content.json").read_bytes()
        (reference_path / "draft_info.json").write_bytes(source_bytes)
        result = build_from_reference(
            self.settings,
            reference["draft_name"],
            "副本",
            text_replacements=[
                {"track_name": "原生字幕", "segment_index": 0, "text": "换好的字幕"},
                {"track_name": "字幕", "segment_index": 0, "text": "普通换字"},
            ],
        )
        folder = Path(result["draft_path"])
        draft_bytes = (folder / "draft_content.json").read_bytes()
        self.assertEqual((folder / "draft_info.json").read_bytes(), draft_bytes)
        self.assertEqual((reference_path / "draft_content.json").read_bytes(), source_bytes)
        self.assertEqual((reference_path / "draft_info.json").read_bytes(), source_bytes)

        content = json.loads(draft_bytes)
        tracks = {track["name"]: track for track in content["tracks"]}
        materials = {material["id"]: material for material in content["materials"]["texts"]}
        tasks = content["config"]["subtitle_taskinfo"]
        self.assertEqual(tracks["原生字幕"]["flag"], 1)
        self.assertEqual(tasks, json.loads(source_bytes)["config"]["subtitle_taskinfo"])
        native, untouched = [materials[segment["material_id"]] for segment in tracks["原生字幕"]["segments"]]
        self.assertEqual(native["recognize_task_id"], tasks[0]["id"])
        self.assertEqual(native["recognize_text"], "换好的字幕")
        body = json.loads(native["content"])
        self.assertEqual(body["text"], "换好的字幕")
        self.assertEqual(body["styles"][0]["size"], 9)
        self.assertTrue(body["styles"][0]["bold"])
        self.assertEqual(body["styles"][0]["range"], [0, len("换好的字幕")])
        self.assertEqual(untouched["recognize_text"], "Second cue")
        legacy = materials[tracks["字幕"]["segments"][0]["material_id"]]
        self.assertEqual(json.loads(legacy["content"])["text"], "普通换字")
        self.assertNotIn("recognize_text", legacy)

        profile = analyze_reference_draft(self.settings, "副本", include_text=True)["profile"]
        native_track = next(track for track in profile["tracks"] if track["name"] == "原生字幕")
        self.assertEqual(native_track["segments"][0]["text"], "换好的字幕")

    def test_reference_preserves_different_or_encrypted_draft_info(self) -> None:
        reference = build_manifest(self.settings, self.manifest(self.native()))
        reference_info = Path(reference["draft_path"]) / "draft_info.json"
        companions = (b'{"tracks":[],"platform":"macOS"}', b"YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXo=")
        for index, companion in enumerate(companions):
            with self.subTest(companion=companion):
                reference_info.write_bytes(companion)
                result = build_from_reference(self.settings, reference["draft_name"], f"副本{index}")
                self.assertEqual((Path(result["draft_path"]) / "draft_info.json").read_bytes(), companion)
