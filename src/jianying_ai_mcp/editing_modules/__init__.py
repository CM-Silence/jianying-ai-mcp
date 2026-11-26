"""Reusable editing modules for the production timeline."""

from dataclasses import dataclass
from typing import Any, Literal


EditingModuleCategory = Literal["layout", "caption", "callout"]


@dataclass(frozen=True, kw_only=True)
class EditingModule:
    id: str
    summary: str
    parameters: dict[str, Any]
    examples: tuple[dict[str, Any], ...]
