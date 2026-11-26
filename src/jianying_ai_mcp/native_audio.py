from __future__ import annotations

import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

import pyJianYingDraft as draft

from .config import Settings
from .jianying_audio_assets import resolve_jianying_audio


class NativeAudioMaterial(draft.AudioMaterial):
    """Expose a selected Jianying material to the SDK without changing its resource identity."""

    def __init__(self, asset: dict[str, Any]):
        original = asset["material"]
        self._original = deepcopy(original)
        source = asset["source"]
        self.local_source = Path(source) if source is not None else None
        name = asset["name"]
        if source is not None:
            super().__init__(source, material_name=name)
        else:
            self.material_id = uuid.uuid4().hex
            self.material_name = name
            self.path = (original.get("path") or "").strip()
            self.duration = asset["duration_us"]

    def export_json(self) -> dict[str, Any]:
        material = deepcopy(self._original)
        old_id = material.get("id")
        material.update({"id": self.material_id, "path": self.path, "duration": self.duration})
        if old_id and material.get("local_material_id") == old_id:
            material["local_material_id"] = self.material_id
        return material


def audio_material_from_item(
    settings: Settings, item: dict[str, Any], native_materials: dict[tuple[str, str], NativeAudioMaterial],
) -> draft.AudioMaterial:
    selector = item.get("jianying_audio")
    if selector is None:
        source = Path(item["source"]).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        return draft.AudioMaterial(str(source))

    if not isinstance(selector, dict) or set(selector) != {"draft_path", "material_id"} or not all(
        isinstance(value, str) and value for value in selector.values()
    ):
        raise ValueError("jianying_audio 必须使用 {draft_path, material_id} 选择已有草稿中的音频")
    key = (selector["draft_path"], selector["material_id"])
    material = native_materials.get(key)
    if material is None:
        material = NativeAudioMaterial(resolve_jianying_audio(settings, selector))
        native_materials[key] = material
    explicit_source = item.get("source")
    if explicit_source is not None and (
        material.local_source is None or Path(explicit_source).expanduser().resolve() != material.local_source
    ):
        raise ValueError("jianying_audio 的 source 必须与所选草稿素材解析出的本地音频相同")
    return material
