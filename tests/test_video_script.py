import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jianying_ai_mcp.broll_library import index_broll_shots
from jianying_ai_mcp.caption_faces import _text_box
from jianying_ai_mcp.caption_text import semantic_caption_runs
from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.video_script import execute_script, plan_script


ROOT = Path(__file__).resolve().parents[1]


class ScriptWorkflowTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".build").mkdir(exist_ok=True)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=ROOT / ".build")))
        self.settings = replace(Settings.from_env(), work_root=self.root / "work", draft_root=self.root / "drafts",
                                jianying_stock_root=self.root / "stock")
        self.settings.jianying_stock_root.mkdir()
        self.camera = str(ROOT / "test_assets/compat_test_video.mp4")
        self.beat = {"kind": "talk", "source": self.camera, "start": 0, "end": 6, "sfx": False}

    def test_script_search_acquire_place_and_actual_caption_reveals(self):
        # Isolated provider fixture exercises the official-library branch without touching the user's catalog.
        video = self.settings.jianying_stock_root / "fixture.mp4"
        shutil.copy(ROOT / "test_assets/compat_test_video_alt.mp4", video)
        sid, = index_broll_shots(self.settings, [{"source": str(video), "start_ms": 0, "end_ms": 7000,
                                                  "description": "测试传送带"}])["shot_ids"]
        spec = {"name": "脚本回归", "width": 360, "height": 640, "beats": [
            {**self.beat, "text": "今天正常发货。"},
            {**self.beat, "text": "先扫码，再封箱。", "stock": {"query": "测试传送带",
             "layout": {"kind": "stacked", "main": "bottom"}}}]}
        plan = plan_script(self.settings, "让观众看清动作", spec)
        self.assertEqual(plan["scenes"][1]["candidates"][0]["shot_id"], sid)
        sheet = Path(plan["script_table_path"]).read_text(encoding="utf-8")
        self.assertIn("6.00–12.00s", sheet)
        self.assertFalse(self.settings.draft_root.exists())
        pending = execute_script(self.settings, plan["script_path"], {}, None, False)
        self.assertEqual(pending["status"], "stock_selection_required")
        result = execute_script(self.settings, plan["script_path"], {"2": sid}, None, False, openfilm_project=True)
        self.assertEqual(result["render_call"]["tool"], "render_openfilm")
        self.assertTrue(Path(result["render_call"]["arguments"]["project_path"]).is_dir())
        self.assertEqual(result["status"], "placed")
        self.assertEqual(result["readback"][0]["target_range_us"], {"start": 6_000_000, "duration": 6_000_000})
        normal, first, last = result["caption_readback"]
        self.assertFalse(normal["entrances"])
        self.assertEqual(first["entrances"], ["渐显"])
        self.assertEqual(last["entrances"], ["渐显"])
        self.assertEqual(first["sizes"], last["sizes"])
        self.assertEqual(first["position"]["y"], last["position"]["y"])
        self.assertLess(first["position"]["x"], last["position"]["x"])
        self.assertGreater(last["target_range_us"]["start"], first["target_range_us"]["start"])
        self.assertEqual(sum(first["target_range_us"].values()), sum(last["target_range_us"].values()))

    def test_known_face_and_manual_exclusion_move_the_completed_group(self):
        # Face covers the exact reference subtitle baseline in an otherwise empty test frame.
        spec = {"name": "避让", "width": 360, "height": 640, "beats": [{
            **self.beat, "text": "先核对客户货号，再清点包裹数量。",
            "caption": {"avoid": [[0, .62, 1, .82]]},
        }]}
        plan = plan_script(self.settings, "保留脸与手的区域", spec)
        with patch("jianying_ai_mcp.caption_faces._sample", return_value=[(.5,(640,360),[(.4,.63,.6,.77)])]):
            result = execute_script(self.settings, plan["script_path"], {}, None, False)
        report = result["caption_safety"]
        self.assertEqual((report["groups_with_faces"], report["moved_groups"], report["review"]), (1,1,[]))
        rows = result["caption_readback"]
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["position"]["y"], rows[1]["position"]["y"])
        self.assertEqual(rows[0]["position"]["x"], rows[1]["position"]["x"])
        for row in rows:
            pos = row["position"]
            _, t, _, b = _text_box({"style":{"size":row["sizes"][0]}, "text":row["text"],
                                    "clip":{"transform_x":pos["x"],"transform_y":pos["y"]}}, 360, 640)
            self.assertTrue(b <= .62*640+1e-4 or t >= .82*640-1e-4)

    def test_empty_stock_requires_recorded_fallback_and_ordinary_timed_lines_stay_single(self):
        scene = {**self.beat, "stock":{"query":"没有这种动作"}, "caption":{
            "cues":[{"at":0,"text":"今天正常发货。"},{"at":3,"text":"明天见。"}]}}
        plan = plan_script(self.settings, "保留实拍", {"name":"空库","beats":[scene]})
        self.assertEqual(plan["scenes"][0]["candidates"], [])
        result = execute_script(self.settings, plan["script_path"], {"1":"keep_camera"}, None, False)
        self.assertEqual(result["stock_decisions"][0]["status"], "kept_camera")
        self.assertEqual(result["readback"], [])
        self.assertTrue(all(not r["entrances"] for r in result["caption_readback"]))
        self.assertEqual(result["caption_readback"][0]["target_range_us"]["duration"], 3_000_000)

    def test_script_records_semantic_choice_before_render(self):
        statement = "今天上午，我们检查了发货清单。"
        self.assertTrue(all(len(group) == 1 for group in semantic_caption_runs(statement, 14)))
        steps = "先确认清单上每一个商品对应的库存货号，再检查纸箱里每种商品实际装入的件数。"
        groups = semantic_caption_runs(steps, 14)
        self.assertEqual([len(g) for g in groups], [2])
        plan = plan_script(self.settings, "排版", {"name":"分句","beats":[
            {**self.beat,"text":statement}, {**self.beat,"text":steps}]})
        a,b = plan["scenes"]
        self.assertEqual(a["caption"], "单句")
        self.assertIn("逐项淡入", b["caption"])
        self.assertEqual(len(b["caption_cues"]), 2)

    def test_script_reports_a_fully_occupied_caption_area_without_publishing(self):
        plan = plan_script(self.settings, "避让操作画面", {"name":"需调整","beats":[{
            **self.beat, "text":"查看完整画面。", "caption":{"avoid":[[0,0,1,1]]}}]})
        result = execute_script(self.settings, plan["script_path"], {}, None, False)
        self.assertEqual(result["status"], "caption_layout_required")
        self.assertFalse((self.settings.draft_root/"需调整").exists())
