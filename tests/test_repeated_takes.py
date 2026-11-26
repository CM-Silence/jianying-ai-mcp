from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.repeated_takes import find_repeated_takes


BATCH_ID = "c1" * 16


class RepeatedTakesTests(unittest.TestCase):
    def setUp(self) -> None:
        workspace = Path(__file__).resolve().parents[1] / ".build"
        workspace.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=workspace, prefix="repeated-takes-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.settings = Settings(root / "drafts", root / "work", None)
        self.sources: list[dict] = []

    def add(self, source_id: str, *cues: tuple[int, int, str], review: bool = False) -> None:
        report = self.settings.work_root / "video_analyses" / source_id / "analysis.json"
        report.parent.mkdir(parents=True)
        report.write_text(json.dumps({"transcript": [
            {"start_ms": start, "end_ms": end, "text": text, "quality": "review" if review else "accepted"}
            for start, end, text in cues
        ]}, ensure_ascii=False), encoding="utf-8")
        self.sources.append({
            "source_id": source_id, "analysis_id": (source_id * 20)[:20], "transcript_count": len(cues),
            "analysis_report_path": str(report), "transcript_source": "whisper:local",
            "audio_evidence": {"scope": "source", "speech_segments": 1},
        })

    def find(self, **options) -> dict:
        batch = self.settings.work_root / "media_batches" / BATCH_ID / "batch.json"
        batch.parent.mkdir(parents=True, exist_ok=True)
        batch.write_text(json.dumps({"sources": self.sources}), encoding="utf-8")
        return find_repeated_takes(self.settings, BATCH_ID, **options)

    def test_groups_traditional_and_simplified_takes_with_distinct_timecodes(self) -> None:
        self.add("m0001", (100, 2500, "先把要交付的東西寫清楚，再確認誰負責驗收。"))
        self.add("m0002", (400, 3400, "先把要交付的东西写清楚，再确认谁负责验收。"), review=True)
        self.add("m0003", (200, 3200, "这个镜头讨论颜色、光线和拍摄机位。"))
        result = self.find()
        self.assertEqual(len(result["groups"]), 1)
        group = result["groups"][0]
        self.assertTrue(group["exact_transcript_match"])
        self.assertEqual(group["minimum_similarity"], 1.0)
        self.assertEqual([(take["source_id"], take["start_ms"], take["end_ms"]) for take in group["takes"]], [
            ("m0001", 100, 2500), ("m0002", 400, 3400),
        ])
        self.assertTrue(group["takes"][1]["needs_transcript_review"])
        self.assertTrue(group["takes"][1]["source_speech_detected"])
        self.assertEqual(self.find(source_ids=["m0001", "m0003"])["groups"], [])

    def test_qualifications_and_quantities_remain_distinct(self) -> None:
        common = "先把具体要求和交付标准整理清楚，再确认这份工作"
        self.add("m0001", (100, 3500, common + "需要三个人来做。"))
        self.add("m0002", (100, 3500, common + "需要两个人来做。"))
        self.add("m0003", (100, 3500, common + "不需要三个人来做。"))
        self.assertFalse(self.find()["groups"])

    def test_adjacent_cues_match_single_cues_at_separate_take_ranges(self) -> None:
        first = "明确一份需要实际交付的文件"
        second = "然后用同一套标准来检查能不能上线"
        self.add("m0001", (0, 1600, first), (1600, 3300, second), (6500, 9000, first + second))
        result = self.find(max_groups=2)
        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual([(take["start_ms"], take["end_ms"]) for take in result["groups"][0]["takes"]], [(0, 3300), (6500, 9000)])


if __name__ == "__main__":
    unittest.main()
