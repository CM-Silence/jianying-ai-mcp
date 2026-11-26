"""Local OpenFilm runtime: discovery, first-run provisioning and browser choice.

The installer provisions a writable, lock-specific directory. Discovery also
supports explicitly configured binaries and older source checkouts. Import and
status checks keep working offline.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Callable, Iterable


SPEC = Path(__file__).with_name("runtime_spec")
OPENFILM_VERSION = json.loads((SPEC / "package.json").read_text(encoding="utf-8"))["dependencies"]["openfilm"]
OPENFILM_PACKAGE = f"openfilm@{OPENFILM_VERSION}"
RUNTIME_ENV = "JIANYING_OPENFILM_RUNTIME"
NODE_ENV = "JIANYING_NODE"
PROVISION_TIMEOUT_SECONDS = 900
_BROWSER_CHANNELS = ("chrome", "msedge", "chromium")
_WINDOWS_BROWSERS = {
    "chrome": (
        "Google/Chrome/Application/chrome.exe",
        "Google/Chrome Beta/Application/chrome.exe",
    ),
    "msedge": ("Microsoft/Edge/Application/msedge.exe",),
    "chromium": ("Chromium/Application/chrome.exe",),
}


class OpenFilmRuntimeError(RuntimeError):
    """Raised when the local OpenFilm runtime cannot be found or installed."""


def _dedupe(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    unique = []
    for path in paths:
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def runtime_roots() -> list[Path]:
    """Candidate runtime folders, most specific first."""
    candidates: list[Path] = []
    override = os.environ.get(RUNTIME_ENV, "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(managed_openfilm())
    # Source checkout: <repo>/runtime/openfilm (parents: package -> src -> repo root).
    candidates.append(Path(__file__).resolve().parents[2] / "runtime" / "openfilm")
    # Installed distribution: <prefix>/share/jianying-ai-mcp/openfilm
    candidates.append(Path(sys.prefix) / "share" / "jianying-ai-mcp" / "openfilm")
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        candidates.append(Path(local_app_data) / "jianying-ai-mcp" / "openfilm")
    return _dedupe(candidates)


def install_root() -> Path:
    """Folder used when the runtime has to be installed for the first time."""
    return runtime_roots()[0]


def runtime_home() -> Path:
    if override := os.environ.get("JIANYING_RUNTIME_HOME"):
        return Path(override).expanduser().resolve()
    prefix = Path(sys.prefix)
    if prefix.parent.name == "runtime" and (prefix.parent / "installed/mcp-stdio.json").is_file():
        return prefix.parent / "installed"
    location = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if location:
        return Path(location).resolve() / "jianying-ai-mcp"
    if os.name == "nt":
        return Path(sys.prefix) / "share/jianying-ai-mcp"
    return Path.home() / ".local/share/jianying-ai-mcp"


def managed_openfilm() -> Path:
    lock_id = hashlib.sha256((SPEC / "package-lock.json").read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:12]
    return runtime_home() / f"openfilm-{OPENFILM_VERSION}-{lock_id}"


def _node_wheel() -> Path | None:
    spec = find_spec("nodejs_wheel")
    return Path(spec.origin).parent if spec and spec.origin else None


def cli_path(root: Path) -> Path | None:
    candidate = root / "node_modules" / "openfilm" / "bin" / "openfilm.mjs"
    return candidate.resolve() if candidate.is_file() else None


def find_node() -> Path | None:
    configured = os.environ.get(NODE_ENV, "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return path.resolve()
    wheel = _node_wheel()
    if wheel:
        binary = wheel / ("node.exe" if os.name == "nt" else "bin/node")
        if binary.is_file():
            return binary
    for name in ("node", "node.exe"):
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    if os.name == "nt":
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(variable, "").strip()
            if base:
                candidate = Path(base) / "nodejs" / "node.exe"
                if candidate.is_file():
                    return candidate.resolve()
    return None


def discover() -> tuple[Path, Path] | None:
    """Return (node, openfilm.mjs) for the first usable local runtime."""
    node = find_node()
    if node is None:
        return None
    for root in runtime_roots():
        cli = cli_path(root)
        if cli is not None:
            return node, cli
    return None


def installed_version(node: Path, cli: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(node), str(cli), "--version"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    version = result.stdout.strip()
    return version if version and result.returncode == 0 else None


def browser_channel(preferred: str | None = None) -> str | None:
    """Pick an installed desktop browser so no Chromium download is required."""
    if preferred:
        return preferred
    configured = os.environ.get("JIANYING_OPENFILM_BROWSER_CHANNEL", "").strip().lower()
    if configured:
        return configured if configured in _BROWSER_CHANNELS else None
    if (runtime_home() / "browsers" / "ready").is_file():
        return None
    if os.name != "nt":
        for channel in _BROWSER_CHANNELS:
            if shutil.which(channel):
                return channel
        return None
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(variable, "").strip()
        if not base:
            continue
        for channel, relatives in _WINDOWS_BROWSERS.items():
            if any((Path(base) / relative).is_file() for relative in relatives):
                return channel
    return None


def _npm() -> Path | None:
    wheel = _node_wheel()
    if wheel:
        cli = wheel / "lib/node_modules/npm/bin/npm-cli.js"
        if cli.is_file():
            return cli
    found = shutil.which("npm") or shutil.which("npm.cmd")
    return Path(found) if found else None


def provision(
    *,
    root: Path | None = None,
    on_log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Install the pinned OpenFilm package into the tool's runtime folder."""
    npm = _npm()
    if npm is None:
        raise OpenFilmRuntimeError(
            "本机没有 npm，无法安装内置 OpenFilm。请安装 Node.js，或把完整的 "
            f"runtime/openfilm 目录放到 {install_root()}。"
        )
    node = find_node()
    if not node:
        raise OpenFilmRuntimeError("未找到 Node.js，请运行项目安装入口。")
    target = (root or install_root()).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if cli_path(target) and installed_version(node, cli_path(target)) == OPENFILM_VERSION:
        return _state(node, cli_path(target))
    if target.exists() and any(target.iterdir()):
        raise OpenFilmRuntimeError(f"运行时目录已有其他内容，请选择空目录: {target}")
    command = ([str(node), str(npm)] if npm.suffix == ".js" else [str(npm)]) + [
        "ci", "--ignore-scripts", "--no-audit", "--no-fund",
    ]
    if on_log is not None:
        on_log(f"安装 OpenFilm 到 {target}")
    try:
        with tempfile.TemporaryDirectory(dir=target.parent, prefix=".npm-") as tmp:
            staged = Path(tmp) / "openfilm"
            staged.mkdir()
            for filename in ("package.json", "package-lock.json"):
                shutil.copyfile(SPEC / filename, staged / filename)
            result = subprocess.run(
                command, cwd=staged, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=PROVISION_TIMEOUT_SECONDS,
                encoding="utf-8", errors="replace", shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode:
                raise OpenFilmRuntimeError(f"npm ci 失败: {(result.stderr or result.stdout)[-1200:]}")
            if not cli_path(staged) or installed_version(node, cli_path(staged)) != OPENFILM_VERSION:
                raise OpenFilmRuntimeError("npm 安装结果与锁定的 OpenFilm 版本不符")
            if target.is_dir() and not any(target.iterdir()):
                target.rmdir()
            try:
                staged.rename(target)
            except OSError:
                if not cli_path(target) or installed_version(node, cli_path(target)) != OPENFILM_VERSION:
                    raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OpenFilmRuntimeError(f"安装内置 OpenFilm 失败: {exc}") from exc
    return _state(node, cli_path(target))


def _state(node: Path, cli: Path) -> dict[str, Any]:
    return {
        "runtime_root": str(cli.parents[3]),
        "executable": str(node),
        "cli_path": str(cli),
        "version": installed_version(node, cli),
        "browser_channel": browser_channel(),
        "installed": True,
    }


def ensure(*, on_log: Callable[[str], None] | None = None, install: bool = False) -> dict[str, Any]:
    """Report the runtime state, installing it first when ``install`` is true."""
    found = discover()
    if found is not None:
        return _state(*found)
    if not install:
        return {
            "runtime_root": str(install_root()),
            "executable": str(node) if (node := find_node()) else None,
            "cli_path": None,
            "version": None,
            "browser_channel": browser_channel(),
            "installed": False,
        }
    return provision(on_log=on_log)


def describe() -> dict[str, Any]:
    """Compact machine-readable state for status tools."""
    state = ensure(install=False)
    state["package"] = OPENFILM_PACKAGE
    state["roots"] = [str(root) for root in runtime_roots()]
    state["npm"] = str(npm) if (npm := _npm()) else None
    return state


def describe_runtime() -> dict[str, Any]:
    return describe()


def install_runtime() -> dict[str, Any]:
    """Provision the runtime, reusing an existing install when it is already there."""
    return ensure(install=True)
