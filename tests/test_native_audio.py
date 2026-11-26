from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.draft_ops import ManifestError, build_manifest


ROOT = Path(__file__).resolve().parents[1]


class NativeAudioTests(unittest.TestCase):
    def setUp(self) -> None:
        build = ROOT / ".build"
        build.mkdir(exist_ok=True)
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="native-audio-test-", dir=build))
        self.root = Path(directory)
        self.settings = Settings(self.root / "drafts", self.root / "work", None)
        self.local_audio = ROOT / "test_assets" / "compat_test_music.wav"
        self.sound = {
            "id": "test-native-sound-id", "name": "测试原生音效", "type": "sound", "duration": 2_000_000,
            "path": str(self.local_audio), "effect_id": "7321234567890123456", "category_id": "101",
            "local_material_id": "test-native-sound-id", "source_platform": 0,
        }
        self.music = {
            "id": "test-native-music-id", "name": "测试草稿音乐", "type": "music", "duration": 4_000_000,
            "path": "<cloud://7321234567890123457>", "music_id": "7321234567890123457",
            "local_material_id": "test-native-music-id", "source_platform": 0,
        }
        self.reference = self.root / "draft_content.json"
        self.reference.write_text(json.dumps({
            "materials": {"audios": [self.sound, self.music]},
            "tracks": [{"type": "audio", "segments": [{"material_id": self.sound["id"]}, {"material_id": self.music["id"]}]}],
        }, ensure_ascii=False), encoding="utf-8")

    def _selector(self, material_id: str) -> dict[str, str]:
        return {"draft_path": str(self.reference), "material_id": material_id}

    def test_build_preserves_verified_local_and_editor_audio_material_metadata(self) -> None:
        original = self.reference.read_bytes()
        result = build_manifest(self.settings, {
            "draft": {"name": "原生音频选择"},
            "tracks": [{"type": "audio", "name": "声音", "segments": [
                {"jianying_audio": self._selector(self.sound["id"]), "source": str(self.local_audio), "duration": "1s"},
                {"jianying_audio": self._selector(self.music["id"]), "start": "1s", "duration": "1s", "volume": 0.6},
                {"source": str(self.local_audio), "start": "2s", "duration": "1s"},
            ]}],
        })
        content = json.loads((Path(result["draft_path"]) / "draft_content.json").read_text(encoding="utf-8"))
        sound, music, usual = content["materials"]["audios"]
        self.assertEqual([material["type"] for material in (sound, music, usual)], ["sound", "music", "extract_music"])
        self.assertEqual(sound["effect_id"], self.sound["effect_id"])
        self.assertEqual(sound["category_id"], self.sound["category_id"])
        self.assertEqual(sound["path"], str(self.local_audio))
        self.assertNotEqual(sound["id"], self.sound["id"])
        self.assertEqual(sound["local_material_id"], sound["id"])
        self.assertEqual(music["music_id"], self.music["music_id"])
        self.assertEqual(music["path"], self.music["path"])
        self.assertEqual(music["duration"], 4_000_000)
        self.assertNotEqual(music["id"], self.music["id"])
        self.assertEqual([item["material_id"] for item in content["tracks"][0]["segments"]],
                         [sound["id"], music["id"], usual["id"]])
        self.assertEqual(content["tracks"][0]["segments"][1]["volume"], 0.6)
        self.assertEqual(self.reference.read_bytes(), original)

    def test_mismatched_source_does_not_borrow_native_identity(self) -> None:
        with self.assertRaisesRegex(ManifestError, "source 必须与"):
            build_manifest(self.settings, {
                "draft": {"name": "错误音频关联"},
                "tracks": [{"type": "audio", "name": "配乐", "segments": [{
                    "jianying_audio": self._selector(self.music["id"]), "source": str(self.local_audio), "duration": "1s",
                }]}],
            }, dry_run=True)


if __name__ == "__main__":
    unittest.main()
