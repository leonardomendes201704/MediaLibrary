import asyncio
import logging
from datetime import timedelta

from app.services.ingest_service import IngestService


logger = logging.getLogger("watched_cleanup")

WATCHED_DELETE_GRACE_PERIOD = timedelta(minutes=15)
WATCHED_DELETE_POLL_INTERVAL_SECONDS = 60.0


class WatchedCleanupService:
    def __init__(
        self,
        ingest_service: IngestService,
        poll_interval_seconds: float = WATCHED_DELETE_POLL_INTERVAL_SECONDS,
        watched_grace_period: timedelta = WATCHED_DELETE_GRACE_PERIOD,
    ) -> None:
        self._ingest = ingest_service
        self._poll_interval_seconds = poll_interval_seconds
        self._watched_grace_period = watched_grace_period
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="watched-cleanup-worker")
        logger.info(
            "Watched cleanup worker started (grace_period=%s minutes, poll_interval=%ss)",
            int(self._watched_grace_period.total_seconds() // 60),
            int(self._poll_interval_seconds),
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        logger.info("Watched cleanup worker stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                deleted_videos = await asyncio.to_thread(
                    self._ingest.permanently_delete_watched_not_favorited,
                    self._watched_grace_period,
                )
                if deleted_videos > 0:
                    logger.info(
                        "Watched cleanup worker deleted %d watched non-favorited video(s)",
                        deleted_videos,
                    )
                await asyncio.sleep(self._poll_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Watched cleanup worker loop error")
                await asyncio.sleep(self._poll_interval_seconds)
