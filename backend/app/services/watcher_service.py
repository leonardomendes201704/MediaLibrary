import asyncio
import logging
import os
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from watchdog.events import FileSystemEvent, FileSystemEventHandler, FileMovedEvent
from watchdog.observers import Observer
from watchdog.observers.api import ObservedWatch

from app.core.config import Settings, get_settings
from app.db.models import Folder, Video
from app.services.file_utils import is_video_path, normalize_path
from app.services.ingest_service import IngestService


logger = logging.getLogger("watcher")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Candidate:
    path: str
    folder_id: int | None
    last_size: int = -1
    last_mtime: float = -1.0
    stable_checks: int = 0
    first_seen_ts: float = 0.0


class _FileEventHandler(FileSystemEventHandler):
    def __init__(self, event_queue: "queue.Queue[tuple[str, str, str | None]]") -> None:
        self._event_queue = event_queue

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._event_queue.put(("candidate", event.src_path, None))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._event_queue.put(("candidate", event.src_path, None))

    def on_moved(self, event: FileMovedEvent) -> None:
        if not event.is_directory:
            self._event_queue.put(("removed", event.src_path, None))
            self._event_queue.put(("candidate", event.dest_path, None))

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._event_queue.put(("removed", event.src_path, None))


class FolderWatcherService:
    def __init__(
        self,
        session_factory: sessionmaker,
        ingest_service: IngestService,
        settings: Settings | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._ingest = ingest_service
        self._settings = settings or get_settings()

        self._observer: Observer | None = None
        self._event_queue: queue.Queue[tuple[str, str, str | None]] = queue.Queue()
        self._handler = _FileEventHandler(self._event_queue)
        self._watch_map: dict[str, ObservedWatch] = {}
        self._folder_lookup: dict[str, Folder] = {}
        self._candidates: dict[str, Candidate] = {}
        self._candidate_lock = threading.Lock()
        self._loop_task: asyncio.Task | None = None
        self._running = False
        self._paused = False
        self._last_full_scan: float = 0.0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._paused = False
        self._last_full_scan = 0.0
        self._discard_event_queue()
        with self._candidate_lock:
            self._candidates.clear()
        self._observer = Observer()
        self._observer.start()
        self._loop_task = asyncio.create_task(self._loop())
        logger.info("Folder watcher started")

    async def stop(self) -> None:
        self._running = False
        self._paused = False
        if self._loop_task:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None

        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=3)
            self._observer = None
        self._watch_map.clear()
        logger.info("Folder watcher stopped")

    async def pause(self) -> None:
        if not self._running:
            return
        self._paused = True
        logger.info("Folder watcher paused")

    async def resume(self) -> None:
        if not self._running:
            return
        self._paused = False
        logger.info("Folder watcher resumed")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._running and self._paused

    async def _loop(self) -> None:
        while self._running:
            try:
                if self._paused:
                    self._discard_event_queue()
                    await asyncio.sleep(self._settings.watcher_poll_seconds)
                    continue
                await asyncio.to_thread(self._refresh_folders_and_watches)
                await asyncio.to_thread(self._drain_events)
                await asyncio.to_thread(self._evaluate_candidates)
                if (datetime.now(timezone.utc).timestamp() - self._last_full_scan) >= self._settings.full_scan_seconds:
                    await asyncio.to_thread(self._full_scan)
                    self._last_full_scan = datetime.now(timezone.utc).timestamp()
                await asyncio.sleep(self._settings.watcher_poll_seconds)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Watcher loop error")
                await asyncio.sleep(self._settings.watcher_poll_seconds)

    def _refresh_folders_and_watches(self) -> None:
        observer = self._observer
        if observer is None:
            return

        with self._session_factory() as session:
            folders = list(session.scalars(select(Folder).where(Folder.is_active.is_(True))))

        new_lookup = {normalize_path(f.path): f for f in folders if Path(f.path).exists()}
        current_paths = set(self._watch_map.keys())
        desired_paths = set(new_lookup.keys())

        for removed in current_paths - desired_paths:
            watch = self._watch_map.pop(removed, None)
            if watch:
                observer.unschedule(watch)
                logger.info("Stopped watching folder: %s", removed)

        for added in desired_paths - current_paths:
            folder = new_lookup[added]
            recursive = bool(folder.scan_subdirs)
            watch = observer.schedule(self._handler, folder.path, recursive=recursive)
            self._watch_map[added] = watch
            logger.info("Watching folder: %s (recursive=%s)", folder.path, recursive)

        self._folder_lookup = new_lookup

    def _discard_event_queue(self) -> None:
        while True:
            try:
                self._event_queue.get_nowait()
            except queue.Empty:
                break

    def _drain_events(self) -> None:
        removed_paths: list[str] = []
        while True:
            try:
                event_type, raw_path, _extra = self._event_queue.get_nowait()
            except queue.Empty:
                break

            norm = normalize_path(raw_path)
            if event_type == "candidate":
                if is_video_path(norm):
                    self._add_candidate(norm)
            elif event_type == "removed":
                removed_paths.append(norm)

        if removed_paths:
            with self._session_factory() as session:
                for path in removed_paths:
                    self._ingest.mark_removed_by_path(session, path)

    def _add_candidate(self, normalized_path: str) -> None:
        folder_id = self._match_folder_id(normalized_path)
        if folder_id is None:
            return
        with self._candidate_lock:
            item = self._candidates.get(normalized_path)
            if item:
                item.folder_id = folder_id
                return
            self._candidates[normalized_path] = Candidate(
                path=normalized_path,
                folder_id=folder_id,
                first_seen_ts=datetime.now(timezone.utc).timestamp(),
            )

    def _evaluate_candidates(self) -> None:
        to_remove: list[str] = []
        to_enqueue: list[Candidate] = []

        with self._candidate_lock:
            items = list(self._candidates.items())

        for normalized_path, candidate in items:
            source = Path(normalized_path)
            if not source.exists():
                to_remove.append(normalized_path)
                continue
            try:
                stat = source.stat()
            except OSError:
                continue
            if stat.st_size != candidate.last_size or stat.st_mtime != candidate.last_mtime:
                candidate.last_size = int(stat.st_size)
                candidate.last_mtime = float(stat.st_mtime)
                candidate.stable_checks = 0
            else:
                candidate.stable_checks += 1

            age_since_mtime = datetime.now(timezone.utc).timestamp() - float(stat.st_mtime)
            if (
                candidate.stable_checks >= self._settings.stable_checks_required
                and age_since_mtime >= self._settings.file_stable_seconds
            ):
                to_enqueue.append(candidate)
                to_remove.append(normalized_path)

        with self._candidate_lock:
            for path in to_remove:
                self._candidates.pop(path, None)

        for candidate in to_enqueue:
            self._ingest.enqueue_ingest(
                source_path=candidate.path,
                folder_id=candidate.folder_id,
                reason="stable_file",
            )

    def _match_folder_id(self, path: str) -> int | None:
        matched_folder: Folder | None = None
        for root, folder in self._folder_lookup.items():
            if path.startswith(root):
                if matched_folder is None or len(root) > len(normalize_path(matched_folder.path)):
                    matched_folder = folder
        return matched_folder.id if matched_folder else None

    def _full_scan(self) -> None:
        logger.info("Running full scan on active folders")
        with self._session_factory() as session:
            folders = list(session.scalars(select(Folder).where(Folder.is_active.is_(True))))
            for folder in folders:
                folder_root = Path(folder.path)
                if not folder_root.exists():
                    continue

                tracked_videos = list(
                    session.scalars(
                        select(Video).where(
                            Video.folder_id == folder.id,
                            Video.is_deleted.is_(False),
                            Video.status != "removed",
                        )
                    )
                )
                tracked_map = {video.original_path: video for video in tracked_videos}
                seen_files: set[str] = set()
                for file_path in self._iter_video_files(folder_root, recursive=folder.scan_subdirs):
                    normalized = normalize_path(file_path)
                    seen_files.add(normalized)
                    tracked = tracked_map.get(normalized)
                    if tracked is None:
                        self._add_candidate(normalized)
                        continue
                    try:
                        stat = Path(normalized).stat()
                        changed = (
                            tracked.source_size != int(stat.st_size)
                            or tracked.source_mtime is None
                            or abs(tracked.source_mtime - float(stat.st_mtime)) > 0.0001
                        )
                    except OSError:
                        changed = False
                    if changed or tracked.status in {"new", "processing"}:
                        self._add_candidate(normalized)

                for video in tracked_map.values():
                    if video.original_path not in seen_files:
                        if (
                            self._settings.delete_source_after_transcode
                            and video.status in {"ready", "duplicate"}
                            and video.derived_path
                            and Path(video.derived_path).exists()
                        ):
                            continue
                        self._ingest.mark_removed_by_path(session, video.original_path)

                folder.last_scan_at = utcnow()
                session.commit()

    @staticmethod
    def _iter_video_files(folder_path: Path, recursive: bool) -> list[str]:
        files: list[str] = []
        if recursive:
            for root, _dirs, filenames in os.walk(folder_path):
                for name in filenames:
                    files.append(str(Path(root) / name))
        else:
            for item in folder_path.iterdir():
                if item.is_file():
                    files.append(str(item))
        return [f for f in files if is_video_path(f)]
