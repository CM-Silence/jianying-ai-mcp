from __future__ import annotations

import io
import os
import subprocess
import wave
from collections.abc import Iterator
from functools import lru_cache
from importlib.util import find_spec
from itertools import count
from pathlib import Path
from typing import Any


SAMPLE_RATE = 16_000
WINDOW_MS = 300_000
OVERLAP_MS = 1_500
DEFAULT_MODEL = "small"
MODEL_REVISION = "536b0662742c02347bc0e980a01041f333bce120"
MODEL_ENV = "JIANYING_WHISPER_MODEL"
LANGUAGE_ENV = "JIANYING_WHISPER_LANGUAGE"
DOWNLOAD_ENV = "JIANYING_WHISPER_ALLOW_DOWNLOAD"
VAD_PARAMETERS = {"min_silence_duration_ms": 500}


class LocalASRError(RuntimeError):
    def __init__(self, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.evidence = evidence


def configured_model() -> str | None:
    explicit = os.environ.get(MODEL_ENV, "").strip()
    if explicit:
        return explicit
    from .openfilm_runtime import runtime_home
    recorded = runtime_home() / "model-path.txt"
    return recorded.read_text(encoding="utf-8").strip() if recorded.is_file() else None


def local_asr_status() -> dict[str, Any]:
    return {
        "installed": find_spec("faster_whisper") is not None,
        "model": configured_model(),
        "allow_model_download": os.environ.get(DOWNLOAD_ENV) == "1",
    }


def model_options() -> dict[str, Any]:
    model = configured_model() or DEFAULT_MODEL
    path = Path(model).expanduser()
    if path.is_dir():
        model = str(path.resolve())
    revision = []
    for name in ("model.bin", "tokenizer.json", "config.json", "preprocessor_config.json"):
        try:
            stat = (path / name).stat()
        except OSError:
            continue
        revision.append((name, stat.st_size, stat.st_mtime_ns))
    return {
        "model": model,
        "model_revision": tuple(revision),
        "language": os.environ.get(LANGUAGE_ENV, "").strip() or None,
        "allow_download": os.environ.get(DOWNLOAD_ENV) == "1",
    }


@lru_cache(maxsize=1)
def _load_model(model: str, model_revision: tuple, allow_download: bool) -> Any:
    from faster_whisper import WhisperModel
    from faster_whisper.utils import download_model

    path = Path(model).expanduser()
    if not path.is_dir():
        if path.is_absolute() or model.startswith((".", "~")) or "\\" in model:
            raise LocalASRError(f"本地 Whisper 模型目录不存在: {path}")
        try:
            path = Path(download_model(model, local_files_only=not allow_download))
        except Exception as exc:
            hint = "请检查模型名称和下载网络" if allow_download else (
                f"请设置 {MODEL_ENV} 指向完整的本地模型目录，或用 {DOWNLOAD_ENV}=1 允许下载权重"
            )
            raise LocalASRError(f"无法读取本地 Whisper 模型 {model}。{hint}。") from exc

    # faster-whisper falls back to downloading a tokenizer even for a local model.
    if not (path / "tokenizer.json").is_file():
        raise LocalASRError(f"本地 Whisper 模型缺少 tokenizer.json: {path}")
    try:
        return WhisperModel(str(path), device="cpu", compute_type="int8", local_files_only=True)
    except Exception as exc:
        raise LocalASRError(f"无法加载本地 Whisper 模型 {model}: {str(exc)[:250]}") from exc


def _audio_windows(duration_ms: int | None) -> Iterator[tuple[int, int, int, int]]:
    for start in count(0, WINDOW_MS):
        if duration_ms is not None and start >= duration_ms:
            return
        end = start + WINDOW_MS if duration_ms is None else min(start + WINDOW_MS, duration_ms)
        decode_end = end + OVERLAP_MS if duration_ms is None else min(end + OVERLAP_MS, duration_ms)
        yield start, end, max(0, start - OVERLAP_MS), decode_end


def _decode_audio(video: Path, ffmpeg: Path, start_ms: int, end_ms: int):
    import numpy as np

    try:
        result = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", f"{start_ms / 1000:.3f}",
             "-protocol_whitelist", "file", "-i", str(video), "-map", "0:a:0",
             "-t", f"{(end_ms - start_ms) / 1000:.3f}", "-af", "aresample=async=1:first_pts=0",
             "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", "-f", "wav", "pipe:1"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LocalASRError(f"无法为本地转写解码音轨: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[-300:]
        raise LocalASRError(f"无法为本地转写解码音轨: {detail}")
    try:
        with wave.open(io.BytesIO(result.stdout)) as audio:
            pcm = audio.readframes(audio.getnframes())
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    except (wave.Error, EOFError, ValueError) as exc:
        raise LocalASRError("FFmpeg 未返回可读的 PCM WAV 音轨") from exc
    samples /= 32768.0
    return samples


def transcribe_local(
    video: Path, ffmpeg: Path, options: dict[str, Any], duration_ms: int | None = None,
) -> dict[str, Any]:
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
    except (ImportError, OSError) as exc:
        raise LocalASRError("无法导入本地语音识别依赖，请检查 jianying-ai-mcp[local-asr] 的安装") from exc

    evidence = {
        "vad": "silero",
        "audio_seconds": 0.0,
        "vad_seconds": 0.0,
        "speech_segments": 0,
        "accepted_segments": 0,
        "flagged_segments": 0,
        "language": None,
        "complete": False,
    }
    transcript = []
    languages = set()
    last_speech_end = -1.0
    model = None
    vad_options = VadOptions(**VAD_PARAMETERS)
    try:
        for start, end, decode_start, decode_end in _audio_windows(duration_ms):
            samples = _decode_audio(video, ffmpeg, decode_start, decode_end)
            decoded_end = decode_start + len(samples) * 1000 / SAMPLE_RATE
            owned_end = min(end, decoded_end)
            if owned_end <= start:
                break
            evidence["audio_seconds"] = round(evidence["audio_seconds"] + (owned_end - start) / 1000, 3)
            speech = get_speech_timestamps(samples, vad_options)
            has_owned_speech = False
            for item in speech:
                speech_start = max(start, decode_start + item["start"] * 1000 / SAMPLE_RATE)
                speech_end = min(owned_end, decode_start + item["end"] * 1000 / SAMPLE_RATE)
                if speech_end <= speech_start:
                    continue
                has_owned_speech = True
                if speech_start > last_speech_end:
                    evidence["speech_segments"] += 1
                added = max(0.0, speech_end - max(last_speech_end, speech_start))
                evidence["vad_seconds"] = round(evidence["vad_seconds"] + added / 1000, 3)
                last_speech_end = max(last_speech_end, speech_end)
            if has_owned_speech:
                if model is None:
                    model = _load_model(options["model"], options["model_revision"], options["allow_download"])
                segments, info = model.transcribe(
                    samples, language=options["language"], beam_size=3, vad_filter=True,
                    vad_parameters=VAD_PARAMETERS, condition_on_previous_text=False,
                )
                languages.add(info.language)
                for segment in segments:
                    text = segment.text.strip()
                    segment_start = decode_start + round(segment.start * 1000)
                    segment_end = min(round(decoded_end), decode_start + round(segment.end * 1000))
                    midpoint = (segment_start + segment_end) / 2
                    if not text or not start <= midpoint < owned_end:
                        continue
                    if transcript and segment_start < transcript[-1]["end_ms"]:
                        # Adjacent decodes can disagree about the boundary of a cue.
                        if text == transcript[-1]["text"]:
                            continue
                        segment_start = transcript[-1]["end_ms"]
                    if segment_end <= segment_start:
                        continue
                    no_speech = round(float(segment.no_speech_prob), 3)
                    logprob = round(float(segment.avg_logprob), 3)
                    compression = round(float(segment.compression_ratio), 3)
                    low_confidence = no_speech > 0.6 or logprob < -1 or compression > 2.4
                    evidence["flagged_segments" if low_confidence else "accepted_segments"] += 1
                    transcript.append({
                        "text": text, "start_ms": segment_start, "end_ms": segment_end,
                        "quality": "review" if low_confidence else "accepted",
                        "no_speech_prob": no_speech, "avg_logprob": logprob, "compression_ratio": compression,
                    })
            if owned_end < end:
                break
    except LocalASRError as exc:
        raise LocalASRError(str(exc), evidence) from exc
    except Exception as exc:
        raise LocalASRError(f"本地 Whisper 转写失败: {str(exc)[:250]}", evidence) from exc

    evidence["complete"] = True
    if languages:
        evidence["language"] = next(iter(languages)) if len(languages) == 1 else "mixed"
    status = "available" if transcript else "no_transcript" if evidence["speech_segments"] else "no_speech_detected"
    return {"segments": transcript, "status": status, "evidence": evidence}
