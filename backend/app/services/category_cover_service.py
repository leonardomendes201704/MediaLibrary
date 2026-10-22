from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from app.core.config import Settings, get_settings
from app.services.channel_service import channel_key_for_category
from app.services.ffmpeg_service import FFmpegService


logger = logging.getLogger("category_cover")


class CategoryCoverService:
    """Stores one JPEG cover per category, named by its channel key."""

    def __init__(
        self,
        ffmpeg_service: FFmpegService,
        settings: Settings | None = None,
    ) -> None:
        self._ffmpeg = ffmpeg_service
        self._settings = settings or get_settings()
        self._lock = threading.Lock()

    def cover_path(self, category: str) -> Path | None:
        key = channel_key_for_category(category)
        if not key:
            return None
        return Path(self._settings.category_cover_dir).expanduser() / f"{key}.jpg"

    def cover_version(self, category: str) -> int | None:
        path = self.cover_path(category)
        if path is None:
            return None
        try:
            return int(path.stat().st_mtime)
        except OSError:
            return None

    def save_from_source(self, category: str, source_path: str) -> Path:
        path = self.cover_path(category)
        if path is None:
            raise ValueError("Categoria invalida para capa.")
        # Extract next to the final file and swap it in, so a request never serves a half-written cover.
        temp_path = path.with_name(f"{path.stem}.tmp.jpg")
        with self._lock:
            try:
                self._ffmpeg.extract_first_frame(source_path, str(temp_path))
                os.replace(temp_path, path)
            finally:
                temp_path.unlink(missing_ok=True)
        logger.info("Category cover for %s set from %s", category, source_path)
        return path

    def remove(self, category: str) -> bool:
        path = self.cover_path(category)
        if path is None:
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            logger.warning("Failed to remove category cover %s: %s", path, exc)
            return False
        return True
