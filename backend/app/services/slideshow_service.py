from __future__ import annotations

import logging
import random
import threading
from datetime import datetime
from pathlib import Path

from app.core.config import Settings, get_settings
from app.services.ffmpeg_service import FFmpegService
from app.services.ingest_service import IngestService


logger = logging.getLogger("slideshow")

SLIDESHOW_CATEGORY = "SlideShows"
SLIDESHOW_INTERVAL_SECONDS = 0.5
SLIDESHOW_MAX_DURATION_SECONDS = 300.0
SLIDESHOW_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


class SlideshowService:
    def __init__(
        self,
        ffmpeg_service: FFmpegService,
        ingest_service: IngestService,
        settings: Settings | None = None,
    ) -> None:
        self._ffmpeg = ffmpeg_service
        self._ingest = ingest_service
        self._settings = settings or get_settings()
        self._lock = threading.Lock()

    @property
    def max_images(self) -> int:
        return max(1, int(SLIDESHOW_MAX_DURATION_SECONDS // SLIDESHOW_INTERVAL_SECONDS))

    def list_thumb_images(self) -> list[Path]:
        thumbs_dir = Path(self._settings.thumbnail_dir).expanduser()
        if not thumbs_dir.exists() or not thumbs_dir.is_dir():
            return []
        images: list[Path] = []
        for path in thumbs_dir.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() not in SLIDESHOW_IMAGE_EXTENSIONS:
                continue
            images.append(path)
        return images

    def generate(self) -> dict:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Ja existe um slideshow em geracao. Aguarde terminar.")

        try:
            self._ffmpeg.ensure_available()
            images = self.list_thumb_images()
            if not images:
                raise RuntimeError(
                    f"Nenhuma imagem encontrada em {Path(self._settings.thumbnail_dir).expanduser().resolve()}"
                )

            random.shuffle(images)
            selected = images[: self.max_images]
            # Keep reported duration aligned with the CFR encoder (15 frames @ 30fps = 0.5s).
            output_fps = 30
            frames_per_slide = max(1, int(round(output_fps * SLIDESHOW_INTERVAL_SECONDS)))
            duration_seconds = round(len(selected) * (frames_per_slide / output_fps), 3)
            output_path = self._build_output_path()
            output_path.parent.mkdir(parents=True, exist_ok=True)

            logger.info(
                "Generating slideshow images=%s duration=%.1fs output=%s",
                len(selected),
                duration_seconds,
                output_path,
            )
            self._ffmpeg.create_image_slideshow(
                image_paths=selected,
                output_path=str(output_path),
                interval_seconds=SLIDESHOW_INTERVAL_SECONDS,
            )

            video_id = self._ingest.process_source_now(
                source_path=str(output_path),
                folder_id=None,
                force=True,
                delete_source_after_transcode=True,
            )
            if not video_id:
                raise RuntimeError("Slideshow gerado, mas a ingestao nao retornou video_id.")

            return {
                "success": True,
                "video_id": int(video_id),
                "category": SLIDESHOW_CATEGORY,
                "images_available": len(images),
                "images_used": len(selected),
                "interval_seconds": SLIDESHOW_INTERVAL_SECONDS,
                "duration_seconds": duration_seconds,
                "max_duration_seconds": SLIDESHOW_MAX_DURATION_SECONDS,
                "source_path": str(output_path.resolve()),
                "gallery_url": f"/admin/category?name={SLIDESHOW_CATEGORY}",
            }
        finally:
            self._lock.release()

    def _build_output_path(self) -> Path:
        now = datetime.now().astimezone()
        date_part = now.date().isoformat()
        stamp = now.strftime("%Y%m%d_%H%M%S")
        return (
            Path(self._settings.reddit_download_dir).expanduser()
            / SLIDESHOW_CATEGORY
            / "videos"
            / date_part
            / f"slideshow_{stamp}.mp4"
        )
