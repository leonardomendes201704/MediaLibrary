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
from app.services.channel_runtime_service import ChannelRuntimeService
from app.services.ffmpeg_service import FFmpegService
from app.services.ingest_service import IngestService
from app.services.job_service import JobService, JobWorker
from app.services.reddit_download_service import RedditDownloadService
from app.services.reddit_service import RedditService
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
        worker = JobWorker(
            session_factory=SessionLocal,
            process_fn=ingest.handle_job,
            workers=settings.job_workers,
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
            settings=settings,
        )
        watched_cleanup = WatchedCleanupService(
            ingest_service=ingest,
        )
        app.state.ffmpeg_service = ffmpeg
        app.state.channel_runtime_service = channel_runtime
        app.state.vod_compat_service = vod_compat
        app.state.job_service = jobs
        app.state.ingest_service = ingest
        app.state.job_worker = worker
        app.state.watcher_service = watcher
        app.state.reddit_service = reddit
        app.state.reddit_download_service = reddit_download
        app.state.watched_cleanup_service = watched_cleanup

        with SessionLocal() as session:
            recovered = jobs.recover_orphaned_running_jobs(session)
        if recovered > 0:
            logger.warning("Recovered %d orphaned running job(s) as pending on startup", recovered)

        await worker.start()
        await watcher.start()
        await reddit_download.start()
        await watched_cleanup.start()
        logger.info("Backend startup complete")

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        watcher: FolderWatcherService = app.state.watcher_service
        worker: JobWorker = app.state.job_worker
        reddit_download: RedditDownloadService = app.state.reddit_download_service
        watched_cleanup: WatchedCleanupService = app.state.watched_cleanup_service
        channel_runtime: ChannelRuntimeService = app.state.channel_runtime_service
        await watched_cleanup.stop()
        await reddit_download.stop()
        await watcher.stop()
        await worker.stop()
        channel_runtime.stop_all()
        logger.info("Backend shutdown complete")

    return app


app = create_app()
