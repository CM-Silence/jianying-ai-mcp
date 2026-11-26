from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .config import Settings
from .media_prepare import run_ffmpeg


_SAPI_COMMAND = """
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Speech
$inputData = [Console]::In.ReadToEnd() | ConvertFrom-Json
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  if ($inputData.list) {
    $voices = @($speaker.GetInstalledVoices() | Where-Object Enabled | ForEach-Object {
      @{name=$_.VoiceInfo.Name; language=$_.VoiceInfo.Culture.Name}
    })
    ConvertTo-Json -InputObject $voices -Compress
  } else {
    $speaker.SelectVoice($inputData.voice)
    $speaker.Rate = $inputData.rate
    $speaker.SetOutputToWaveFile($inputData.output)
    $speaker.Speak($inputData.text)
  }
} finally { $speaker.Dispose() }
"""


def _powershell() -> str:
    executable = shutil.which("powershell.exe") if os.name == "nt" else None
    if not executable:
        raise ValueError("本机 SAPI 旁白需要 Windows PowerShell 和已安装语音。可显式选择 edge 后端。")
    return executable


def _sapi(values: dict[str, Any]) -> str:
    command = base64.b64encode(_SAPI_COMMAND.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
        input=json.dumps(values, ensure_ascii=False).encode("utf-8"), capture_output=True, timeout=90,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace")
        # PowerShell can embed XML-formatted diagnostics. Keep messages short.
        raise RuntimeError(f"Windows 语音合成失败: {detail.strip()[-700:]}")
    return result.stdout.decode("utf-8-sig")


def narration_status() -> dict[str, Any]:
    return {"sapi": os.name == "nt" and bool(shutil.which("powershell.exe")), "edge_installed": importlib.util.find_spec("edge_tts") is not None}


def _edge_module() -> Any:
    try:
        import edge_tts
    except ImportError as exc:
        raise ValueError("edge 后端需要安装 jianying-ai-mcp[online-tts]") from exc
    return edge_tts


def list_narration_voices(backend: str = "sapi", language: str | None = None) -> dict[str, Any]:
    if backend == "sapi":
        voices = json.loads(_sapi({"list": True}))
    elif backend == "edge":
        edge_tts = _edge_module()

        async def fetch() -> list[dict[str, str]]:
            voices = await edge_tts.list_voices()
            return [{"name": row["ShortName"], "language": row["Locale"]} for row in voices]

        voices = asyncio.run(asyncio.wait_for(fetch(), timeout=45))
    else:
        raise ValueError("backend 请选择 sapi 或 edge")
    if language:
        locale = language.casefold()
        voices = [row for row in voices if row["language"].casefold().split("-")[0] == locale or row["language"].casefold() == locale]
    return {"backend": backend, "voices": voices[:60], "total": len(voices)}


def _choose_voice(backend: str, voice: str | None, language: str | None, text: str) -> str:
    if backend == "edge":
        if not voice:
            raise ValueError("edge 请提供具体 voice；可调用 list_narration_voices 查询")
        return voice
    if voice:
        return voice
    locale = language or ("zh" if re.search(r"[\u3400-\u9fff]", text) else "en")
    voices = list_narration_voices("sapi", locale)["voices"]
    if not voices:
        raise ValueError(f"未安装 {locale} 对应的 Windows SAPI 语音；请安装对应语音或指定其他后端")
    return voices[0]["name"]


def _make_wav(settings: Settings, output: Path, *, text: str, backend: str, voice: str, rate: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent, prefix="working-") as temporary:
        folder = Path(temporary)
        raw = folder / ("voice.wav" if backend == "sapi" else "voice.mp3")
        if backend == "sapi":
            _sapi({"output": str(raw), "text": text, "voice": voice, "rate": round(rate / 12)})
        else:
            edge_tts = _edge_module()

            async def synthesize() -> None:
                await edge_tts.Communicate(text, voice, rate=f"{rate:+d}%").save(str(raw))

            asyncio.run(asyncio.wait_for(synthesize(), timeout=90))
        normalized = folder / "normalized.wav"
        run_ffmpeg(settings, ["-i", str(raw), "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(normalized)], timeout=90)
        os.replace(normalized, output)


def synthesize_narration(
    settings: Settings,
    segments: list[str],
    *,
    backend: str = "sapi",
    voice: str | None = None,
    language: str | None = None,
    rate: int = 0,
) -> dict[str, Any]:
    if backend not in {"sapi", "edge"}:
        raise ValueError("backend 请选择 sapi 或 edge")
    if type(rate) is not int or not -50 <= rate <= 100:
        raise ValueError("rate 必须是 -50 到 100 的整数百分比")
    if not 1 <= len(segments) <= 80 or any(not text.strip() or len(text) > 1500 for text in segments):
        raise ValueError("需要 1–80 段非空旁白，每段最多 1500 字符")
    resolved_voice = _choose_voice(backend, voice, language, "".join(segments))
    items = []
    for index, text in enumerate(segments):
        content = text.strip()
        fingerprint = hashlib.sha256(json.dumps([1, backend, resolved_voice, rate, content], ensure_ascii=False).encode("utf-8")).hexdigest()[:24]
        output = settings.work_root / "narration" / fingerprint / "voice.wav"
        cached = output.is_file()
        if not cached:
            try:
                _make_wav(settings, output, text=content, backend=backend, voice=resolved_voice, rate=rate)
            except Exception as exc:
                # Partial work stays usable. Report the failing segment and continue with independent lines.
                items.append({"index": index, "error": str(exc) or type(exc).__name__})
                continue
        with wave.open(str(output), "rb") as audio:
            samples, sample_rate = audio.getnframes(), audio.getframerate()
        items.append({"index": index, "audio_path": str(output), "sample_count": samples, "sample_rate": sample_rate,
                      "duration": samples / sample_rate, "duration_ms": round(samples * 1000 / sample_rate), "cache_hit": cached})
    errors = sum("error" in item for item in items)
    return {"success": errors < len(items), "backend": backend, "voice": resolved_voice, "segments": items, "error_count": errors}
