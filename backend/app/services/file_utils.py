import hashlib
import os
from pathlib import Path

from app.core.config import get_settings


def normalize_path(path: str | Path) -> str:
    raw = Path(path).resolve(strict=False)
    return os.path.normcase(str(raw))


def is_video_path(path: str | Path) -> bool:
    ext = Path(path).suffix.lower()
    return ext in set(get_settings().allowed_video_extensions)


def quick_fingerprint(path: str | Path) -> str:
    path_obj = Path(path)
    file_size = path_obj.stat().st_size
    h = hashlib.sha1()
    h.update(str(file_size).encode("utf-8"))
    with path_obj.open("rb") as f:
        head = f.read(1024 * 1024)
        h.update(head)
        if file_size > 1024 * 1024:
            f.seek(max(file_size - 1024 * 1024, 0), os.SEEK_SET)
            tail = f.read(1024 * 1024)
            h.update(tail)
    return h.hexdigest()


def path_exists(path: str | None) -> bool:
    if not path:
        return False
    return Path(path).exists()
