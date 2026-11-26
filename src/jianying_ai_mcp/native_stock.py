"""Reuse an editor-authored stock selection without decoding its video cache."""
from __future__ import annotations

import json
import uuid
from copy import deepcopy
from pathlib import Path

import pyJianYingDraft as draft

from .config import Settings
from .draft_crypto import load_draft_json
from .jianying_stock import is_draft_media, stock_origin, stock_roots


def _resource_id(material: dict) -> str | None:
    for field in ("resource_id", "origin_material_id", "material_id"):
        value = material.get(field)
        if type(value) in (str, int) and str(value).strip() not in ("", "0", material.get("id")):
            return str(value)
    return None


def read_stock_selection(settings: Settings, selection: str) -> dict[Path, dict]:
    file = Path(selection).expanduser()
    if not file.is_absolute():
        raise ValueError("请指定剪映官方素材采集稿或其时间线文件的绝对路径")
    if file.is_dir():
        file = next((file / name for name in ("draft_content.json", "draft_info.json")
                     if (file / name).is_file()), file / "draft_content.json")
    content = load_draft_json(settings, file, label="官方素材采集稿").content
    materials = content.get("materials") or {}
    by_id = {row["id"]: (kind, row) for kind, rows in materials.items() if isinstance(rows, list)
             for row in rows if isinstance(row, dict) and row.get("id")}
    roots = stock_roots(settings)
    entries = {}
    for track in content.get("tracks", []):
        for segment in track.get("segments", []):
            kind, video = by_id.get(segment.get("material_id"), ("", {}))
            path = Path(video.get("path") or "")
            asset_id = _resource_id(video)
            if (track.get("type") != "video" or kind != "videos" or not path.is_absolute()
                    or not asset_id or video.get("type") != "video"
                    or any(type(video.get(k)) is not int or video[k] <= 0 for k in ("duration", "width", "height"))):
                raise ValueError("采集稿只可包含从官方面板添加的原始视频，且须带资源 ID、尺寸、时长和共享缓存路径")
            path = path.resolve()
            if stock_origin(roots, path) != "online_material_cache" or is_draft_media(settings, path) or not path.is_file():
                raise ValueError("采集稿中有视频未指向剪映共享官方素材缓存")
            extras = []
            for ref in segment.get("extra_material_refs") or []:
                group, sidecar = by_id.get(ref, ("", {}))
                if group not in {"speeds", "canvases", "sound_channel_mappings", "material_colors",
                                 "vocal_separations", "placeholder_infos"}:
                    raise ValueError("采集稿含附加编辑或未知素材引用，请使用刚从官方面板添加的镜头")
                extras.append([group, sidecar])
            if segment.get("common_keyframes") or segment.get("keyframe_refs") or segment.get("reverse"):
                raise ValueError("采集稿中的原始镜头须保留正向播放且无关键帧")
            if path in entries and entries[path]["asset_id"] != asset_id:
                raise ValueError("同一缓存对应多个官方素材 ID")
            entries[path] = {"asset_id": asset_id, "video": video, "segment": segment, "extras": extras}
    if not entries:
        raise ValueError("该时间线里没有官方素材片段")
    return entries


def stock_panel(source_size, rect, canvas, focus=(.5, .5), fit="cover") -> dict:
    sw, sh = source_size
    x, y, w, h = rect
    width, height = canvas
    ratio = (w / h) / (sw / sh)
    cw, ch = min(1., ratio), min(1., 1 / ratio)
    if fit == "contain":
        factor = min(w / sw, h / sh)
        new_w, new_h = sw * factor, sh * factor
        x, y = x + (w - new_w) * focus[0], y + (h - new_h) * focus[1]
        w, h, cw, ch = new_w, new_h, 1., 1.
    left, top = (1 - cw) * focus[0], (1 - ch) * focus[1]
    scale = max(w / width, h / height)
    return {"stock_crop": [left, top, left + cw, top + ch], "panel_rect": list(rect),
            "clip": {"scale_x": scale, "scale_y": scale, "transform_x": (2 * x + w) / width - 1,
                     "transform_y": 1 - (2 * y + h) / height}}


class NativeStockMaterial(draft.VideoMaterial):
    def __init__(self, receipt: dict, path: Path, crop: list):
        self.original = receipt["video"]
        self.path = str(path)
        self.material_id = uuid.uuid4().hex
        self.material_name = self.original.get("material_name") or path.name
        self.width, self.height, self.duration = (self.original[k] for k in ("width", "height", "duration"))
        self.material_type = "video"
        left, top, right, bottom = crop
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise ValueError("原生库存镜头的裁切区域无效")
        self.crop_settings = draft.CropSettings(upper_left_x=left, upper_left_y=top, upper_right_x=right,
                                               upper_right_y=top, lower_left_x=left, lower_left_y=bottom,
                                               lower_right_x=right, lower_right_y=bottom)

    def export_json(self) -> dict:
        original = deepcopy(self.original)
        for key in ("material_id", "local_material_id"):
            if original.get(key) == original["id"]:
                original[key] = self.material_id
        return {**original, "id": self.material_id, "path": self.path,
                "crop": self.crop_settings.export_json(), "crop_ratio": "free", "crop_scale": 1.0}


def write_stock_metadata(path: Path, receipts: dict[str, dict]) -> None:
    """Retain native commerce/source fields and remap clip-local sidecar identities."""
    content = json.loads(path.read_text(encoding="utf-8"))
    for track in content["tracks"]:
        for built in track["segments"]:
            receipt = receipts.get(built["id"])
            if not receipt:
                continue
            original = receipt["segment"]
            for key, value in original.items():
                if key not in built:
                    built[key] = value
            for group, item in receipt["extras"]:
                if group == "speeds":
                    continue  # The SDK already wrote the playback rate for this placement.
                fresh = {**item, "id": uuid.uuid4().hex}
                content["materials"].setdefault(group, []).append(fresh)
                built["extra_material_refs"].append(fresh["id"])
    path.write_text(json.dumps(content, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
