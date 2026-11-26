from __future__ import annotations

from typing import Any

from ...production_graphics import ProductionError
from . import full, interview_box, stacked
from .common import InterviewLayout


DEFAULT_LAYOUT_ID = full.LAYOUT.module.id
_LAYOUTS = {layout.module.id: layout for layout in (full.LAYOUT, stacked.LAYOUT, interview_box.LAYOUT)}
MODULES = tuple(layout.module for layout in _LAYOUTS.values())


def read_interview_layout(raw: Any, width: int, height: int, label: str) -> InterviewLayout | None:
    label = f"{label}.layout"
    if not isinstance(raw, dict):
        raise ProductionError(f"{label} 必须是包含 kind 的对象")
    kind = raw.get("kind")
    layout = _LAYOUTS.get(kind) if isinstance(kind, str) else None
    if layout is None:
        raise ProductionError(f"{label}.kind 支持 {'、'.join(_LAYOUTS)}")
    return layout.resolve(raw, width, height, label)
