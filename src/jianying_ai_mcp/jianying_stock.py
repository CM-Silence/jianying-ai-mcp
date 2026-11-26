"""Discover playable video from the editor's shared online material cache or a designated stock folder."""
from __future__ import annotations

import os
from pathlib import Path

from .config import Settings, default_draft_root
from .media_info import MediaProbeError, parse_media_info


def is_draft_media(settings: Settings, path: Path) -> bool:
    if path.is_relative_to(settings.draft_root.resolve()):
        return True
    return any(
        parent.name.casefold().startswith("com.lveditor.") and "draft" in parent.name.casefold()
        or (parent / "draft_content.json").is_file() or (parent / "draft_meta_info.json").is_file()
        for parent in (path, *path.parents) if parent.is_dir()
    )


def stock_roots(settings: Settings) -> dict[Path, str]:
    roots = {}
    # A moved projects folder can coexist with the default application cache.
    for draft_root in (settings.draft_root, default_draft_root()):
        if draft_root.parent.name.casefold() == "projects":
            cache = draft_root.parent.parent / "Cache" / "onlineMaterial"
            roots[cache.resolve()] = "online_material_cache"
    if settings.jianying_stock_root:
        root = settings.jianying_stock_root.resolve()
        if root.is_absolute() and root != root.parent and not is_draft_media(settings, root):
            roots.setdefault(root, "designated_stock_folder")
        else:
            raise ValueError("JIANYING_STOCK_ROOT 需要指向独立保存剪映素材库下载视频的目录")
    return roots


def stock_origin(roots: dict[Path, str], path: Path) -> str | None:
    return next((origin for root, origin in roots.items() if path.is_relative_to(root)), None)


def _video_candidate(file: Path) -> bool:
    if file.suffix:
        return file.suffix.lower() in {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".ts", ".mxf"}
    with file.open("rb") as stream:
        header = stream.read(12)
    return header[4:8] == b"ftyp" or header.startswith(b"\x1aE\xdf\xa3") or header[8:12] == b"AVI "


def list_stock_videos(settings: Settings, *, offset: int = 0, limit: int = 50) -> dict:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("offset 须为非负整数，limit 为 1–100")
    roots = stock_roots(settings)
    videos = []
    editor_candidates = []
    seen = set()
    unreadable = 0
    for root, origin in roots.items():
        if not root.is_dir() or is_draft_media(settings, root):
            continue
        for base, dirs, files in os.walk(root):
            folder = Path(base)
            if is_draft_media(settings, folder):
                dirs.clear()
                continue
            dirs[:] = sorted(d for d in dirs if not (folder / d).is_symlink() and (folder / d).resolve().is_relative_to(root))
            for name in sorted(files):
                path = (folder / name).resolve()
                if not path.is_relative_to(root) or path in seen or is_draft_media(settings, path):
                    continue
                seen.add(path)
                try:
                    if not _video_candidate(path):
                        continue
                    info = parse_media_info(settings, str(path))["media_info"]
                except (OSError, MediaProbeError):
                    unreadable += 1
                    if origin == "online_material_cache" and path.suffix.lower() == ".mp4":
                        editor_candidates.append({"source": str(path), "origin": origin})
                    continue
                if info["type"] == "video" and info["duration_ms"]:
                    videos.append({"source": str(path), "origin": origin, "duration_ms": info["duration_ms"]})
    return {"videos": videos[offset:offset + limit], "total": len(videos), "unreadable": unreadable,
            "needs_editor_preview": editor_candidates[offset:offset + limit], "editor_candidate_count": len(editor_candidates),
            "locations": [{"path": str(p), "origin": origin, "exists": p.is_dir()} for p, origin in roots.items()]}
