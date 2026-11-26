"""Locally synthesised sound design: a small, licence-clean SFX palette.

Every cue is generated from a deterministic recipe with numpy, so a series can
reuse the same palette across episodes without shipping audio assets. Cue names
describe an edit decision (a cut, a build, a reveal), not a library recording.
"""

from __future__ import annotations

import hashlib
import math
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .config import Settings
from .jianying_audio_assets import resolve_jianying_audio
from .motion_library import motion_item_starts
from .production_graphics import ProductionError, create_output_folder


SAMPLE_RATE = 48_000
SFX_CACHE_VERSION = 4


@dataclass(frozen=True)
class Cue:
    name: str
    label: str
    seconds: float
    summary: str


CUES: dict[str, Cue] = {
    "whoosh": Cue("whoosh", "转场风声", 0.42, "镜头切换、卡片推入"),
    "tick": Cue("tick", "刻度点按", 0.09, "列表逐条出现、关键词落位"),
    "pop": Cue("pop", "弹出", 0.16, "小元素、标签、下三分"),
    "impact": Cue("impact", "重音", 0.85, "章节卡、金句、开场定调"),
    "riser": Cue("riser", "上升音", 1.20, "进入结论前的蓄力"),
    "swell": Cue("swell", "渐强", 0.75, "画面揭示、数字出现"),
    "keyboard": Cue("keyboard", "键盘", 0.55, "需求表、清单、文字型信息"),
    "swipe": Cue("swipe", "擦除", 0.30, "遮罩转场、左右切换"),
}


def _seed(name: str) -> np.random.Generator:
    seed = int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big")
    return np.random.default_rng(seed)


def _envelope(t: np.ndarray, attack: float, decay: float) -> np.ndarray:
    rise = np.clip(t / max(attack, 1e-4), 0, 1)
    fall = np.exp(-t / max(decay, 1e-4))
    return rise * fall


def _band_noise(rng: np.random.Generator, seconds: float, low: float, high: float) -> np.ndarray:
    """Noise shaped by a smooth spectral window between two frequencies."""
    count = int(seconds * SAMPLE_RATE)
    spectrum = np.fft.rfft(rng.normal(0, 1, count))
    freqs = np.fft.rfftfreq(count, 1 / SAMPLE_RATE)
    weight = np.exp(-((freqs - (low + high) / 2) ** 2) / (2 * ((high - low) / 2.5) ** 2))
    return np.fft.irfft(spectrum * weight, count)


def _render(name: str) -> np.ndarray:
    cue = CUES[name]
    seconds = cue.seconds
    t = np.linspace(0, seconds, int(seconds * SAMPLE_RATE), endpoint=False)
    rng = _seed(name)
    if name == "whoosh":
        sweep = 500 + 2600 * (t / seconds) ** 0.7
        tone = np.sin(2 * np.pi * np.cumsum(sweep) / SAMPLE_RATE) * 0.35
        air = _band_noise(rng, seconds, 800, 5000) * 0.5
        wave_ = (tone + air) * _envelope(t, 0.06, seconds * 0.42)
    elif name == "tick":
        click = _band_noise(rng, seconds, 2500, 7000) * _envelope(t, 0.002, 0.012)
        tone = np.sin(2 * np.pi * 2400 * t) * _envelope(t, 0.001, 0.010) * 0.5
        wave_ = click * 0.8 + tone
    elif name == "pop":
        freq = 420 * np.exp(-t / 0.05) + 120
        wave_ = np.sin(2 * np.pi * np.cumsum(freq) / SAMPLE_RATE) * _envelope(t, 0.004, 0.05)
    elif name == "impact":
        thump = np.sin(2 * np.pi * (52 * np.exp(-t / 0.25) + 34) * t) * _envelope(t, 0.004, 0.30)
        body = _band_noise(rng, seconds, 60, 400) * _envelope(t, 0.002, 0.10) * 0.55
        wave_ = thump + body
    elif name == "riser":
        freq = 180 + 2400 * (t / seconds) ** 2.2
        tone = np.sin(2 * np.pi * np.cumsum(freq) / SAMPLE_RATE) * 0.35
        air = _band_noise(rng, seconds, 1200, 9000) * 0.45
        wave_ = (tone + air) * (t / seconds) ** 1.6
    elif name == "swell":
        air = _band_noise(rng, seconds, 300, 6000)
        wave_ = air * (t / seconds) ** 2.0 * 0.9
    elif name == "keyboard":
        wave_ = np.zeros_like(t)
        for start in (0.02, 0.14, 0.27, 0.39):
            offset = int(start * SAMPLE_RATE)
            span = min(int(0.035 * SAMPLE_RATE), len(t) - offset)
            if span <= 0:
                continue
            local_t = t[:span]
            click = _band_noise(rng, 0.035, 1800, 6500)[:span] * _envelope(local_t, 0.001, 0.008)
            wave_[offset:offset + span] += click[:span]
    elif name == "swipe":
        air = _band_noise(rng, seconds, 600, 4500)
        wave_ = air * (np.sin(np.pi * t / seconds) ** 1.4)
    else:  # pragma: no cover - guarded by callers
        raise ProductionError(f"未知音效 {name!r}")
    peak = float(np.max(np.abs(wave_))) or 1.0
    return wave_ / peak * 0.85


