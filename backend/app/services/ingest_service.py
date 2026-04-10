import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Select, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.db.models import ErrorLog, FeedState, Folder, Job, RedditMediaHistory, Video
from app.services.category_blacklist import extract_download_root, extract_path_badges, normalize_category_name
from app.services.ffmpeg_service import FFmpegService
from app.services.file_utils import is_video_path, normalize_path, path_exists, quick_fingerprint
from app.services.job_service import JobService


logger = logging.getLogger("ingest")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IngestService:
    def __init__(
        self,
        session_factory: sessionmaker,
        ffmpeg_service: FFmpegService,
        job_service: JobService,
        settings: Settings | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._ffmpeg = ffmpeg_service
        self._jobs = job_service
        self._settings = settings or get_settings()

    def enqueue_ingest(self, source_path: str, folder_id: int | None, reason: str = "watcher") -> int:
        normalized = normalize_path(source_path)
        with self._session_factory() as session:
            job = self._jobs.enqueue(
                session=session,
                job_type="ingest",
                payload={"source_path": normalized, "folder_id": folder_id, "reason": reason},
            )
            return job.id

    def enqueue_reprocess(self, video_id: int) -> int:
        with self._session_factory() as session:
            job = self._jobs.enqueue(
                session=session,
                job_type="reprocess",
                video_id=video_id,
                payload={"video_id": video_id},
            )
            return job.id

    def process_source_now(
        self,
        source_path: str,
        folder_id: int | None = None,
        force: bool = True,
        delete_source_after_transcode: bool | None = None,
    ) -> int | None:
        normalized = normalize_path(source_path)
        with self._session_factory() as session:
            self._process_path(
                session=session,
                source_path=normalized,
                folder_id=folder_id,
                force=force,
                delete_source_after_transcode=delete_source_after_transcode,
            )
            video = session.scalar(select(Video).where(Video.original_path == normalized))
            return video.id if video else None

    def handle_job(self, session: Session, job: Job) -> None:
        payload = self._jobs.decode_payload(job.payload)
        if job.type == "ingest":
            source_path = payload.get("source_path")
            folder_id = payload.get("folder_id")
            if not source_path:
                raise ValueError("ingest job missing source_path")
            self._process_path(
                session=session,
                source_path=source_path,
                folder_id=folder_id,
                force=False,
                delete_source_after_transcode=None,
            )
            return

        if job.type == "reprocess":
            video_id = payload.get("video_id") or job.video_id
            if not video_id:
                raise ValueError("reprocess job missing video_id")
            self._reprocess_video(session, int(video_id))
            return

        raise ValueError(f"unsupported job type: {job.type}")

    def _reprocess_video(self, session: Session, video_id: int) -> None:
        video = session.get(Video, video_id)
        if not video:
            raise ValueError(f"video {video_id} not found")
        self._process_path(
            session=session,
            source_path=video.original_path,
            folder_id=video.folder_id,
            force=True,
            delete_source_after_transcode=None,
        )

    def _process_path(
        self,
        session: Session,
        source_path: str,
        folder_id: int | None,
        force: bool,
        delete_source_after_transcode: bool | None,
    ) -> None:
        normalized = normalize_path(source_path)
        source = Path(normalized)

        if not source.exists():
            self.mark_removed_by_path(session, normalized)
            return
        if not is_video_path(source):
            return

        stat = source.stat()
        source_size = int(stat.st_size)
        source_mtime = float(stat.st_mtime)
        fingerprint = quick_fingerprint(source)
        video = session.scalar(select(Video).where(Video.original_path == normalized))

        if not video:
            renamed = session.scalar(
                select(Video)
                .where(Video.source_fingerprint == fingerprint, Video.is_deleted.is_(True))
                .order_by(Video.updated_at.desc())
                .limit(1)
            )
            if renamed:
                logger.info("Detected rename/reappear: %s -> %s", renamed.original_path, normalized)
                renamed.original_path = normalized
                renamed.folder_id = folder_id
                renamed.is_deleted = False
                renamed.status = "processing"
                renamed.processing_error = None
                video = renamed
            else:
                duplicate = session.scalar(
                    select(Video)
                    .where(
                        Video.source_fingerprint == fingerprint,
                        Video.is_deleted.is_(False),
                        Video.status == "ready",
                    )
                    .order_by(Video.processed_at.desc())
                    .limit(1)
                )
                if duplicate:
                    dup_video = Video(
                        folder_id=folder_id,
                        original_path=normalized,
                        derived_path=duplicate.derived_path,
                        thumbnail_path=duplicate.thumbnail_path,
                        status="duplicate",
                        duplicate_of_video_id=duplicate.id,
                        source_size=source_size,
                        source_mtime=source_mtime,
                        source_fingerprint=fingerprint,
                        duration_seconds=duplicate.duration_seconds,
                        width=duplicate.width,
                        height=duplicate.height,
                        video_codec=duplicate.video_codec,
                        has_audio=duplicate.has_audio,
                        audio_codec=duplicate.audio_codec,
                        container=duplicate.container,
                        bitrate=duplicate.bitrate,
                        processed_at=duplicate.processed_at,
                        sort_timestamp=duplicate.sort_timestamp,
                    )
                    session.add(dup_video)
                    session.commit()
                    return
                video = Video(
                    folder_id=folder_id,
                    original_path=normalized,
                    status="processing",
                    detected_at=utcnow(),
                )
                session.add(video)
                session.flush()
        else:
            if (
                not force
                and video.status == "ready"
                and video.source_size == source_size
                and video.source_mtime is not None
                and abs(video.source_mtime - source_mtime) < 0.0001
                and path_exists(video.derived_path)
            ):
                video.updated_at = utcnow()
                session.commit()
                return
            video.status = "processing"
            video.is_deleted = False
            video.processing_error = None
            if folder_id is not None:
                video.folder_id = folder_id

        video.source_size = source_size
        video.source_mtime = source_mtime
        video.source_fingerprint = fingerprint
        session.commit()
        session.refresh(video)

        folder = session.get(Folder, folder_id) if folder_id else None
        serve_files = True if not folder else folder.serve_files

        try:
            source_probe = self._ffmpeg.probe(str(source))
            video.duration_seconds = source_probe.duration_seconds
            video.width = source_probe.width
            video.height = source_probe.height
            video.video_codec = source_probe.video_codec
            video.has_audio = source_probe.has_audio
            video.audio_codec = source_probe.audio_codec
            video.container = source_probe.container
            video.bitrate = source_probe.bitrate

            if serve_files:
                derived = self._settings.media_output_dir / f"{video.id}.mp4"
                thumb = self._settings.thumbnail_dir / f"{video.id}.jpg"
                plan = self._ffmpeg.build_plan(source_probe)
                self._ffmpeg.optimize_for_streaming(str(source), str(derived), plan)
                self._ffmpeg.generate_thumbnail(str(derived), str(thumb))
                out_probe = self._ffmpeg.probe(str(derived))
                video.derived_path = str(derived.resolve())
                video.thumbnail_path = str(thumb.resolve())
                video.duration_seconds = out_probe.duration_seconds or video.duration_seconds
                video.width = out_probe.width or video.width
                video.height = out_probe.height or video.height
                video.video_codec = out_probe.video_codec or video.video_codec
                video.has_audio = out_probe.has_audio
                video.audio_codec = out_probe.audio_codec
                video.container = out_probe.container or "mp4"
                video.bitrate = out_probe.bitrate or video.bitrate
                video.status = "ready"
            else:
                video.status = "indexed"

            video.processed_at = utcnow()
            video.sort_timestamp = video.processed_at
            video.processing_error = None
            session.commit()
            should_delete_source = (
                self._settings.delete_source_after_transcode
                if delete_source_after_transcode is None
                else bool(delete_source_after_transcode)
            )
            if serve_files and should_delete_source and video.status == "ready":
                self._delete_original_source_file(video=video)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to process video: %s", normalized)
            self._discard_failed_video(
                session=session,
                video=video,
                source_path=normalized,
                error_message=str(exc),
                remove_source=True,
            )
            session.add(
                ErrorLog(
                    context="ingest",
                    message=f"Failed processing {normalized}",
                    details=str(exc),
                )
            )
            session.commit()
            raise

    def _discard_failed_video(
        self,
        session: Session,
        video: Video,
        source_path: str,
        error_message: str,
        remove_source: bool,
    ) -> None:
        video_id = int(video.id or 0)
        original_path = str(video.original_path or source_path or "")
        derived_path = str(video.derived_path or "") or None
        thumbnail_path = str(video.thumbnail_path or "") or None
        derived_shared = False
        thumbnail_shared = False

        if video_id > 0 and derived_path:
            derived_shared = (
                session.scalar(
                    select(func.count(Video.id)).where(
                        Video.id != video_id,
                        Video.is_deleted.is_(False),
                        Video.derived_path == derived_path,
                    )
                )
                or 0
            ) > 0
        if video_id > 0 and thumbnail_path:
            thumbnail_shared = (
                session.scalar(
                    select(func.count(Video.id)).where(
                        Video.id != video_id,
                        Video.is_deleted.is_(False),
                        Video.thumbnail_path == thumbnail_path,
                    )
                )
                or 0
            ) > 0

        if video_id > 0:
            if not derived_shared:
                self._delete_file_quietly(path=derived_path, context="derived", video_id=video_id)
            if not thumbnail_shared:
                self._delete_file_quietly(path=thumbnail_path, context="thumbnail", video_id=video_id)
            if remove_source:
                self._delete_file_quietly(path=original_path, context="original", video_id=video_id)
        elif remove_source and original_path:
            try:
                Path(original_path).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Failed to delete original file for discarded ingest path=%s error=%s", original_path, exc)

        logger.warning(
            "Discarding failed ingest for video_id=%s path=%s remove_source=%s error=%s",
            video_id or None,
            original_path or source_path,
            remove_source,
            error_message,
        )
        session.delete(video)
        self._prune_deleted_category_dirs([original_path])

    def _delete_original_source_file(self, video: Video) -> None:
        if not video.original_path or not video.derived_path:
            return

        source_path = Path(video.original_path)
        derived_path = Path(video.derived_path)
        if not source_path.exists():
            return

        try:
            if source_path.resolve() == derived_path.resolve():
                logger.warning(
                    "Skip deleting source: source and derived are the same path for video_id=%s (%s)",
                    video.id,
                    source_path,
                )
                return
        except OSError:
            # If resolution fails due to permissions/symlink issues, continue with deletion attempt.
            pass

        try:
            source_path.unlink(missing_ok=True)
            logger.info("Deleted original source after successful transcode: video_id=%s path=%s", video.id, source_path)
        except OSError as exc:
            logger.warning(
                "Could not delete original source after successful transcode: video_id=%s path=%s error=%s",
                video.id,
                source_path,
                exc,
            )

    def mark_removed_by_path(self, session: Session, source_path: str) -> bool:
        normalized = normalize_path(source_path)
        video = session.scalar(
            select(Video).where(Video.original_path == normalized, Video.is_deleted.is_(False)).limit(1)
        )
        if not video:
            return False
        if video.status in {"ready", "duplicate"} and path_exists(video.derived_path):
            logger.info(
                "Source removed but keeping video in catalog because derived stream exists: video_id=%s path=%s",
                video.id,
                normalized,
            )
            return False
        video.is_deleted = True
        video.status = "removed"
        video.updated_at = utcnow()
        session.commit()
        return True

    def remove_from_catalog(self, video_id: int) -> bool:
        with self._session_factory() as session:
            video = session.get(Video, video_id)
            if not video:
                return False
            video.is_deleted = True
            video.status = "removed"
            video.updated_at = utcnow()
            session.commit()
            return True

    def count_category_videos(self, category_name: str) -> int:
        return len(self._collect_category_video_targets(category_name))

    def permanently_delete_video(self, video_id: int) -> bool:
        with self._session_factory() as session:
            video = session.get(Video, video_id)
            if not video:
                return False

            original_path = video.original_path
            derived_path = video.derived_path
            thumbnail_path = video.thumbnail_path

            derived_shared = False
            if derived_path:
                derived_shared = (
                    session.scalar(
                        select(func.count(Video.id)).where(
                            Video.id != video_id,
                            Video.is_deleted.is_(False),
                            Video.derived_path == derived_path,
                        )
                    )
                    or 0
                ) > 0

            thumbnail_shared = False
            if thumbnail_path:
                thumbnail_shared = (
                    session.scalar(
                        select(func.count(Video.id)).where(
                            Video.id != video_id,
                            Video.is_deleted.is_(False),
                            Video.thumbnail_path == thumbnail_path,
                        )
                    )
                    or 0
                ) > 0

            self._delete_file_quietly(path=original_path, context="original", video_id=video_id)
            if not derived_shared:
                self._delete_file_quietly(path=derived_path, context="derived", video_id=video_id)
            if not thumbnail_shared:
                self._delete_file_quietly(path=thumbnail_path, context="thumbnail", video_id=video_id)

            session.delete(video)
            session.commit()
            return True

    def permanently_delete_category(self, category_name: str) -> int:
        targets = self._collect_category_video_targets(category_name)
        return self._permanently_delete_targets(targets)

    def permanently_delete_all_downloaded_videos(self) -> int:
        targets: list[tuple[int, str]] = []
        download_roots: set[Path] = set()

        with self._session_factory() as session:
            rows = session.execute(
                select(Video.id, Video.original_path).where(
                    Video.is_deleted.is_(False),
                    Video.is_favorite.is_(False),
                )
            ).all()

        for video_id, original_path in rows:
            normalized_path = normalize_path(str(original_path or ""))
            if not normalized_path:
                continue
            download_root = extract_download_root(normalized_path, self._settings.reddit_download_dir)
            if download_root is None:
                continue
            targets.append((int(video_id), normalized_path))
            download_roots.add(download_root)

        deleted = self._permanently_delete_targets(targets)
        for download_root in download_roots:
            if self._download_root_has_catalog_videos(download_root):
                continue
            self._purge_directory_contents(download_root)
        return deleted

    def permanently_delete_watched_not_favorited(self, min_watched_age: timedelta | None = None) -> int:
        with self._session_factory() as session:
            stmt = select(Video.id, Video.original_path).where(
                Video.is_deleted.is_(False),
                Video.is_watched.is_(True),
                Video.is_favorite.is_(False),
            )
            if min_watched_age is not None:
                cutoff = utcnow() - min_watched_age
                stmt = stmt.where(
                    Video.watched_at.is_not(None),
                    Video.watched_at <= cutoff,
                )
            rows = session.execute(stmt).all()
        targets = [(int(video_id), str(original_path or "")) for video_id, original_path in rows]
        return self._permanently_delete_targets(targets)

    def reset_library_data(self) -> dict[str, int]:
        managed_roots = [
            self._settings.media_output_dir,
            self._settings.thumbnail_dir,
            self._settings.reddit_download_dir,
            self._settings.channel_hls_dir,
            self._settings.video_vod_dir,
        ]

        with self._session_factory() as session:
            videos = list(
                session.execute(
                    select(
                        Video.original_path,
                        Video.derived_path,
                        Video.thumbnail_path,
                    )
                ).all()
            )
            deleted_videos = len(videos)
            deleted_jobs = int(session.scalar(select(func.count(Job.id))) or 0)
            deleted_error_logs = int(session.scalar(select(func.count(ErrorLog.id))) or 0)
            deleted_feed_states = int(session.scalar(select(func.count(FeedState.id))) or 0)
            deleted_reddit_history = int(session.scalar(select(func.count(RedditMediaHistory.id))) or 0)

            paths_to_delete: set[str] = set()
            for original_path, derived_path, thumbnail_path in videos:
                for raw_path in (original_path, derived_path, thumbnail_path):
                    normalized = normalize_path(str(raw_path or "").strip())
                    if normalized:
                        paths_to_delete.add(normalized)

        deleted_files = self._delete_paths_quietly(paths_to_delete)
        for root_dir in managed_roots:
            deleted_files += self._purge_directory_contents(root_dir)

        with self._session_factory() as session:
            session.execute(delete(Job))
            session.execute(delete(FeedState))
            session.execute(delete(ErrorLog))
            session.execute(delete(RedditMediaHistory))
            session.execute(delete(Video))
            for folder in session.scalars(select(Folder)):
                folder.last_scan_at = None
            session.commit()

        return {
            "deleted_videos": deleted_videos,
            "deleted_jobs": deleted_jobs,
            "deleted_error_logs": deleted_error_logs,
            "deleted_feed_states": deleted_feed_states,
            "deleted_reddit_history": deleted_reddit_history,
            "deleted_files": deleted_files,
        }

    def backfill_missing_audio_metadata(self, limit: int = 200) -> dict[str, int]:
        with self._session_factory() as session:
            videos = list(
                session.scalars(
                    select(Video)
                    .where(
                        Video.is_deleted.is_(False),
                        Video.status.in_(["ready", "duplicate", "indexed"]),
                        Video.has_audio.is_(None),
                    )
                    .order_by(Video.id.asc())
                    .limit(max(1, int(limit)))
                )
            )

            scanned_videos = 0
            updated_videos = 0
            videos_with_audio = 0
            videos_without_audio = 0
            failed_videos = 0

            for video in videos:
                probe_path = self._resolve_probe_path(video)
                if not probe_path:
                    failed_videos += 1
                    continue
                try:
                    probe = self._ffmpeg.probe(probe_path)
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to backfill audio metadata for video_id=%s path=%s", video.id, probe_path)
                    failed_videos += 1
                    continue

                scanned_videos += 1
                updated_videos += 1
                video.has_audio = probe.has_audio
                video.audio_codec = probe.audio_codec
                if probe.has_audio:
                    videos_with_audio += 1
                else:
                    videos_without_audio += 1

            session.commit()

            remaining_videos = (
                session.scalar(
                    select(func.count(Video.id)).where(
                        Video.is_deleted.is_(False),
                        Video.status.in_(["ready", "duplicate", "indexed"]),
                        Video.has_audio.is_(None),
                    )
                )
                or 0
            )

        return {
            "scanned_videos": scanned_videos,
            "updated_videos": updated_videos,
            "videos_with_audio": videos_with_audio,
            "videos_without_audio": videos_without_audio,
            "failed_videos": failed_videos,
            "remaining_videos": int(remaining_videos),
        }

    def _collect_category_video_targets(self, category_name: str) -> list[tuple[int, str]]:
        normalized_target = normalize_category_name(category_name)
        if not normalized_target:
            return []

        targets: list[tuple[int, str]] = []
        with self._session_factory() as session:
            rows = session.execute(select(Video.id, Video.original_path)).all()
            for video_id, original_path in rows:
                category, _ = extract_path_badges(str(original_path or ""))
                if normalize_category_name(category or "") != normalized_target:
                    continue
                targets.append((int(video_id), str(original_path or "")))
        return targets

    def _permanently_delete_targets(self, targets: list[tuple[int, str]]) -> int:
        if not targets:
            return 0

        candidate_ids = [video_id for video_id, _ in targets]
        original_paths = [original_path for _, original_path in targets]
        deleted = 0
        for video_id in candidate_ids:
            if self.permanently_delete_video(video_id):
                deleted += 1

        self._prune_deleted_category_dirs(original_paths)
        return deleted

    @staticmethod
    def _delete_file_quietly(path: str | None, context: str, video_id: int) -> None:
        if not path:
            return
        target = Path(path)
        if not target.exists():
            return
        try:
            target.unlink(missing_ok=True)
            logger.info("Deleted %s file for video_id=%s path=%s", context, video_id, target)
        except OSError as exc:
            logger.warning(
                "Failed to delete %s file for video_id=%s path=%s error=%s",
                context,
                video_id,
                target,
                exc,
            )

    @staticmethod
    def _delete_paths_quietly(paths: set[str]) -> int:
        deleted = 0
        for raw_path in sorted(paths):
            target = Path(raw_path)
            if not target.exists():
                continue
            try:
                target.unlink(missing_ok=True)
                deleted += 1
            except OSError as exc:
                logger.warning("Failed to delete path=%s during library reset error=%s", target, exc)
        return deleted

    def _prune_deleted_category_dirs(self, original_paths: list[str]) -> None:
        for raw_path in original_paths:
            source_path = Path(raw_path)
            try:
                resolved_source = source_path.resolve()
            except OSError:
                resolved_source = source_path

            stop_dir = extract_download_root(str(resolved_source), self._settings.reddit_download_dir)
            if stop_dir is None:
                continue

            try:
                resolved_source.relative_to(stop_dir)
            except ValueError:
                continue

            self._prune_empty_ancestors(resolved_source.parent, stop_dir)

    def _download_root_has_catalog_videos(self, download_root: Path) -> bool:
        with self._session_factory() as session:
            for original_path in session.scalars(select(Video.original_path).where(Video.is_deleted.is_(False))):
                normalized_path = normalize_path(str(original_path or ""))
                if not normalized_path:
                    continue
                current_root = extract_download_root(normalized_path, self._settings.reddit_download_dir)
                if current_root == download_root:
                    return True
        return False

    @staticmethod
    def _prune_empty_ancestors(start_dir: Path, stop_dir: Path) -> None:
        current = start_dir
        while current != stop_dir and current.parent != current:
            if not current.exists() or not current.is_dir():
                current = current.parent
                continue
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    @staticmethod
    def _purge_directory_contents(root_dir: Path) -> int:
        if not root_dir.exists() or not root_dir.is_dir():
            return 0

        try:
            resolved_root = root_dir.resolve()
        except OSError:
            resolved_root = root_dir

        if str(resolved_root) in {resolved_root.anchor, "", "."}:
            logger.warning("Refusing to purge unsafe root directory path=%s", resolved_root)
            return 0

        deleted = 0
        for child in root_dir.iterdir():
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)
                deleted += 1
            except OSError as exc:
                logger.warning("Failed to purge download path=%s error=%s", child, exc)
        return deleted

    @staticmethod
    def _resolve_probe_path(video: Video) -> str | None:
        if path_exists(video.derived_path):
            return str(Path(video.derived_path))
        if path_exists(video.original_path):
            return str(Path(video.original_path))
        return None

    @staticmethod
    def stream_source_for_video(video: Video) -> str | None:
        if video.status not in {"ready", "duplicate"}:
            return None
        if video.status == "duplicate" and video.derived_path:
            return video.derived_path
        return video.derived_path

    def folder_video_stats(self, session: Session, folder_id: int) -> dict[str, int]:
        counts = {
            "video_count": session.scalar(
                select(func.count(Video.id)).where(Video.folder_id == folder_id, Video.is_deleted.is_(False))
            )
            or 0,
            "ready_count": session.scalar(
                select(func.count(Video.id)).where(
                    Video.folder_id == folder_id,
                    Video.status.in_(["ready", "duplicate"]),
                    Video.is_deleted.is_(False),
                )
            )
            or 0,
            "pending_count": session.scalar(
                select(func.count(Video.id)).where(
                    Video.folder_id == folder_id,
                    Video.status.in_(["new", "processing"]),
                    Video.is_deleted.is_(False),
                )
            )
            or 0,
            "error_count": session.scalar(
                select(func.count(Video.id)).where(
                    Video.folder_id == folder_id,
                    Video.status == "error",
                    Video.is_deleted.is_(False),
                )
            )
            or 0,
        }
        return counts

    def list_videos(
        self,
        session: Session,
        page: int,
        page_size: int,
        status: str | None = None,
    ) -> tuple[list[Video], int]:
        stmt: Select[tuple[Video]] = select(Video).where(Video.is_deleted.is_(False))
        count_stmt = select(func.count(Video.id)).where(Video.is_deleted.is_(False))
        if status:
            stmt = stmt.where(Video.status == status)
            count_stmt = count_stmt.where(Video.status == status)

        stmt = stmt.order_by(Video.sort_timestamp.desc(), Video.id.desc())
        total = session.scalar(count_stmt) or 0
        items = list(session.scalars(stmt.offset((page - 1) * page_size).limit(page_size)))
        return items, total
