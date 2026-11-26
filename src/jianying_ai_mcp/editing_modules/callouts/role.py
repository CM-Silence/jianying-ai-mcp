from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .. import EditingModule


@dataclass(frozen=True, kw_only=True)
class CalloutModule(EditingModule):
    style: dict[str, Any]
    clip: dict[str, Any]
    decoration: dict[str, Any]
    native_entrance: str
    native_exit: str
    portable_entrance: str
    portable_exit: str

    def overlay_defaults(self, duration: int) -> dict[str, Any]:
        motion_time = min(250_000, max(1, duration // 3))
        return {
            "style": {
                "bold": True, "align": 1, "color": [1, 1, 1], "auto_wrapping": True, "max_line_width": .72,
                **deepcopy(self.style),
            },
            "clip": dict(self.clip),
            **deepcopy(self.decoration),
            "animations": [
                {"category": "text_intro", "name": self.native_entrance, "duration": motion_time},
                {"category": "text_outro", "name": self.native_exit, "duration": motion_time},
            ],
            "portable_animation": {"kind": self.portable_entrance, "duration_us": motion_time},
            "portable_exit_animation": {"kind": self.portable_exit, "duration_us": motion_time},
        }


def callout_parameters(role: str) -> dict[str, str]:
    return {
        "shots[].overlays[].role": role,
        "text": "标注文字，必填。",
        "offset_frame": "相对镜头起点的偏移帧数，默认 0。",
        "frames": "显示帧数，默认持续到镜头结束。",
        "style / clip": "按字段覆盖默认排版或位置。",
        "animations": "可替换剪映原生进退场。",
        "portable_animation / portable_exit_animation": "可替换 OpenFilm 进场或退场。",
    }
