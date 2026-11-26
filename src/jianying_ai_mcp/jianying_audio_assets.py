from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from .config import Settings
from .draft_crypto import DraftCryptoError, load_draft_json


class JianyingAudioAssetError(ValueError):
    pass


_DRAFT_PATH_PREFIX = re.compile(r"^##_draftpath_placeholder_[0-9a-fA-F-]+_##[/\\]?")
_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_CLOUD_PATH = re.compile(r"^<cloud://([A-Za-z0-9_-]+)>$")
_NATIVE_TYPES = {"music": "music", "sound": "sound_effect"}


def _draft_file(draft_path: str) -> Path:
    if not isinstance(draft_path, str) or not draft_path.strip():
        raise JianyingAudioAssetError("draft_path 必须是已有剪映草稿的本地绝对路径")
    path = Path(draft_path).expanduser()
    if not path.is_absolute() or path.drive.startswith("\\\\"):
        raise JianyingAudioAssetError("draft_path 必须是已有剪映草稿的本地绝对路径")
    path = path.resolve()
    if path.drive.startswith("\\\\"):
        raise JianyingAudioAssetError("不读取网络路径中的剪映草稿")
    if path.is_dir():
        names = ("draft_info.json", "draft_content.json") if sys.platform == "darwin" else ("draft_content.json", "draft_info.json")
        path = next((p for name in names if (p := path / name).is_file()), path)
    path = path.resolve()
    if path.drive.startswith("\\\\") or path.suffix.lower() != ".json" or not path.is_file():
        raise JianyingAudioAssetError("未找到可读的 draft_content.json 或 draft_info.json")
    return path


def _audio_rows(settings: Settings, draft_path: str) -> tuple[Path, str, list[dict[str, Any]]]:
    file = _draft_file(draft_path)
    try:
        loaded = load_draft_json(settings, file, label="音频来源草稿")
    except DraftCryptoError as exc:
        raise JianyingAudioAssetError(str(exc)) from exc
    materials = loaded.content.get("materials")
    rows = materials.get("audios", []) if isinstance(materials, dict) else None
    if not isinstance(rows, list):
        raise JianyingAudioAssetError("草稿中没有有效的 materials.audios 素材列表")
    return file, loaded.source_format, [row for row in rows if isinstance(row, dict)]


def _library_id(row: dict[str, Any]) -> str | None:
    keys = ("music_id", "resource_id") if row.get("type") == "music" else ("effect_id", "resource_id")
    for key in keys:
        value = row.get(key)
        if type(value) is int and value > 0:
            return str(value)
        if isinstance(value, str) and value.strip() and value.strip() != "0":
            return value.strip()
    return None


def _source_path(row: dict[str, Any], draft_dir: Path, library_id: str) -> tuple[str | None, str]:
    raw = row.get("path")
    if raw is None or raw == "":
        return None, "editor_reference"
    if not isinstance(raw, str):
        return None, "unsupported_path"
    raw = raw.strip()
    if not raw:
        return None, "editor_reference"
    if row.get("type") == "music" and (cloud := _CLOUD_PATH.fullmatch(raw)) and cloud[1] == library_id:
        return None, "editor_reference"
    if _URL.match(raw) or raw.startswith(("<", "\\\\", "//")):
        return None, "unsupported_path"
    normalized = _DRAFT_PATH_PREFIX.sub("", raw).replace("\\", "/")
    candidate = Path(normalized)
    if candidate.drive.startswith("\\\\"):
        return None, "unsupported_path"
    relative = not candidate.is_absolute()
    try:
        path = (draft_dir / candidate if relative else candidate).resolve()
        if path.drive.startswith("\\\\") or (relative and not path.is_relative_to(draft_dir)):
            return None, "unsupported_path"
        if not path.is_file():
            return None, "missing_file"
    except (OSError, ValueError, RuntimeError):
        return None, "unsupported_path"
    return str(path), "local_file"


def _entry(row: dict[str, Any], draft_dir: Path) -> dict[str, Any] | None:
    material_type, material_id, duration = row.get("type"), row.get("id"), row.get("duration")
    if material_type not in ("music", "sound") or not isinstance(material_id, str) or not material_id.strip():
        return None
    if type(duration) is not int or duration <= 0 or not (library_id := _library_id(row)):
        return None
    source, availability = _source_path(row, draft_dir, library_id)
    name = row.get("name") or row.get("material_name") or library_id
    return {
        "material_id": material_id,
        "library_id": library_id,
        "kind": _NATIVE_TYPES[material_type],
        "name": name if isinstance(name, str) else library_id,
        "duration_us": duration,
        "source": source,
        "availability": availability,
        "copyright_limit_type": row.get("copyright_limit_type") if isinstance(row.get("copyright_limit_type"), str) else None,
    }


def inspect_jianying_audio(
    settings: Settings,
    draft_path: str,
    *,
    keyword: str | None = None,
    offset: int = 0,
    limit: int = 40,
) -> dict[str, Any]:
    """List native audio references already present in an explicitly selected draft."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise JianyingAudioAssetError("offset 须为非负整数，limit 须为 1 到 100")
    if keyword is not None and not isinstance(keyword, str):
        raise JianyingAudioAssetError("keyword 必须是字符串")
    file, source_format, rows = _audio_rows(settings, draft_path)
    results = []
    query = (keyword or "").strip().casefold()
    for row in rows:
        entry = _entry(row, file.parent)
        if entry is not None and (not query or any(query in entry[key].casefold() for key in ("name", "kind", "library_id", "material_id"))):
            results.append(entry)
    return {
        "draft_path": str(file), "source_format": source_format, "assets": results[offset:offset + limit],
        "total": len(results), "offset": offset, "next_offset": offset + limit if offset + limit < len(results) else None,
    }


def resolve_jianying_audio(settings: Settings, selector: dict[str, Any]) -> dict[str, Any]:
    """Resolve one saved native music/SFX material without fetching library media."""
    if not isinstance(selector, dict) or selector.keys() != {"draft_path", "material_id"}:
        raise JianyingAudioAssetError("jianying_audio 须包含 draft_path 和 material_id")
    material_id = selector["material_id"]
    if not isinstance(material_id, str) or not material_id.strip():
        raise JianyingAudioAssetError("material_id 必须使用来源草稿中的素材 ID")
    file, _, rows = _audio_rows(settings, selector["draft_path"])
    matches = [row for row in rows if row.get("id") == material_id]
    if len(matches) != 1 or (entry := _entry(matches[0], file.parent)) is None:
        raise JianyingAudioAssetError("未找到唯一有效的剪映原生音乐或音效素材，请先从来源草稿列出素材")
    if entry["availability"] in ("missing_file", "unsupported_path"):
        raise JianyingAudioAssetError("剪映素材文件不可用或路径不受支持，请在剪映中恢复或下载素材后再试")
    return {**entry, "material": matches[0]}
