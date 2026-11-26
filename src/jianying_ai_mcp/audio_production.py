from __future__ import annotations

import array
import json
import math
import re
import subprocess
import sys
import unicodedata
import wave
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import Settings
from .media_info import parse_media_info
from .production_graphics import create_output_folder
from .video_observer import analyze_video


_PROGRESSIONS = {
    "calm": ((0, 4, 7, 11), (9, 12, 16, 19), (5, 9, 12, 16), (7, 11, 14, 17)),
    "warm": ((0, 4, 7, 12), (5, 9, 12, 16), (9, 12, 16, 19), (7, 11, 14, 19)),
    "pulse": ((0, 3, 7, 12), (8, 12, 15, 19), (3, 7, 10, 15), (10, 14, 17, 22)),
}


def create_music_bed(
    settings: Settings, duration: float, *, preset: str = "calm", bpm: int = 92, root_note: int = 60,
) -> dict[str, Any]:
    if not math.isfinite(duration) or not 1 <= duration <= 600:
        raise ValueError("配乐时长需要 1–600 秒")
    if preset not in _PROGRESSIONS:
        raise ValueError("配乐预设请选择 calm、warm 或 pulse")
    if type(bpm) is not int or not 48 <= bpm <= 180 or type(root_note) is not int or not 48 <= root_note <= 72:
        raise ValueError("bpm 需要 48–180，root_note 需要 48–72 MIDI 音高")
    rate = 24000
    beat = 60 / bpm
    bar_samples = round(4 * beat * rate)
    loop = array.array("f", [0.0]) * (4 * bar_samples)

    def add_note(start: int, midi: int, length: float, gain: float) -> None:
        count = round(length * rate)
        phase_step = math.tau * 440 * 2 ** ((midi - 69) / 12) / rate
        attack, fade = max(1, round(0.022 * rate)), max(1, round(0.16 * rate))
        for i in range(count):
            envelope = min(1.0, i / attack, (count - i) / fade) * math.exp(-2.2 * i / count)
            phase = phase_step * i
            index = (start + i) % len(loop)
            loop[index] += gain * envelope * (math.sin(phase) + 0.12 * math.sin(phase * 2))

    for bar, chord in enumerate(_PROGRESSIONS[preset]):
        start = bar * bar_samples
        add_note(start, root_note + chord[0] - 12, beat * 3.8, 0.065)
        for note in chord[:3]:
            add_note(start, root_note + note, beat * 3.65, 0.027)
        notes = chord if preset == "pulse" else (chord[1], chord[3])
        for step, note in enumerate(notes):
            add_note(start + round((step + 0.5) * 4 * beat * rate / len(notes)), root_note + note + 12, beat * 0.78, 0.032)

    output = create_output_folder(settings, "music") / "bed.wav"
    total = round(duration * rate)
    with wave.open(str(output), "wb") as wav:
        wav.setparams((1, 2, rate, total, "NONE", "not compressed"))
        for start in range(0, total, rate):
            samples = array.array("h")
            for index in range(start, min(start + rate, total)):
                fade = min(1.0, index / (rate * 0.15), (total - 1 - index) / (rate * 0.45))
                samples.append(round(loop[index % len(loop)] * fade * 32767))
            # WAV uses little-endian PCM.
            if sys.byteorder != "little":
                samples.byteswap()
            wav.writeframesraw(samples.tobytes())
    return {"audio_path": str(output), "duration": total / rate, "bpm": bpm, "preset": preset, "sample_rate": rate}


def _loudnorm_stats(ffmpeg: Path, source: Path, arguments: list[str], label: str) -> dict[str, Any]:
    command = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
        "-protocol_whitelist", "file", "-i", str(source), *arguments,
    ]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{label}超过 10 分钟。请先分段导出，再处理各段。") from exc
    except OSError as exc:
        raise ValueError(f"{label}无法启动 FFmpeg: {exc}") from exc
    if result.returncode:
        raise ValueError(f"{label}失败: {result.stderr.strip()[-900:]}")
    matches = re.findall(r'\{\s*"input_i"\s*:.*?\}', result.stderr, re.S)
    if not matches:
        raise ValueError(f"{label}未返回响度数据。请检查 FFmpeg 是否包含 loudnorm 滤镜。")
    return json.loads(matches[-1])


