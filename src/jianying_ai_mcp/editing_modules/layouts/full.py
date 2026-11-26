from __future__ import annotations

from typing import Any

from ...production_graphics import ProductionError
from .. import EditingModule
from .common import LAYOUT_PATH, LayoutDefinition


ID = "full"


def _resolve(raw: dict[str, Any], _width: int, _height: int, label: str) -> None:
    if set(raw) != {"kind"}:
        raise ProductionError(f"{label} 全屏构图只接受 kind")


LAYOUT = LayoutDefinition(
    module=EditingModule(
        id=ID,
        summary="全屏呈现访谈或意境素材，适合单一主体和自然留白。",
        parameters={f"{LAYOUT_PATH}.kind": f'可设为 "{ID}"，省略 layout 等同全屏。'},
        examples=({"shots": [{"layout": {"kind": ID}}]},),
    ),
    resolve=_resolve,
)
