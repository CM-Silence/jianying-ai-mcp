from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile


@contextmanager
def atomic_text_writer(path: Path):
    """Publish a complete UTF-8 file beside its temporary sibling, including on Windows."""
    stream = NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False)
    temporary = Path(stream.name)
    try:
        with stream:
            yield stream
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
