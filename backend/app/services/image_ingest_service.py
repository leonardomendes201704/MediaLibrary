from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import RedditImage
from app.services.category_blacklist import category_name_from_original_path
from app.services.file_utils import guess_image_mime_type, is_image_path, normalize_path, quick_fingerprint


logger = logging.getLogger("image_ingest")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ImageIngestService:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def process_source_now(
        self,
        source_path: str,
        *,
        post_id: str | None = None,
        post_url: str | None = None,
        title: str | None = None,
        is_reddit_saved: bool = False,
    ) -> int | None:
        normalized = normalize_path(source_path)
        with self._session_factory() as session:
            image_id = self._process_path(
                session=session,
                source_path=normalized,
                post_id=post_id,
                post_url=post_url,
                title=title,
                is_reddit_saved=is_reddit_saved,
            )
            session.commit()
            return image_id

    def _process_path(
        self,
        session: Session,
        source_path: str,
        *,
        post_id: str | None,
        post_url: str | None,
        title: str | None,
        is_reddit_saved: bool = False,
    ) -> int | None:
        source = Path(source_path)
        if not source.exists():
            return None
        if not is_image_path(source):
            return None

        stat = source.stat()
        source_size = int(stat.st_size)
        source_mtime = float(stat.st_mtime)
        fingerprint = quick_fingerprint(source)
        mime_type = guess_image_mime_type(source)
        category_name = category_name_from_original_path(source_path)
        now = utcnow()

        image = session.scalar(select(RedditImage).where(RedditImage.original_path == source_path))
        if image:
            image.file_path = source_path
            image.mime_type = mime_type
            image.status = "ready"
            image.is_deleted = False
            image.source_size = source_size
            image.source_mtime = source_mtime
            image.source_fingerprint = fingerprint
            image.post_id = post_id
            image.post_url = post_url
            image.title = title
            image.category_name = category_name
            if is_reddit_saved:
                image.is_reddit_saved = True
            image.processed_at = now
            image.sort_timestamp = now
            image.updated_at = now
            session.flush()
            return image.id

        image = RedditImage(
            original_path=source_path,
            file_path=source_path,
            mime_type=mime_type,
            status="ready",
            is_deleted=False,
            is_reddit_saved=bool(is_reddit_saved),
            source_size=source_size,
            source_mtime=source_mtime,
            source_fingerprint=fingerprint,
            post_id=post_id,
            post_url=post_url,
            title=title,
            category_name=category_name,
            detected_at=now,
            processed_at=now,
            sort_timestamp=now,
        )
        session.add(image)
        session.flush()
        logger.info("Cataloged reddit image id=%s path=%s", image.id, source_path)
        return image.id
