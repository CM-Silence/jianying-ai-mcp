from __future__ import annotations

import json
import math
import tempfile
import uuid
from pathlib import Path
from typing import Any, Literal

from .config import Settings
from .native_captions import NativeCaptionError, validate_generated_material_refs
from .openfilm import (
    OPENFILM_CLI_BASELINE,
    OpenFilmError,
    resolve_openfilm_project,
    run_openfilm_worker,
)


DRAFT_EXPORT_TIMEOUT = 900
_RESULT_PREFIX = "JIANYING_MCP_OPENFILM_DRAFT "
_DRAFT_FILES = {"draft_content.json", "draft_info.json", "draft_meta_info.json"}


def export_openfilm_draft(
    settings: Settings,
    project_path: str,
    *,
    publish: bool = False,
    media: Literal["copy", "link"] = "copy",
    fps: float = 30,
) -> dict[str, Any]:
    """Ask OpenFilm to serialize its whole timeline as a Jianying draft."""
    project = resolve_openfilm_project(project_path)
    if media not in ("copy", "link"):
        raise OpenFilmError("media 必须是 copy 或 link")
    if type(fps) not in (int, float) or not math.isfinite(fps) or not 1 <= fps <= 120:
        raise OpenFilmError("fps 必须是 1 到 120 之间的数字")

    destination = settings.draft_root if publish else settings.work_root / "openfilm-drafts"
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    export_id = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(dir=destination, prefix=".openfilm-draft-") as staging_dir:
        staging = Path(staging_dir)
        request = {
            "id": export_id,
            "name": f"{project.name[:80]}_{export_id[:12]}",
            "project": str(project),
            "destination": str(staging),
            "media": media,
            "fps": fps,
            "version": OPENFILM_CLI_BASELINE,
            "timeoutMs": (DRAFT_EXPORT_TIMEOUT - 15) * 1000,
        }
        stdout, _ = run_openfilm_worker(
            settings,
            "openfilm_draft.mjs",
            [],
            cwd=project,
            timeout=DRAFT_EXPORT_TIMEOUT,
            input_text=json.dumps(request),
        )
        draft, staged_draft, outputs = _checked_export(stdout, staging)
        draft_path = destination / staged_draft.name
        if draft_path.exists():
            raise FileExistsError(draft_path)
        _relocate_draft_meta(staged_draft, draft_path)
        staged_draft.rename(draft_path)
    return {
        "success": True,
        "project_path": str(project),
        "draft_name": draft_path.name,
        "draft_path": str(draft_path),
        "published": publish,
        "media": media,
        "fps": draft.get("fps"),
        "artifact_paths": [str(draft_path / path.relative_to(staged_draft)) for path in outputs],
    }


def _checked_export(stdout: str, staging: Path) -> tuple[dict[str, Any], Path, list[Path]]:
    response = next((line[len(_RESULT_PREFIX):] for line in reversed(stdout.splitlines())
                     if line.startswith(_RESULT_PREFIX)), None)
    try:
        result = json.loads(response) if response else {}
    except json.JSONDecodeError as exc:
        raise OpenFilmError("OpenFilm 返回了无效的导出结果") from exc
    if not isinstance(result, dict):
        raise OpenFilmError("OpenFilm 返回了无效的导出结果")
    raw_outputs = result.get("outputs")
    if not isinstance(raw_outputs, list) or not all(isinstance(path, str) for path in raw_outputs):
        raise OpenFilmError("OpenFilm 返回了无效的草稿文件列表")
    outputs = [Path(path).resolve() for path in raw_outputs]
    content = next((path for path in outputs if path.name == "draft_content.json"), None)
    if content is None or content.parent.parent != staging:
        raise OpenFilmError("OpenFilm 返回的草稿目录不在预期位置")
    draft_path = content.parent
    if any(not path.is_relative_to(draft_path) or not path.is_file() for path in outputs):
        raise OpenFilmError("OpenFilm 返回的草稿文件缺失或超出草稿目录")
    if not _DRAFT_FILES.issubset(path.name for path in outputs if path.parent == draft_path):
        raise OpenFilmError("OpenFilm 返回的草稿结构不完整")

    raw = content.read_bytes()
    if (draft_path / "draft_info.json").read_bytes() != raw:
        raise OpenFilmError("OpenFilm 生成的两份草稿正文不一致")
    draft = json.loads(raw.decode("utf-8"))
    if not isinstance(draft, dict) or not isinstance(draft.get("tracks"), list):
        raise OpenFilmError("OpenFilm 生成的草稿缺少轨道数据")
    try:
        validate_generated_material_refs(draft)
    except NativeCaptionError as exc:
        raise OpenFilmError(str(exc)) from exc
    return draft, draft_path, outputs


def _relocate_draft_meta(staged_draft: Path, draft_path: Path) -> None:
    # OpenFilm writes copied media as draft-relative paths, but the meta file stores absolute draft locations.
    path = staged_draft / "draft_meta_info.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise OpenFilmError("OpenFilm 生成的草稿元数据不是 JSON 对象")
    if "draft_fold_path" in meta:
        meta["draft_fold_path"] = str(draft_path)
    if "draft_root_path" in meta:
        meta["draft_root_path"] = str(draft_path.parent)
    path.write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
