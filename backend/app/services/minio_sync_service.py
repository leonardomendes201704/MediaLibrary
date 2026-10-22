import io
import json
import logging
import mimetypes
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from minio import Minio
from minio.error import S3Error
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.db.models import Job, RedditImage, Video
from app.services.category_blacklist import category_name_from_original_path
from app.services.job_service import JobService


logger = logging.getLogger("minio_sync")

MediaRow = Video | RedditImage
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".m4v": "video/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_WAITING_STATUSES = {"new", "pending", "processing"}
_JOB_TYPE = "minio_sync"
_SUBREDDIT_INDEX_KEY = "meta/subreddits.json"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MinioSyncService:
    def __init__(
        self,
        session_factory: sessionmaker,
        job_service: JobService,
        settings: Settings | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._jobs = job_service
        self._settings = settings or get_settings()
        self._client: Minio | None = None
        self._bucket_ready = False
        self._bucket_lock = threading.Lock()
        self._index_lock = threading.Lock()
        self._index_timer: threading.Timer | None = None

    def enabled(self) -> bool:
        return self._settings.minio_enabled

    def ensure_bucket(self) -> None:
        if not self.enabled() or self._bucket_ready:
            return
        with self._bucket_lock:
            if self._bucket_ready:
                return
            client = self._client_or_raise()
            bucket = self._settings.minio_bucket.strip()
            if not client.bucket_exists(bucket):
                region = self._settings.minio_region.strip() or None
                client.make_bucket(bucket, location=region)
                logger.info("Created MinIO bucket %s", bucket)
            self._bucket_ready = True

    def schedule_favorite(self, session: Session, media_type: str, row: MediaRow, is_favorite: bool) -> None:
        if not self.enabled():
            session.commit()
            return
        row.minio_sync_token = int(row.minio_sync_token or 0) + 1
        token = int(row.minio_sync_token)
        if is_favorite:
            row.minio_sync_status = "pending"
            row.minio_error = None
        self._enqueue(session, media_type, int(row.id), "upload" if is_favorite else "delete", token)

    def note_ready_video(self, session: Session, video: Video) -> None:
        if not self.enabled() or not video.is_favorite or video.is_deleted:
            return
        if video.status not in {"ready", "duplicate", "indexed"}:
            return
        path = self._local_file(video, "video")
        if path is None:
            return
        if not self._needs_upload(video, "video", path):
            return
        token = self._ensure_token(video)
        video.minio_sync_status = "pending"
        video.minio_error = None
        self._enqueue(session, "video", int(video.id), "upload", token)

    def backfill(self, session: Session) -> int:
        if not self.enabled():
            return 0
        queued = 0
        queued += self._backfill_rows(session, "video", self._favorite_videos(session))
        queued += self._backfill_rows(session, "image", self._favorite_images(session))
        if session.dirty or session.new:
            session.commit()
        self._schedule_index()
        return queued

    def publish_subreddit_index(self, session: Session) -> int:
        if not self.enabled():
            return 0
        mapping = self._subreddit_mapping(session)
        self.ensure_bucket()
        payload = json.dumps(mapping, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._client_or_raise().put_object(
            self._settings.minio_bucket.strip(),
            _SUBREDDIT_INDEX_KEY,
            io.BytesIO(payload),
            length=len(payload),
            content_type="application/json",
        )
        logger.info("Published subreddit index with %d name(s)", len(mapping))
        return len(mapping)

    def pending_count(self, session: Session) -> int:
        return int(
            session.scalar(
                select(func.count()).select_from(Job).where(Job.type == _JOB_TYPE, Job.status == "pending")
            )
            or 0
        )

    def handle_job(self, session: Session, job: Any) -> None:
        payload = self._jobs.decode_payload(job.payload)
        media_type = str(payload.get("media_type") or "")
        media_id = int(payload.get("media_id") or 0)
        action = str(payload.get("action") or "")
        token = int(payload.get("token") or 0)
        if media_type not in {"video", "image"} or media_id <= 0 or token <= 0:
            raise ValueError("minio_sync job payload is invalid")
        try:
            if action == "upload":
                self._upload(session, media_type, media_id, token)
                return
            if action == "delete":
                self._delete(session, media_type, media_id, token)
                return
            raise ValueError(f"unsupported minio action: {action}")
        except Exception as exc:
            row = self._load(session, media_type, media_id)
            if row is not None and int(row.minio_sync_token or 0) == token:
                row.minio_sync_status = "error"
                row.minio_error = str(exc)[:2000]
            raise

    def _backfill_rows(self, session: Session, media_type: str, rows: list[MediaRow]) -> int:
        queued = 0
        for row in rows:
            path = self._local_file(row, media_type)
            if path is not None and not self._needs_upload(row, media_type, path):
                continue
            if path is None and media_type == "video" and row.status in _WAITING_STATUSES:
                row.minio_sync_status = "pending"
                row.minio_error = "aguardando arquivo processado"
                continue
            if path is None and row.minio_sync_status == "error" and "nao encontrado" in (row.minio_error or ""):
                continue
            token = self._ensure_token(row)
            row.minio_sync_status = "pending"
            row.minio_error = None
            if self._enqueue(session, media_type, int(row.id), "upload", token):
                queued += 1
        return queued

    def _favorite_videos(self, session: Session) -> list[Video]:
        stmt = select(Video).where(
            Video.is_favorite.is_(True),
            Video.is_deleted.is_(False),
            or_(Video.minio_sync_status.is_(None), Video.minio_sync_status != "uploaded"),
        )
        return list(session.scalars(stmt))

    def _favorite_images(self, session: Session) -> list[RedditImage]:
        stmt = select(RedditImage).where(
            RedditImage.is_favorite.is_(True),
            RedditImage.is_deleted.is_(False),
            or_(RedditImage.minio_sync_status.is_(None), RedditImage.minio_sync_status != "uploaded"),
        )
        return list(session.scalars(stmt))

    def _upload(self, session: Session, media_type: str, media_id: int, token: int) -> None:
        row = self._load(session, media_type, media_id)
        if row is None or row.is_deleted or not row.is_favorite or int(row.minio_sync_token or 0) != token:
            return
        path = self._local_file(row, media_type)
        if path is None:
            if media_type == "video" and row.status in _WAITING_STATUSES:
                row.minio_sync_status = "pending"
                row.minio_error = "aguardando arquivo processado"
                return
            raise FileNotFoundError(f"Arquivo local nao encontrado para {media_type} {media_id}")
        size = int(path.stat().st_size)
        key = self._object_key(media_type, row.id, path)
        if not self._needs_upload(row, media_type, path):
            return
        previous_key = row.minio_object_key
        self.ensure_bucket()
        content_type = self._content_type(row, media_type, path)
        self._client_or_raise().fput_object(
            self._settings.minio_bucket.strip(),
            key,
            str(path),
            content_type=content_type,
        )
        session.refresh(row)
        if row.is_deleted or not row.is_favorite or int(row.minio_sync_token or 0) != token:
            self._remove_quiet(key)
            return
        if previous_key and previous_key != key:
            self._remove_quiet(previous_key)
        row.minio_object_key = key
        row.minio_sync_status = "uploaded"
        row.minio_source_size = size
        row.minio_synced_at = utcnow()
        row.minio_error = None
        logger.info("Uploaded %s %s to %s (%s bytes)", media_type, media_id, key, size)
        self._schedule_index()

    def _delete(self, session: Session, media_type: str, media_id: int, token: int) -> None:
        row = self._load(session, media_type, media_id)
        if row is None or int(row.minio_sync_token or 0) != token or row.is_favorite:
            return
        key = row.minio_object_key
        if key:
            self.ensure_bucket()
            self._remove_quiet(key)
        row.minio_object_key = None
        row.minio_source_size = None
        row.minio_sync_status = None
        row.minio_error = None
        row.minio_synced_at = None
        logger.info("Removed MinIO object for %s %s", media_type, media_id)
        self._schedule_index()

    def _enqueue(self, session: Session, media_type: str, media_id: int, action: str, token: int) -> bool:
        payload = {
            "action": action,
            "media_id": media_id,
            "media_type": media_type,
            "token": token,
        }
        payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        existing = session.scalar(
            select(Job).where(
                Job.type == _JOB_TYPE,
                Job.status.in_(["pending", "running"]),
                Job.payload == payload_bytes,
            )
        )
        self._jobs.enqueue(
            session=session,
            job_type=_JOB_TYPE,
            payload=payload,
            video_id=media_id if media_type == "video" else None,
            max_attempts=5,
        )
        if session.dirty or session.new:
            session.commit()
        return existing is None

    def _needs_upload(self, row: MediaRow, media_type: str, path: Path) -> bool:
        size = int(path.stat().st_size)
        key = self._object_key(media_type, row.id, path)
        return not (
            row.minio_sync_status == "uploaded"
            and row.minio_object_key == key
            and row.minio_source_size == size
        )

    def _ensure_token(self, row: MediaRow) -> int:
        token = int(row.minio_sync_token or 0)
        if token <= 0:
            token = 1
            row.minio_sync_token = token
        return token

    def _load(self, session: Session, media_type: str, media_id: int) -> MediaRow | None:
        model = Video if media_type == "video" else RedditImage
        return session.get(model, media_id)

    def _local_file(self, row: MediaRow, media_type: str) -> Path | None:
        candidates: list[str | None]
        if media_type == "video":
            video = row if isinstance(row, Video) else None
            if video is None:
                return None
            candidates = [video.derived_path, video.original_path]
        else:
            image = row if isinstance(row, RedditImage) else None
            if image is None:
                return None
            candidates = [image.file_path]
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate)
            if path.is_file():
                return path
        return None

    def _object_key(self, media_type: str, media_id: int, path: Path) -> str:
        prefix = "videos" if media_type == "video" else "images"
        suffix = path.suffix.lower()[:10]
        name = _SAFE_NAME.sub("_", path.name).strip("._") or f"file{suffix}"
        if len(name) > 180:
            name = f"{name[:160]}{suffix}"
        return f"{prefix}/{media_id}/{name}"

    def _content_type(self, row: MediaRow, media_type: str, path: Path) -> str:
        if media_type == "image" and isinstance(row, RedditImage) and row.mime_type:
            return row.mime_type
        mapped = _CONTENT_TYPES.get(path.suffix.lower())
        if mapped:
            return mapped
        guessed, _ = mimetypes.guess_type(path.name)
        return guessed or "application/octet-stream"

    def _schedule_index(self) -> None:
        if not self.enabled():
            return
        with self._index_lock:
            if self._index_timer is not None:
                self._index_timer.cancel()
            timer = threading.Timer(5.0, self._flush_index)
            timer.daemon = True
            self._index_timer = timer
            timer.start()

    def _flush_index(self) -> None:
        session = self._session_factory()
        try:
            self.publish_subreddit_index(session)
        except Exception:
            logger.exception("Failed to publish subreddit index")
        finally:
            session.close()

    def _subreddit_mapping(self, session: Session) -> dict[str, str]:
        mapping: dict[str, str] = {}
        videos = session.scalars(
            select(Video).where(
                Video.is_favorite.is_(True),
                Video.is_deleted.is_(False),
                Video.minio_sync_status == "uploaded",
                Video.minio_object_key.is_not(None),
            )
        )
        for video in videos:
            label = self._subreddit_label(video)
            if label and video.minio_object_key:
                mapping[video.minio_object_key] = label
        images = session.scalars(
            select(RedditImage).where(
                RedditImage.is_favorite.is_(True),
                RedditImage.is_deleted.is_(False),
                RedditImage.minio_sync_status == "uploaded",
                RedditImage.minio_object_key.is_not(None),
            )
        )
        for image in images:
            label = self._subreddit_label(image)
            if label and image.minio_object_key:
                mapping[image.minio_object_key] = label
        return mapping

    def _subreddit_label(self, row: MediaRow) -> str:
        raw = ""
        if isinstance(row, RedditImage):
            raw = (row.category_name or "").strip()
            if not raw:
                raw = category_name_from_original_path(row.original_path or "") or ""
        elif isinstance(row, Video):
            raw = category_name_from_original_path(row.original_path or "") or ""
        cleaned = " ".join(raw.split())
        return cleaned[:80]

    def _client_or_raise(self) -> Minio:
        if self._client is not None:
            return self._client
        endpoint = self._settings.minio_endpoint.strip()
        endpoint = endpoint.removeprefix("https://").removeprefix("http://").strip("/")
        self._client = Minio(
            endpoint,
            access_key=self._settings.minio_access_key.strip(),
            secret_key=self._settings.minio_secret_key.strip(),
            secure=bool(self._settings.minio_secure),
            region=self._settings.minio_region.strip() or None,
        )
        return self._client

    def _remove_quiet(self, key: str) -> None:
        try:
            self._client_or_raise().remove_object(self._settings.minio_bucket.strip(), key)
        except S3Error as exc:
            if exc.code not in {"NoSuchKey", "NoSuchBucket"}:
                raise