def synthesize_sfx(
    settings: Settings, name: str, *, gain_db: float = 0.0, seconds: float | None = None,
) -> dict[str, Any]:
    """Render one cue to a cached WAV file and return its path and length."""
    cue = CUES.get(name)
    if cue is None:
        raise ProductionError(f"未知音效 {name!r}。可用：{', '.join(CUES)}")
    if not math.isfinite(gain_db) or abs(gain_db) > 24:
        raise ProductionError("gain_db 需在 -24 到 24 之间")
    length = cue.seconds if seconds is None else seconds
    if not 0.05 <= length <= 12:
        raise ProductionError("音效时长需在 0.05–12 秒")
    folder = create_output_folder(settings, "sound_design") / f"{name}-v{SFX_CACHE_VERSION}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}-{length:.2f}s-{gain_db:+.1f}db.wav"
    if not path.is_file():
        samples = _render(name)
        wanted = int(length * SAMPLE_RATE)
        if wanted <= len(samples):
            samples = samples[:wanted]
        else:  # shorter recipes stretch with a short tail so the length matches
            samples = np.concatenate([samples, np.zeros(wanted - len(samples))])
        samples = samples * (10 ** (gain_db / 20))
        pcm = np.clip(samples, -1, 1)
        stereo = np.repeat(pcm[:, None], 2, axis=1)
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes((stereo * 32767).astype("<i2").tobytes())
    return {
        "name": name, "label": cue.label, "path": str(path),
        "seconds": length, "gain_db": gain_db,
    }


def list_sfx() -> dict[str, Any]:
    return {"cues": [
        {"id": cue.name, "label": cue.label, "seconds": cue.seconds, "summary": cue.summary}
        for cue in CUES.values()
    ]}


DEFAULT_MAP: dict[str, str] = {
    "transition": "whoosh",
    "chapter": "impact",
    "quote": "impact",
    "hook": "swell",
    "list": "tick",
    "checklist": "tick",
    "steps": "tick",
    "compare": "swipe",
    "number": "riser",
    "end": "swell",
}


def plan_sound(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn edit events into cue placements.

    Each event is ``{kind, at, ...}``; ``kind`` matches a recipe or the keyword
    ``transition``. A cue can be overridden per event with ``sfx`` (name, or
    ``False`` to stay silent).
    """
    placements: list[dict[str, Any]] = []
    for event in events:
        kind = str(event.get("kind") or "")
        requested = event.get("sfx", "")
        if requested is False:
            continue
        name = requested if isinstance(requested, str) and requested else DEFAULT_MAP.get(kind, "")
        if not name:
            continue
        if name not in CUES:
            raise ProductionError(f"事件 {kind!r} 指定了未知音效 {name!r}")
        at = event.get("at")
        if not isinstance(at, (int, float)) or at < 0:
            raise ProductionError(f"事件 {kind!r} 需要非负的 at 秒数")
        placements.append({
            "name": name, "at": round(float(at), 3),
            "gain_db": float(event.get("gain_db", -6.0)),
            "kind": kind,
        })
    return placements


def item_accent_tracks(settings: Settings, recipe: str, data: dict, first: int, frames: int, fps: int) -> list[dict]:
    """Place one subdued tick on each distinct item entrance inside the card."""
    starts = {round(at * fps) for at in motion_item_starts(recipe, data.get("items", []))}
    starts = sorted(at for at in starts if 0 <= at < frames)
    if not starts:
        return []
    tick = synthesize_sfx(settings, "tick")
    length = int(tick["seconds"] * fps)
    if length < 1:
        return []
    return [{"source": tick["path"], "start_frame": first + at, "frames": min(length, frames - at), "volume": 0.18}
            for at in starts]


def caption_accent_tracks(settings: Settings, groups: list[tuple], fps: int, total_frames: int,
                         occupied: list[int], *, reference_dialogue: bool = False) -> list[dict[str, Any]]:
    """Accent marked phrases, spacing sounds away from edits and card entrances."""
    tracks = []
    tick = None
    last = -3 * fps
    for cues, terms, choice in groups:
        if choice is False or not terms:
            continue
        if choice is not True and not isinstance(choice, dict):
            raise ProductionError("caption_sfx 使用 true、false 或剪映音效的 {draft_path, material_id}")
        for cue in cues:
            start = cue["start_frame"]
            if (not any(term in cue["text"] for term in terms) or start - last < (0.45 if reference_dialogue else 2.5) * fps
                    or any(abs(start - at) < 0.45 * fps for at in occupied)):
                continue
            if isinstance(choice, dict):
                native = resolve_jianying_audio(settings, choice)
                if native["kind"] != "sound_effect":
                    raise ProductionError("caption_sfx 需要剪映音效素材")
                material = {"jianying_audio": choice}
                seconds = min(0.4, native["duration_us"] / 1_000_000)
            else:
                if tick is None:
                    tick = synthesize_sfx(settings, "tick", gain_db=0)
                material = {"source": tick["path"]}
                seconds = tick["seconds"]
            frames = min(int(seconds * fps), total_frames - start, cue["end_frame"] - start)
            if frames > 0:
                tracks.append({**material, "start_frame": start, "frames": frames, "volume": 0.18})
                last = start
                if not reference_dialogue:
                    break
    return tracks


def effect_path(settings: Settings, placement: dict[str, Any]) -> dict[str, Any]:
    return synthesize_sfx(settings, placement["name"], gain_db=placement["gain_db"])
