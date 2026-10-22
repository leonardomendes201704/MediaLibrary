import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import ErrorLog, Job


logger = logging.getLogger("jobs")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobService:
    def enqueue(
        self,
        session: Session,
        job_type: str,
        payload: dict[str, Any] | None = None,
        video_id: int | None = None,
        max_attempts: int = 3,
    ) -> Job:
        payload_bytes = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        existing = session.scalar(
            select(Job).where(
                Job.type == job_type,
                Job.status.in_(["pending", "running"]),
                Job.payload == payload_bytes,
            )
        )
        if existing:
            return existing

        job = Job(
            type=job_type,
            status="pending",
            video_id=video_id,
            payload=payload_bytes,
            max_attempts=max_attempts,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return job

    def decode_payload(self, payload: bytes | None) -> dict[str, Any]:
        if not payload:
            return {}
        try:
            return json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def list_jobs(self, session: Session, limit: int = 200) -> list[Job]:
        stmt: Select[tuple[Job]] = select(Job).order_by(Job.created_at.desc()).limit(limit)
        return list(session.scalars(stmt))

    def recover_orphaned_running_jobs(self, session: Session) -> int:
        jobs = list(session.scalars(select(Job).where(Job.status == "running")))
        if not jobs:
            return 0

        for job in jobs:
            job.status = "pending"
            job.started_at = None
            job.finished_at = None
            if not job.error_message:
                job.error_message = "Recovered after backend restart"

        session.add(
            ErrorLog(
                context="job_worker",
                message=f"Recovered {len(jobs)} orphaned running job(s) on startup",
                details="Status changed from running to pending after restart",
            )
        )
        session.commit()
        return len(jobs)


class JobWorker:
    def __init__(
        self,
        session_factory: sessionmaker,
        process_fn: Callable[[Session, Job], None],
        workers: int = 1,
        poll_interval_seconds: float = 1.0,
        job_types: set[str] | None = None,
        exclude_job_types: set[str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._process_fn = process_fn
        self._workers = workers
        self._poll_interval_seconds = poll_interval_seconds
        self._job_types = job_types
        self._exclude_job_types = exclude_job_types
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._paused = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._paused = False
        for idx in range(self._workers):
            self._tasks.append(asyncio.create_task(self._worker_loop(idx + 1)))
        logger.info("Job worker started with %d worker(s)", self._workers)

    async def stop(self) -> None:
        self._running = False
        self._paused = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Job worker stopped")

    async def pause(self) -> None:
        if not self._running:
            return
        self._paused = True
        logger.info("Job worker paused")

    async def resume(self) -> None:
        if not self._running:
            return
        self._paused = False
        logger.info("Job worker resumed")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._running and self._paused

    async def _worker_loop(self, worker_id: int) -> None:
        while self._running:
            try:
                if self._paused:
                    await asyncio.sleep(self._poll_interval_seconds)
                    continue
                claimed = await asyncio.to_thread(self._claim_next_job)
                if not claimed:
                    await asyncio.sleep(self._poll_interval_seconds)
                    continue
                await asyncio.to_thread(self._process_job, claimed.id, worker_id)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Worker loop error")
                await asyncio.sleep(self._poll_interval_seconds)

    def _claim_next_job(self) -> Job | None:
        with self._session_factory() as session:
            stmt = select(Job).where(Job.status == "pending")
            if self._job_types:
                stmt = stmt.where(Job.type.in_(self._job_types))
            if self._exclude_job_types:
                stmt = stmt.where(Job.type.notin_(self._exclude_job_types))
            job = session.scalar(stmt.order_by(Job.created_at.asc()).limit(1))
            if not job:
                return None
            job.status = "running"
            job.started_at = utcnow()
            job.attempts += 1
            session.commit()
            session.refresh(job)
            return job

    def _process_job(self, job_id: int, worker_id: int) -> None:
        with self._session_factory() as session:
            job = session.get(Job, job_id)
            if not job:
                return
            try:
                logger.info("Worker %d processing job %d (%s)", worker_id, job.id, job.type)
                self._process_fn(session, job)
                job.status = "completed"
                job.finished_at = utcnow()
                session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Job %d failed", job_id)
                job.error_message = str(exc)
                job.finished_at = utcnow()
                if job.attempts >= job.max_attempts:
                    job.status = "failed"
                else:
                    job.status = "pending"
                session.add(
                    ErrorLog(
                        context="job_worker",
                        message=f"job {job.id} failed",
                        details=str(exc),
                    )
                )
                session.commit()

    @staticmethod
    def queue_stats(session: Session) -> dict[str, int]:
        return {
            "pending": session.scalar(select(func.count(Job.id)).where(Job.status == "pending")) or 0,
            "running": session.scalar(select(func.count(Job.id)).where(Job.status == "running")) or 0,
            "failed": session.scalar(select(func.count(Job.id)).where(Job.status == "failed")) or 0,
        }
