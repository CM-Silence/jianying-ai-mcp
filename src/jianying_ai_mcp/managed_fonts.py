"""Install pinned OFL fonts and carry the used face with portable projects."""

import hashlib
import shutil
import tempfile
import urllib.request
from pathlib import Path

from .openfilm_runtime import runtime_home


FONT_REVISION = "83c62e30d87daa70216dab6b984d00d20d96d585"
_BASE = f"https://raw.githubusercontent.com/google/fonts/{FONT_REVISION}/ofl"
_FILES = {
    "NotoSansSC-VF.ttf": ("notosanssc/NotoSansSC%5Bwght%5D.ttf", "a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da"),
    "NotoSansSC-OFL.txt": ("notosanssc/OFL.txt", "1c05c68c34f9708415aada51f17e1b0092d2cea709bf4a94cd38114f9e73d7d9"),
    "NotoSerifSC-VF.ttf": ("notoserifsc/NotoSerifSC%5Bwght%5D.ttf", "050080d9255a86808f2945bffac582b31ef32bc36411ce29563b4961670c66f9"),
    "NotoSerifSC-OFL.txt": ("notoserifsc/OFL.txt", "5e0da210fb04058a8c0087985d2d456b931c2579811a49655721d3cf0c36b6d6"),
}


def _hash(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def install_fonts(root: Path) -> None:
    folder = root / "fonts" / FONT_REVISION
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder) as temporary:
        for name, (remote, digest) in _FILES.items():
            dest = folder / name
            if dest.is_file() and _hash(dest) == digest:
                continue
            staged = Path(temporary) / name
            with urllib.request.urlopen(f"{_BASE}/{remote}", timeout=90) as stream, staged.open("wb") as out:
                shutil.copyfileobj(stream, out)
            if _hash(staged) != digest:
                raise RuntimeError(f"字体文件校验失败: {name}")
            staged.replace(dest)


def font_path(serif: bool) -> Path:
    return runtime_home() / "fonts" / FONT_REVISION / ("NotoSerifSC-VF.ttf" if serif else "NotoSansSC-VF.ttf")


def font_face(project: Path, serif: bool, prefix: str = "") -> str:
    font = font_path(serif)
    if not font.is_file():  # Manually configured installations retain system font fallback.
        return ""
    folder = project / "fonts"
    folder.mkdir(exist_ok=True)
    for src in (font, font.with_name(font.name.replace("-VF.ttf", "-OFL.txt"))):
        dest = folder / src.name
        if not dest.exists():
            shutil.copyfile(src, dest)
    family = "Noto Serif SC" if serif else "Noto Sans SC"
    return f'@font-face{{font-family:"{family}";src:url("{prefix}fonts/{font.name}") format("truetype");font-weight:100 900;}}'
