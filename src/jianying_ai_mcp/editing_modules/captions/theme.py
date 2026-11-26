from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .. import EditingModule


@dataclass(frozen=True, kw_only=True)
class CaptionThemeModule(EditingModule):
    style: dict[str, Any]
    decoration: dict[str, Any]
    position: float
    inherit_background: bool = True
