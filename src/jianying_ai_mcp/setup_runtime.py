"""Explicit one-time provisioning. The stdio server never installs dependencies."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

from . import openfilm_runtime
from .config import MCP_CALL_TIMEOUT_SECONDS, Settings
from .desktop_runtime import JIANYING_CLI_VERSION
from .video_local_asr import MODEL_ENV, MODEL_REVISION


def _write(path: Path, text: str) -> None:
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as out:
        out.write(text)
    Path(out.name).replace(path)


def _run(args: list, *, env: dict | None = None, timeout: int = 900, cwd: Path | None = None) -> str:
    result = subprocess.run(args, env=env, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr.strip() + "\n" + result.stdout.strip())[-2000:])
    return result.stdout.strip()


def _model(root: Path) -> Path:
    from faster_whisper import WhisperModel
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    required = ["model.bin", "tokenizer.json", "config.json", "vocabulary.txt"]
    configured = os.environ.get(MODEL_ENV)
    if configured and Path(configured).expanduser().is_dir():
        path = Path(configured).expanduser().resolve()
    else:
        args = dict(repo_id="Systran/faster-whisper-small", revision=MODEL_REVISION, allow_patterns=required)
        try:
            path = Path(snapshot_download(**args, local_files_only=True))
        except LocalEntryNotFoundError:
            path = root / "models" / MODEL_REVISION
        if any(not (path / name).is_file() for name in required):
            path = Path(snapshot_download(**args, local_dir=root / "models" / MODEL_REVISION))
    if any(not (path / name).is_file() for name in required):
        raise RuntimeError(f"本地语音模型不完整: {path}")
    WhisperModel(str(path), device="cpu", compute_type="int8", local_files_only=True)
    _write(root / "model-path.txt", str(path))
    return path


def _browser(node: Path, cli: Path, root: Path) -> Path:
    browsers = root / "browsers"
    browsers.mkdir(exist_ok=True)
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(browsers)}
    playwright = cli.parents[2] / "playwright-core"
    _run([node, playwright / "cli.js", "install", "chromium-headless-shell"], env=env)
    # A downloaded archive alone does not prove that OS libraries allow Chromium to start.
    _run([node, "-e", "require('playwright-core').chromium.launch().then(b=>b.close()).catch(e=>{console.error(e);process.exit(1)})"],
         cwd=cli.parents[3], env=env, timeout=60)
    _write(browsers / "ready", str(playwright))
    return browsers


def _desktop(root: Path) -> Path | None:
    if sys.platform not in {"win32", "darwin"}:
        return None
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("独立桌面运行环境需要 uv，请使用项目安装入口。")
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(root / "desktop")}
    _run([uv, "sync", "--locked", "--no-dev", "--project", openfilm_runtime.SPEC / "desktop",
          "--python", sys.executable], env=env)
    binary = root / "desktop" / ("Scripts/jianying.exe" if os.name == "nt" else "bin/jianying")
    if _run([binary, "--version"], timeout=20) != JIANYING_CLI_VERSION:
        raise RuntimeError("剪映桌面 CLI 版本校验失败")
    return binary


def write_client_configs(root: Path, executable: str, env: dict[str, str]) -> dict[str, str]:
    args = ["-m", "jianying_ai_mcp"]
    server = {"command": executable, "args": args, "env": env}
    local = {"type": "local", "command": [executable, *args], "environment": env}
    configs = {
        "stdio_config": ("mcp-stdio.json", {"mcpServers": {"jianying-ai": server}}),
        "http_config": ("mcp-http.json", {"mcpServers": {"jianying-ai": {"url": "http://127.0.0.1:8000/mcp"}}}),
        "vscode_config": ("vscode-mcp.json", {"servers": {"jianying-ai": {"type": "stdio", **server}}}),
        "opencode_config": ("opencode.json", {"mcp": {"jianying-ai": {**local, "enabled": True, "timeout": MCP_CALL_TIMEOUT_SECONDS * 1000}}}),
        "opencode_v2_config": ("opencode-v2.json", {"mcp": {"servers": {"jianying-ai": {
            **local, "codemode": False, "timeout": {"execution": MCP_CALL_TIMEOUT_SECONDS * 1000}}}}}),
    }
    paths = {}
    for key, (filename, config) in configs.items():
        _write(root / filename, json.dumps(config, ensure_ascii=False, indent=2))
        paths[key] = str(root / filename)
    codex = ['[mcp_servers.jianying-ai]', f'command = {json.dumps(executable, ensure_ascii=False)}',
             f'args = {json.dumps(args)}', 'startup_timeout_sec = 60', f'tool_timeout_sec = {MCP_CALL_TIMEOUT_SECONDS}',
             '', '[mcp_servers.jianying-ai.env]']
    codex.extend(f'{key} = {json.dumps(value, ensure_ascii=False)}' for key, value in env.items())
    path = root / "codex-mcp.toml"
    _write(path, "\n".join(codex) + "\n")
    return {**paths, "codex_config": str(path)}


def setup() -> dict:
    from ffmpeg import init as init_ffmpeg

    root = openfilm_runtime.runtime_home()
    root.mkdir(parents=True, exist_ok=True)
    init_ffmpeg()
    settings = Settings.from_env()
    versions = {}
    for name, executable in (("ffmpeg", settings.ffmpeg_exe), ("ffprobe", settings.ffprobe_exe)):
        if not executable:
            raise RuntimeError(f"未找到 {name}")
        versions[name] = _run([executable, "-version"]).splitlines()[0]
    if not settings.yt_dlp_exe:
        raise RuntimeError("当前 Python 环境缺少 yt-dlp")
    runtime = openfilm_runtime.provision()
    node, cli = Path(runtime["executable"]), Path(runtime["cli_path"])
    browsers = _browser(node, cli, root)
    model = _model(root)
    desktop = _desktop(root)
    from .managed_fonts import install_fonts
    install_fonts(root)
    env = {
        "JIANYING_RUNTIME_HOME": str(root),
        "JIANYING_MCP_WORK_ROOT": str(Path(os.environ.get("JIANYING_MCP_WORK_ROOT") or root / "work").resolve()),
        "JIANYING_OPENFILM_CLI": str(cli), "JIANYING_OPENFILM_EXE": str(node),
        "JIANYING_FFMPEG": str(settings.ffmpeg_exe), "JIANYING_FFPROBE": str(settings.ffprobe_exe),
        "JIANYING_YT_DLP": str(settings.yt_dlp_exe), MODEL_ENV: str(model), "PLAYWRIGHT_BROWSERS_PATH": str(browsers),
    }
    if desktop:
        env["JIANYING_TOOL_EXE"] = str(desktop)
    if settings.jianying_exe and settings.jianying_exe.is_file():
        env["JIANYING_EXE"] = str(settings.jianying_exe)
    for name in ("JIANYING_DRAFT_ROOT", "JIANYING_EXE", "JIANYING_DRAFT_CRYPTO"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return {"ready": True, **write_client_configs(root, sys.executable, env),
            "versions": {**versions, "node": _run([node, "--version"]), "openfilm": runtime["version"]},
            "model": str(model), "desktop_cli": str(desktop) if desktop else None,
            "jianying_exe": str(settings.jianying_exe) if settings.jianying_exe else None}
