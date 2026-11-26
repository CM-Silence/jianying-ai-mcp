from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jianying_ai_mcp.config import Settings
from jianying_ai_mcp.openfilm import OpenFilmError
from jianying_ai_mcp.openfilm_draft import export_openfilm_draft


class OpenFilmDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        build = Path(__file__).resolve().parents[1] / ".build"
        build.mkdir(exist_ok=True)
        scope = tempfile.TemporaryDirectory(dir=build, prefix="openfilm-draft-test-")
        self.addCleanup(scope.cleanup)
        self.root = Path(scope.name)
        self.project = self.root / "短片"
        self.project.mkdir()
        (self.project / "film.html").write_text("<section></section>", encoding="utf-8")
        self.settings = Settings(self.root / "drafts", self.root / "work", None)

    def fake_export(self, settings, worker, args, *, cwd, timeout, input_text):
        self.assertIs(settings, self.settings)
        self.assertEqual(worker, "openfilm_draft.mjs")
        self.assertEqual(args, [])
        self.assertEqual(cwd, self.project)
        request = json.loads(input_text)
        self.assertEqual(request["version"], "0.4.1")
        self.assertEqual(request["project"], str(self.project))
        self.assertLess(request["timeoutMs"], timeout * 1000)
        folder = Path(request["destination"]) / request["name"]
        folder.mkdir()
        files = []
        for name in ("draft_content.json", "draft_info.json", "draft_meta_info.json"):
            path = folder / name
            document = {"fps": 30, "tracks": []}
            if name == "draft_meta_info.json":
                document = {"draft_fold_path": str(folder), "draft_root_path": str(folder.parent)}
            path.write_text(json.dumps(document), encoding="utf-8")
            files.append(str(path))
        return "log\nJIANYING_MCP_OPENFILM_DRAFT " + json.dumps({"outputs": files}), ""

    @patch("jianying_ai_mcp.openfilm_draft.run_openfilm_worker")
    def test_preview_and_published_export_use_separate_roots(self, worker) -> None:
        worker.side_effect = self.fake_export
        preview = export_openfilm_draft(self.settings, str(self.project))
        self.assertFalse(preview["published"])
        self.assertFalse(self.settings.draft_root.exists())
        self.assertEqual(Path(preview["draft_path"]).parent, self.settings.work_root / "openfilm-drafts")
        published = export_openfilm_draft(self.settings, str(self.project), publish=True, media="link")
        self.assertTrue(published["published"])
        self.assertEqual(published["media"], "link")
        self.assertEqual(Path(published["draft_path"]).parent, self.settings.draft_root)
        self.assertEqual(len(published["artifact_paths"]), 3)
        meta = json.loads((Path(published["draft_path"]) / "draft_meta_info.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["draft_fold_path"], published["draft_path"])
        self.assertEqual(meta["draft_root_path"], str(self.settings.draft_root))
        self.assertEqual(list(self.settings.draft_root.iterdir()), [Path(published["draft_path"])])

    @patch("jianying_ai_mcp.openfilm_draft.run_openfilm_worker")
    def test_rejects_dangling_generated_refs_before_publishing(self, worker) -> None:
        def damaged_export(*args, **kwargs):
            stdout, stderr = self.fake_export(*args, **kwargs)
            info_path = Path(json.loads(stdout.split("JIANYING_MCP_OPENFILM_DRAFT ")[1])["outputs"][0])
            draft = {
                "tracks": [{"type": "text", "segments": [{"material_id": "caption", "extra_material_refs": ["ghost"]}]}],
                "materials": {"texts": [{"id": "caption", "type": "text"}]},
            }
            for path in (info_path, info_path.with_name("draft_info.json")):
                path.write_text(json.dumps(draft), encoding="utf-8")
            return stdout, stderr

        worker.side_effect = damaged_export
        with self.assertRaisesRegex(OpenFilmError, "附加素材不存在"):
            export_openfilm_draft(self.settings, str(self.project), publish=True)
        self.assertEqual(list(self.settings.draft_root.iterdir()), [])

    @patch("jianying_ai_mcp.openfilm_draft.run_openfilm_worker")
    def test_rejects_external_output(self, worker) -> None:
        path = self.project / "draft_content.json"
        worker.return_value = "JIANYING_MCP_OPENFILM_DRAFT " + json.dumps({"outputs": [str(path)]}), ""
        with self.assertRaisesRegex(OpenFilmError, "不在预期位置"):
            export_openfilm_draft(self.settings, str(self.project))