def _input_levels(stats: dict[str, Any]) -> dict[str, float]:
    levels = {
        "integrated_lufs": float(stats["input_i"]),
        "true_peak_dbtp": float(stats["input_tp"]),
        "loudness_range_lu": float(stats["input_lra"]),
    }
    if not all(math.isfinite(value) for value in levels.values()):
        raise ValueError("音轨静音或太短，无法测量综合响度。请先检查对白和混音。")
    return levels


def master_video_audio(
    settings: Settings,
    media_path: str,
    *,
    target_loudness: float = -16.0,
    true_peak: float = -1.5,
    loudness_range: float = 11.0,
) -> dict[str, Any]:
    """Make an MP4 copy with the first audio stream normalized using two-pass EBU R128."""
    if not all(math.isfinite(value) for value in (target_loudness, true_peak, loudness_range)):
        raise ValueError("响度、真峰值和动态范围须为有限数值")
    if not (-70 <= target_loudness <= -5 and -9 <= true_peak <= 0 and 1 <= loudness_range <= 50):
        raise ValueError("target_loudness 需要 -70 至 -5 LUFS，true_peak 需要 -9 至 0 dBTP，loudness_range 需要 1 至 50 LU")
    metadata = parse_media_info(settings, media_path)
    info = metadata["media_info"]
    source = Path(metadata["media_path"])
    if info["type"] != "video" or source.suffix.lower() not in {".mp4", ".m4v"}:
        raise ValueError("请提供已导出的本地 MP4 视频。若是其他封装，请先导出 MP4。")
    if not info["has_audio"]:
        raise ValueError("成片没有音轨。请先在剪映导出含音轨的 MP4。")
    ffmpeg = settings.ffmpeg_exe
    if ffmpeg is None or not ffmpeg.is_file():
        raise ValueError("未找到 FFmpeg。请设置 JIANYING_FFMPEG 或将 ffmpeg 加入 PATH。")

    def render(output: Path, filter_peak: float) -> tuple[dict[str, float], dict[str, float], str | None]:
        target = f"loudnorm=I={target_loudness:g}:TP={filter_peak:g}:LRA={loudness_range:g}"
        measure = ["-map", "0:a:0", "-af", f"{target}:print_format=json", "-f", "null", "-"]
        measured = _loudnorm_stats(ffmpeg, source, measure, "测量源音轨")
        before = _input_levels(measured)
        threshold, offset = float(measured["input_thresh"]), float(measured["target_offset"])
        if not math.isfinite(threshold) or not math.isfinite(offset):
            raise ValueError("源音轨未得到有效的响度校正参数。请先检查是否有可用对白或配乐。")
        second_pass = (
            f"{target}:measured_I={before['integrated_lufs']:g}:measured_TP={before['true_peak_dbtp']:g}:"
            f"measured_LRA={before['loudness_range_lu']:g}:measured_thresh={threshold:g}:"
            f"offset={offset:g}:linear=true:print_format=json"
        )
        applied = _loudnorm_stats(ffmpeg, source, [
            "-map", "0", "-c", "copy", "-c:a:0", "aac", "-b:a:0", "192k", "-ar:a:0", "48000",
            "-filter:a:0", second_pass, "-movflags", "+faststart", str(output),
        ], "生成响度母版")
        after = _input_levels(_loudnorm_stats(ffmpeg, output, measure, "复测母版音轨"))
        return before, after, applied.get("normalization_type")

    # AAC encoding may increase the true peak, so reserve room and allow one measured correction.
    filter_peak = max(-9, true_peak - 0.5)
    folder = create_output_folder(settings, "audio_masters")
    output = folder / "master.mp4"
    attempts = 1
    retry_error = None
    try:
        before, after, mode = render(output, filter_peak)
        if after["true_peak_dbtp"] > true_peak and filter_peak > -9:
            attempts += 1
            alternative = folder / "peak-adjusted.mp4"
            adjusted_peak = max(-9, filter_peak - (after["true_peak_dbtp"] - true_peak) - 0.3)
            try:
                _, adjusted_levels, adjusted_mode = render(alternative, adjusted_peak)
                if adjusted_levels["true_peak_dbtp"] < after["true_peak_dbtp"]:
                    alternative.replace(output)
                    after, mode = adjusted_levels, adjusted_mode
            except (OSError, ValueError) as exc:
                retry_error = str(exc)
            finally:
                alternative.unlink(missing_ok=True)
    except Exception:
        output.unlink(missing_ok=True)
        folder.rmdir()
        raise

    difference = round(after["integrated_lufs"] - target_loudness, 2)
    meets_loudness = abs(difference) <= 0.5
    meets_peak = after["true_peak_dbtp"] <= true_peak
    warning = None
    if not (meets_loudness and meets_peak):
        warning = "母版未同时满足 ±0.5 LU 响度容差和真峰值上限。请检查混音动态，或选用更低的响度目标后复测。"
    return {
        "mp4_path": str(output), "source_path": str(source), "video_stream_copy": True,
        "normalized_audio_stream": "a:0", "normalization_mode": mode, "attempts": attempts,
        "target_loudness_lufs": target_loudness, "target_true_peak_dbtp": true_peak,
        "target_loudness_range_lu": loudness_range, "before": before, "after": after,
        "difference_from_target_lu": difference, "meets_loudness": meets_loudness,
        "meets_true_peak": meets_peak, "master_meets_targets": meets_loudness and meets_peak,
        "warning": warning, "retry_error": retry_error,
    }


