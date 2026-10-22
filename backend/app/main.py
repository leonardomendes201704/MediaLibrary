import logging
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api import router as api_router
from app.core.config import get_settings
from app.core.logging_config import setup_logging
from app.db.database import Base, SessionLocal, engine
from app.db.runtime_migrations import ensure_runtime_schema
from app.services.category_cover_service import CategoryCoverService
from app.services.channel_runtime_service import ChannelRuntimeService
from app.services.ffmpeg_service import FFmpegService
from app.services.image_ingest_service import ImageIngestService
from app.services.ingest_service import IngestService
from app.services.job_service import Job, JobService, JobWorker
from app.services.minio_sync_service import MinioSyncService
from app.services.reddit_download_service import RedditDownloadService
from app.services.reddit_service import RedditService
from app.services.slideshow_service import SlideshowService
from app.services.vod_compat_service import VodCompatService
from app.services.watched_cleanup_service import WatchedCleanupService
from app.services.watcher_service import FolderWatcherService


setup_logging()
logger = logging.getLogger("app")
settings = get_settings()
settings.ensure_dirs()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))


def create_app() -> FastAPI:
    app = FastAPI(
        title="Media Library Backend",
        version="1.0.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )
    app.include_router(api_router, prefix="/api")
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "web" / "static")),
        name="static",
    )

    @app.get("/", include_in_schema=False)
    async def root_redirect():
        return RedirectResponse(url="/admin")

    @app.get("/admin", include_in_schema=False)
    async def admin_page(request: Request):
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "api_base_url": "/api",
                "default_transcode_crf": settings.transcode_crf,
            },
        )

    @app.get("/admin/category", include_in_schema=False)
    async def admin_category_page(
        request: Request,
        name: str = Query(default=""),
        favorites: bool = Query(default=False),
        recent: bool = Query(default=False),
        secret: bool = Query(default=False),
        saved: bool = Query(default=False),
        images: bool = Query(default=False),
        random_images: bool = Query(default=False),
        play: bool = Query(default=False),
    ):
        return templates.TemplateResponse(
            "category_gallery.html",
            {
                "request": request,
                "api_base_url": "/api",
                "category_name": name.strip(),
                "favorites_mode": favorites,
                "recent_mode": recent,
                "secret_mode": secret,
                "saved_mode": saved,
                "images_mode": images or random_images,
                "random_images_mode": random_images,
                "play_mode": play,
            },
        )

    @app.get("/admin/catalog", include_in_schema=False)
    async def admin_catalog_page(request: Request):
        return templates.TemplateResponse(
            "category_catalog.html",
            {
                "request": request,
                "api_base_url": "/api",
            },
        )

    @app.get("/admin/channel", include_in_schema=False)
    async def admin_channel_page(
        request: Request,
        key: str = Query(default=""),
        name: str = Query(default=""),
    ):
        return templates.TemplateResponse(
            "channel_player.html",
            {
                "request": request,
                "api_base_url": "/api",
                "channel_key": key.strip(),
                "channel_name": name.strip(),
            },
        )

    @app.on_event("startup")
    async def on_startup() -> None:
        Base.metadata.create_all(bind=engine)
        ensure_runtime_schema(engine)
        ffmpeg = FFmpegService(settings=settings, session_factory=SessionLocal)
        channel_runtime = ChannelRuntimeService(settings=settings)
        vod_compat = VodCompatService(settings=settings)
        jobs = JobService()
        ingest = IngestService(
            session_factory=SessionLocal,
            ffmpeg_service=ffmpeg,
            job_service=jobs,
            settings=settings,
        )
        image_ingest = ImageIngestService(session_factory=SessionLocal)
        minio_sync = MinioSyncService(
            session_factory=SessionLocal,
            job_service=jobs,
            settings=settings,
        )
        ingest.on_video_ready = minio_sync.note_ready_video

        def process_job(session, job: Job) -> None:
            if job.type == "minio_sync":
                minio_sync.handle_job(session, job)
                return
            ingest.handle_job(session, job)

        worker = JobWorker(
            session_factory=SessionLocal,
            process_fn=process_job,
            workers=settings.job_workers,
            exclude_job_types={"minio_sync"},
        )
        minio_worker = JobWorker(
            session_factory=SessionLocal,
            process_fn=minio_sync.handle_job,
            workers=1,
            poll_interval_seconds=2.0,
            job_types={"minio_sync"},
        )
        watcher = FolderWatcherService(
            session_factory=SessionLocal,
            ingest_service=ingest,
            settings=settings,
        )
        reddit = RedditService(settings=settings)
        reddit_download = RedditDownloadService(
            session_factory=SessionLocal,
            reddit_service=reddit,
            ingest_service=ingest,
            image_ingest_service=image_ingest,
            settings=settings,
        )
        watched_cleanup = WatchedCleanupService(
            ingest_service=ingest,
        )
        slideshow = SlideshowService(
            ffmpeg_service=ffmpeg,
            ingest_service=ingest,
            settings=settings,
        )
        app.state.ffmpeg_service = ffmpeg
        app.state.channel_runtime_service = channel_runtime
        app.state.vod_compat_service = vod_compat
        app.state.job_service = jobs
        app.state.ingest_service = ingest
        app.state.minio_sync_service = minio_sync
        app.state.job_worker = worker
        app.state.minio_job_worker = minio_worker
        app.state.watcher_service = watcher
        app.state.reddit_service = reddit
        app.state.reddit_download_service = reddit_download
        app.state.watched_cleanup_service = watched_cleanup
        app.state.slideshow_service = slideshow
        app.state.category_cover_service = CategoryCoverService(
            ffmpeg_service=ffmpeg,
            settings=settings,
        )
        try:
            ffmpeg.ensure_available()
            app.state.media_tools_available = True
        except RuntimeError as exc:
            app.state.media_tools_available = False
            logger.warning("Media tools check failed: %s", exc)

        with SessionLocal() as session:
            recovered = jobs.recover_orphaned_running_jobs(session)
        if recovered > 0:
            logger.warning("Recovered %d orphaned running job(s) as pending on startup", recovered)

        if minio_sync.enabled():
            try:
                minio_sync.ensure_bucket()
                with SessionLocal() as session:
                    queued = minio_sync.backfill(session)
                    pending = minio_sync.pending_count(session)
                logger.info("MinIO sync enabled; new uploads %d, pending %d", queued, pending)
            except Exception as exc:  # noqa: BLE001
                logger.warning("MinIO sync setup failed: %s", exc)
            try:
                with SessionLocal() as session:
                    named = minio_sync.publish_subreddit_index(session)
                logger.info("MinIO subreddit index has %d name(s)", named)
            except Exception as exc:  # noqa: BLE001
                logger.warning("MinIO subreddit index failed: %s", exc)
        else:
            logger.info("MinIO sync disabled")

        await worker.start()
        if minio_sync.enabled():
            await minio_worker.start()
        if settings.folder_watcher_enabled:
            await watcher.start()
        else:
            logger.info("Folder watcher disabled by configuration (FOLDER_WATCHER_ENABLED=false)")
        await reddit_download.start()
        if settings.watched_cleanup_enabled:
            await watched_cleanup.start()
        else:
            logger.info("Watched cleanup worker disabled by configuration (WATCHED_CLEANUP_ENABLED=false)")
        logger.info("Backend startup complete")

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        watcher: FolderWatcherService = app.state.watcher_service
        worker: JobWorker = app.state.job_worker
        minio_worker: JobWorker = app.state.minio_job_worker
        reddit_download: RedditDownloadService = app.state.reddit_download_service
        watched_cleanup: WatchedCleanupService = app.state.watched_cleanup_service
        channel_runtime: ChannelRuntimeService = app.state.channel_runtime_service
        await watched_cleanup.stop()
        await reddit_download.stop()
        await watcher.stop()
        await minio_worker.stop()
        await worker.stop()
        channel_runtime.stop_all()
        logger.info("Backend shutdown complete")

    return app


app = create_app()