def _is_han(character: str) -> bool:
    return unicodedata.name(character, "").startswith("CJK UNIFIED IDEOGRAPH")


@lru_cache(maxsize=1)
def _simplifier():
    from opencc import OpenCC

    return OpenCC("t2s")


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    if any(_is_han(character) for character in value):
        value = _simplifier().convert(value)
    return "".join(character for character in value if character.isalnum())


@lru_cache(maxsize=256)
def _pronunciation(value: str) -> tuple[str, ...]:
    from pypinyin import Style, lazy_pinyin

    return tuple(lazy_pinyin(value, style=Style.TONE3, neutral_tone_with_five=True, errors=list))


def _homophone_span(target: str, actual: str) -> tuple[float, int, int] | None:
    if len(actual) < len(target) or sum(_is_han(character) for character in target) < 4:
        return None
    wanted, observed = _pronunciation(target), _pronunciation(actual)
    if len(wanted) != len(target) or len(observed) != len(actual):
        return None
    best = None
    for start in range(len(actual) - len(target) + 1):
        end = start + len(target)
        if observed[start:end] != wanted:
            continue
        changes = [(left, right) for left, right in zip(target, actual[start:end]) if left != right]
        if not 1 <= len(changes) <= min(2, len(target) // 4):
            continue
        if all(_is_han(left) and _is_han(right) for left, right in changes):
            score = 1 - len(changes) / len(target)
            if best is None or score > best[0]:
                best = score, start, end
    return best


def _fuzzy_span(target: str, actual: str) -> tuple[float, int, int]:
    # Edit distance with a free prefix and suffix in the ASR text. Track the start of each alignment.
    count = len(actual)
    distances = [0] * (count + 1)
    starts = list(range(count + 1))
    for row, wanted in enumerate(target, 1):
        next_distances = [row] + [0] * count
        next_starts = [0] * (count + 1)
        for column, observed in enumerate(actual, 1):
            replacement = distances[column - 1] + (wanted != observed)
            deletion = distances[column] + 1
            insertion = next_distances[column - 1] + 1
            if replacement <= deletion and replacement <= insertion:
                next_distances[column], next_starts[column] = replacement, starts[column - 1]
            elif deletion <= insertion:
                next_distances[column], next_starts[column] = deletion, starts[column]
            else:
                next_distances[column], next_starts[column] = insertion, next_starts[column - 1]
        distances, starts = next_distances, next_starts

    best = 0.0, 0, 0
    for end, (edits, start) in enumerate(zip(distances, starts)):
        if end > start:
            score = 1 - edits / max(len(target), end - start)
            current_rank = score, -abs(len(target) - (end - start))
            best_rank = best[0], -abs(len(target) - (best[2] - best[1]))
            if current_rank > best_rank:
                best = score, start, end
    return best


def _cue_evidence(cues: list[tuple[dict[str, Any], str]], actual: str, start: int, end: int, method: str) -> dict[str, Any]:
    matched = []
    offset = 0
    for cue, value in cues:
        next_offset = offset + len(value)
        if next_offset > start and offset < end:
            matched.append(cue)
        offset = next_offset
    evidence = {
        "start_ms": matched[0]["start_ms"], "end_ms": matched[-1]["end_ms"],
        "time_precision": "cue", "text": " ".join(cue["text"] for cue in matched)[:260],
        "matched_text": actual[start:end][:300], "match_method": method,
    }
    if any(cue.get("quality") == "review" for cue in matched):
        evidence["quality"] = "review"
    return evidence


def _compare_cue(cues: list[dict[str, Any]], target: str) -> tuple[float, dict[str, Any] | None]:
    normalized = [(cue, _normalized(cue["text"])) for cue in cues]
    normalized = [(cue, value) for cue, value in normalized if value]
    best_key = 0, 0.0, False
    best = None
    for index in range(len(normalized)):
        joined = []
        actual = ""
        for cue, value in normalized[index:index + 3]:
            if joined and cue["start_ms"] - joined[-1][0]["end_ms"] > 2000:
                break
            joined.append((cue, value))
            actual += value
            start = actual.find(target)
            if start >= 0:
                score, end, method, priority = 1.0, start + len(target), "exact_text", 3
            elif phonetic := _homophone_span(target, actual):
                score, start, end = phonetic
                method, priority = "homophone", 2
            else:
                score, start, end = _fuzzy_span(target, actual)
                if not score:
                    continue
                method, priority = "approximate_text", 1
            evidence = _cue_evidence(joined, actual, start, end, method)
            confident = evidence.get("quality") != "review"
            if method == "exact_text" and confident:
                return score, evidence
            key = priority, score, confident
            if key > best_key:
                best_key, best = key, evidence
    return best_key[1], best


def verify_audio_dialogue(
    settings: Settings,
    media_path: str,
    expected: list[dict[str, Any]],
    *,
    transcription: str = "local",
) -> dict[str, Any]:
    if transcription not in {"local", "groq", "openai"}:
        raise ValueError("复核转写后端请选择 local、groq 或 openai")
    path = Path(media_path).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError("媒体文件需要本地绝对路径")
    if not 1 <= len(expected) <= 24:
        raise ValueError("expected 需要 1–24 句待核对的文字")
    for row in expected:
        text = row.get("text")
        start, end = row.get("start_ms", 0), row.get("end_ms")
        if not isinstance(text, str) or not _normalized(text) or len(text) > 300:
            raise ValueError("待核对文字需要包含字母或数字，最多 300 字符")
        if type(start) is not int or start < 0 or (end is not None and (type(end) is not int or end <= start)):
            raise ValueError("复核时间需为有效的开始和结束毫秒数")

    analysis = analyze_video(
        settings, str(path), detail="transcript", transcription=transcription, transcript_limit=0, prefer_subtitles=False,
    )
    cues = json.loads(Path(analysis["report_path"]).read_text(encoding="utf-8"))["transcript"]
    status = analysis["transcript_status"]
    results = []
    for index, row in enumerate(expected):
        start, end = row.get("start_ms", 0), row.get("end_ms")
        window = [cue for cue in cues if cue["end_ms"] >= start and (end is None or cue["start_ms"] <= end)]
        score, evidence = _compare_cue(window, _normalized(row["text"]))
        method = evidence["match_method"] if evidence else None
        if status in {"unavailable", "disabled"}:
            result, reason = "unavailable", "transcription_unavailable"
        elif method == "exact_text":
            if evidence.get("quality") == "review":
                result, reason = "review", "asr_low_confidence"
            else:
                result, reason = "matched", None
        elif method == "homophone":
            result, reason = "review", "homophone_only"
        elif score >= 0.65:
            result, reason = "review", "approximate_text"
        else:
            result, reason = "not_matched", "dialogue_not_found"
        results.append({
            "index": index, "status": result, "similarity": round(score, 3), "evidence": evidence,
            "match_method": method, "review_reason": reason, "needs_manual_review": result in {"review", "unavailable"},
        })
    return {
        "transcript_status": status, "transcript_warning": analysis.get("transcript_warning"),
        "transcript_srt_path": analysis.get("transcript_srt_path"), "analysis_id": analysis["analysis_id"],
        "checks": results,
    }
