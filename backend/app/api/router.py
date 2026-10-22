from datetime import datetime, timezone
from email.utils import formatdate
import json
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.database import get_session
from app.db.models import AppConfig, Folder, Job, RedditImage, Video
from app.db.schemas import (
    AudioBackfillOut,
    RedditAutoDownloadUpdateIn,
    RedditIdleImageCountUpdateIn,
    ChannelOut,
    CategoryDeleteIn,
    CategoryDeleteOut,
    CategoryCountOut,
    CategoryMediaCountsOut,
    CategoryProgressOut,
    CategoryBlacklistOut,
    CategoryCoverOut,
    ConfigIn,
    ConfigOut,
    ControlStatusOut,
    DeleteResponse,
    FavoriteUpdateIn,
    FavoriteUpdateOut,
    FeedResponse,
    FolderCreate,
    FolderOut,
    FolderUpdate,
    GalleryDownloadListAddIn,
    GalleryDownloadListAddOut,
    GalleryDownloadListItemOut,
    GalleryDownloadListOut,
    GalleryDownloadListRemoveOut,
    ImageCleanupOut,
    ImageFeedResponse,
    ImageOut,
    JobOut,
    LibraryResetOut,
    ReprocessResponse,
    SlideshowOut,
    StatsOut,
    VideoListResponse,
    VideoOut,
    RedditConfigStatusOut,
    RedditDownloadStartIn,
    RedditDownloadStartOut,
    RedditDownloadStatusOut,
    RedditSavedDownloadStartIn,
    RedditSavedSyncStatsOut,
    RedditSelectionIn,
    RedditSelectionOut,
    RedditSubredditOut,
    RedditSubscriptionsOut,
    VideoCleanupOut,
    WatchedUpdateIn,
    WatchedUpdateOut,
)
from app.services.category_cover_service import CategoryCoverService
from app.services.channel_runtime_service import ChannelRuntimeService
from app.services.channel_service import (
    channel_key_for_category,
    find_category_summary_by_key,
    load_channel_videos,
    load_vod_videos,
    list_category_summaries,
)
from app.services.vod_compat_service import VodCompatService
from app.services.category_blacklist import (
    add_blacklisted_category,
    extract_path_badges,
    load_blacklisted_categories,
    normalize_category_name,
)
from app.services.feed_service import count_category_videos, count_category_watched_videos, load_feed
from app.services.gallery_download_list_service import add_item as add_gallery_download_list_item
from app.services.gallery_download_list_service import list_items as list_gallery_download_list_items
from app.services.gallery_download_list_service import remove_item as remove_gallery_download_list_item
from app.services.image_feed_service import (
    count_category_images,
    count_category_watched_images,
    load_image_feed,
)
from app.services.ingest_service import IngestService
from app.services.job_service import JobService, JobWorker
from app.services.reddit_download_service import (
    InsufficientDiskSpaceError,
    REDDIT_DOWNLOADED_SUBREDDITS_KEY,
    RedditDownloadService,
)
from app.services.reddit_service import RedditApiError, RedditConfigError, RedditService
from app.services.slideshow_service import SlideshowService
from app.services.streaming import head_stream_with_range, stream_with_range
from app.services.watcher_service import FolderWatcherService


router = APIRouter()
REDDIT_SELECTED_SUBREDDITS_KEY = "reddit_selected_subreddits"
REDDIT_SUBSCRIPTIONS_CACHE_KEY = "reddit_subscriptions_cache_v1"
REDDIT_SUBSCRIPTIONS_CACHE_AT_KEY = "reddit_subscriptions_cache_at_v1"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _build_control_status(worker: JobWorker, watcher: FolderWatcherService) -> ControlStatusOut:
    if not worker.is_running and not watcher.is_running:
        state = "stopped"
    elif worker.is_paused and watcher.is_paused:
        state = "paused"
    else:
        state = "running"
    return ControlStatusOut(
        state=state,
        worker_running=worker.is_running,
        worker_paused=worker.is_paused,
        watcher_running=watcher.is_running,
        watcher_paused=watcher.is_paused,
    )


def _ready_storage_bytes(session: Session) -> int:
    total = session.scalar(
        select(func.coalesce(func.sum(Video.source_size), 0)).where(
            Video.is_deleted.is_(False),
            Video.status.in_(["ready", "duplicate"]),
        )
    )
    return int(total or 0)


def _images_storage_bytes(session: Session) -> int:
    total = session.scalar(
        select(func.coalesce(func.sum(RedditImage.source_size), 0)).where(
            RedditImage.is_deleted.is_(False),
            RedditImage.status == "ready",
        )
    )
    return int(total or 0)


def _safe_int(value: object) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _absolute_app_url(request: Request, path: str, **query: object) -> str:
    base = str(request.base_url).rstrip("/")
    url = f"{base}{path}"
    params = [(key, str(value)) for key, value in query.items() if value not in {None, ""}]
    if params:
        url = f"{url}?{urlencode(params)}"
    return url


def _category_summary_to_channel(summary, request: Request) -> ChannelOut:
    live_url = _absolute_app_url(request, f"/api/channels/{summary.channel_key}/hls/index.m3u8")
    gallery_name = summary.category
    gallery_params: dict[str, str] = {"name": gallery_name}
    if int(getattr(summary, "video_count", 0) or 0) <= 0 and int(getattr(summary, "image_count", 0) or 0) > 0:
        gallery_params["images"] = "1"
    return ChannelOut(
        category=summary.category,
        channel_key=summary.channel_key,
        video_count=int(getattr(summary, "video_count", summary.count) or 0),
        image_count=int(getattr(summary, "image_count", 0) or 0),
        favorites_count=summary.favorites_count,
        watched_count=summary.watched_count,
        is_user_category=summary.is_user_category,
        gallery_url=_absolute_app_url(request, "/admin/category", **gallery_params),
        play_url=_absolute_app_url(request, "/admin/channel", key=summary.channel_key, name=summary.category),
        feed_url=_absolute_app_url(
            request,
            "/api/feed",
            category=summary.category,
            order_by="detected_desc",
            page_size=100,
        ),
        playlist_url=_absolute_app_url(
            request,
            "/api/channels/playlist.m3u",
            category=summary.category,
            order_by="detected_desc",
            limit=300,
        ),
        live_url=live_url,
    )


def _escape_m3u_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ").strip()


def _head_file_response(path: Path, media_type: str, headers: dict[str, str] | None = None) -> Response:
    stat = path.stat()
    merged_headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(stat.st_size),
        "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
    }
    if headers:
        merged_headers.update(headers)
    return Response(status_code=200, media_type=media_type, headers=merged_headers)


def _build_category_playlist_m3u(request: Request, category: str, videos: list[Video]) -> str:
    group_title = "Usuarios" if normalize_category_name(category).startswith("u_") else "Categorias"
    lines = ["#EXTM3U", f'#PLAYLIST:{_escape_m3u_value(category)}']

    for video in videos:
        stream_url = str(request.url_for("stream_video", video_id=video.id))
        title = Path(video.original_path).stem.strip() or f"Video {video.id}"
        duration_seconds = int(video.duration_seconds) if video.duration_seconds and video.duration_seconds > 0 else -1
        attrs = [
            f'tvg-id="{_escape_m3u_value(f"{category}-{video.id}")}"',
            f'tvg-name="{_escape_m3u_value(title)}"',
            f'group-title="{_escape_m3u_value(group_title)}"',
        ]
        if video.thumbnail_path and Path(video.thumbnail_path).exists():
            thumb_url = str(request.url_for("get_thumbnail", video_id=video.id))
            attrs.append(f'tvg-logo="{_escape_m3u_value(thumb_url)}"')
        lines.append(f'#EXTINF:{duration_seconds} {" ".join(attrs)},{title}')
        lines.append(stream_url)

    return "\n".join(lines) + "\n"


def _build_master_channels_m3u(request: Request, channels: list[ChannelOut]) -> str:
    lines = ["#EXTM3U", '#PLAYLIST:MediaLibrary Channels']
    for channel in channels:
        attrs = [
            f'tvg-id="{_escape_m3u_value(channel.channel_key)}"',
            f'tvg-name="{_escape_m3u_value(channel.category)}"',
            f'group-title="{_escape_m3u_value("Usuarios" if channel.is_user_category else "Categorias")}"',
        ]
        lines.append(f'#EXTINF:-1 {" ".join(attrs)},{channel.category}')
        lines.append(channel.live_url)
    return "\n".join(lines) + "\n"


def _build_vod_playlist_m3u(request: Request, videos: list[Video]) -> str:
    lines = ["#EXTM3U", '#PLAYLIST:MediaLibrary VOD']

    for video in videos:
        stream_url = str(request.url_for("get_video_vod_manifest", video_id=video.id))
        title = Path(video.original_path).stem.strip() or f"Video {video.id}"
        category, _, _ = extract_path_badges(video.original_path)
        group_title = str(category or "Sem categoria").strip() or "Sem categoria"
        normalized_group = normalize_category_name(group_title) or "vod"
        duration_seconds = int(video.duration_seconds) if video.duration_seconds and video.duration_seconds > 0 else -1
        attrs = [
            f'tvg-id="{_escape_m3u_value(f"{normalized_group}-{video.id}")}"',
            f'tvg-name="{_escape_m3u_value(title)}"',
            f'group-title="{_escape_m3u_value(group_title)}"',
        ]
        if video.thumbnail_path and Path(video.thumbnail_path).exists():
            thumb_url = str(request.url_for("get_thumbnail", video_id=video.id))
            attrs.append(f'tvg-logo="{_escape_m3u_value(thumb_url)}"')
        lines.append(f'#EXTINF:{duration_seconds} {" ".join(attrs)},{title}')
        lines.append(stream_url)

    return "\n".join(lines) + "\n"


def _normalize_subreddit_name(value: str) -> str:
    return normalize_category_name(value)


def _load_selected_subreddits(session: Session) -> set[str]:
    row = session.scalar(select(AppConfig).where(AppConfig.key == REDDIT_SELECTED_SUBREDDITS_KEY))
    if not row:
        return set()
    try:
        parsed = json.loads(row.value)
    except json.JSONDecodeError:
        return set()
    if not isinstance(parsed, list):
        return set()
    selected: set[str] = set()
    for item in parsed:
        if isinstance(item, str):
            normalized = _normalize_subreddit_name(item).lower()
            if normalized:
                selected.add(normalized)
    return selected


def _save_selected_subreddits(session: Session, names: list[str]) -> set[str]:
    selected: set[str] = set()
    for raw_name in names:
        normalized = _normalize_subreddit_name(str(raw_name)).lower()
        if normalized:
            selected.add(normalized)
    serialized = json.dumps(sorted(selected), ensure_ascii=False)

    row = session.scalar(select(AppConfig).where(AppConfig.key == REDDIT_SELECTED_SUBREDDITS_KEY))
    if row:
        row.value = serialized
    else:
        session.add(AppConfig(key=REDDIT_SELECTED_SUBREDDITS_KEY, value=serialized))
    session.commit()
    return selected


def _parse_datetime(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        normalized = raw.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _load_reddit_subscriptions_cache(session: Session) -> tuple[list[dict[str, object]], datetime | None]:
    payload_entry = session.scalar(select(AppConfig).where(AppConfig.key == REDDIT_SUBSCRIPTIONS_CACHE_KEY))
    cached_at_entry = session.scalar(select(AppConfig).where(AppConfig.key == REDDIT_SUBSCRIPTIONS_CACHE_AT_KEY))
    if not payload_entry or not payload_entry.value:
        return [], None

    try:
        parsed = json.loads(payload_entry.value)
    except json.JSONDecodeError:
        return [], _parse_datetime(cached_at_entry.value if cached_at_entry else None)

    if not isinstance(parsed, list):
        return [], _parse_datetime(cached_at_entry.value if cached_at_entry else None)

    items: list[dict[str, object]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        items.append(
            {
                "name": name,
                "title": str(item.get("title") or "").strip(),
                "over18": bool(item.get("over18")),
                "subscribers": int(item.get("subscribers") or 0),
                "url": str(item.get("url") or "").strip(),
            }
        )

    return items, _parse_datetime(cached_at_entry.value if cached_at_entry else None)


def _save_reddit_subscriptions_cache(session: Session, items: list[dict[str, object]]) -> datetime:
    sanitized: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        sanitized.append(
            {
                "name": name,
                "title": str(item.get("title") or "").strip(),
                "over18": bool(item.get("over18")),
                "subscribers": int(item.get("subscribers") or 0),
                "url": str(item.get("url") or "").strip(),
            }
        )

    cached_at = utcnow()
    payload_value = json.dumps(sanitized, ensure_ascii=False)
    cached_at_value = cached_at.isoformat()

    payload_entry = session.scalar(select(AppConfig).where(AppConfig.key == REDDIT_SUBSCRIPTIONS_CACHE_KEY))
    if payload_entry:
        payload_entry.value = payload_value
    else:
        session.add(AppConfig(key=REDDIT_SUBSCRIPTIONS_CACHE_KEY, value=payload_value))

    cached_at_entry = session.scalar(select(AppConfig).where(AppConfig.key == REDDIT_SUBSCRIPTIONS_CACHE_AT_KEY))
    if cached_at_entry:
        cached_at_entry.value = cached_at_value
    else:
        session.add(AppConfig(key=REDDIT_SUBSCRIPTIONS_CACHE_AT_KEY, value=cached_at_value))

    session.commit()
    return cached_at


def _remove_subreddit_from_subscriptions_cache(session: Session, normalized_name: str) -> bool:
    cached_items, _cached_at = _load_reddit_subscriptions_cache(session)
    if not cached_items:
        return False

    filtered = [
        item
        for item in cached_items
        if _normalize_subreddit_name(str(item.get("name") or "")).lower() != normalized_name
    ]
    if len(filtered) == len(cached_items):
        return False

    _save_reddit_subscriptions_cache(session, filtered)
    return True


def _remove_subreddit_from_downloaded(session: Session, normalized_name: str) -> bool:
    row = session.scalar(select(AppConfig).where(AppConfig.key == REDDIT_DOWNLOADED_SUBREDDITS_KEY))
    if not row or not row.value:
        return False

    try:
        parsed = json.loads(row.value)
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed, list):
        return False

    updated: list[str] = []
    changed = False
    for item in parsed:
        if not isinstance(item, str):
            continue
        normalized_item = _normalize_subreddit_name(item).lower()
        if not normalized_item:
            continue
        if normalized_item == normalized_name:
            changed = True
            continue
        updated.append(normalized_item)

    if not changed:
        return False

    row.value = json.dumps(sorted(set(updated)), ensure_ascii=False)
    session.commit()
    return True


def _clear_downloaded_subreddits(session: Session) -> bool:
    row = session.scalar(select(AppConfig).where(AppConfig.key == REDDIT_DOWNLOADED_SUBREDDITS_KEY))
    if not row:
        return False
    session.delete(row)
    session.commit()
    return True


def _clear_runtime_app_state(session: Session) -> int:
    # Preserva assinaturas e selecao (configs). Limpa so o marcador de
    # "ja baixado", para permitir novo download apos zerar a midia.
    runtime_keys = {
        REDDIT_DOWNLOADED_SUBREDDITS_KEY,
    }
    deleted_rows = session.execute(delete(AppConfig).where(AppConfig.key.in_(runtime_keys)))
    session.commit()
    return int(deleted_rows.rowcount or 0)


def _current_processing_context(
    session: Session,
    job_service: JobService,
) -> tuple[str | None, str | None, int | None]:
    running_jobs = list(
        session.scalars(
            select(Job)
            .where(Job.status == "running")
            .order_by(Job.started_at.asc(), Job.id.asc())
            .limit(10)
        )
    )
    if not running_jobs:
        return None, None, None

    for job in running_jobs:
        payload = job_service.decode_payload(job.payload)
        source_path = str(payload.get("source_path") or "").strip()
        folder_id = _safe_int(payload.get("folder_id"))
        video_id = _safe_int(payload.get("video_id")) or _safe_int(job.video_id)

        folder_path: str | None = None
        if video_id:
            video = session.get(Video, video_id)
            if video:
                source_path = source_path or video.original_path
                if video.folder_id:
                    folder = session.get(Folder, video.folder_id)
                    if folder:
                        folder_path = folder.path

        if folder_path is None and folder_id:
            folder = session.get(Folder, folder_id)
            if folder:
                folder_path = folder.path

        category: str | None = None
        if source_path:
            category, _, _ = extract_path_badges(source_path)

        if category or folder_path or video_id:
            return category, folder_path, video_id

    return None, None, None


def _video_to_schema(video: Video, request: Request) -> VideoOut:
    stream_url = None
    thumb_url = None
    if video.status in {"ready", "duplicate"} and video.derived_path:
        stream_url = str(request.url_for("stream_video", video_id=video.id))
    if video.thumbnail_path and Path(video.thumbnail_path).exists():
        thumb_url = str(request.url_for("get_thumbnail", video_id=video.id))
    path_category, path_date, _ = extract_path_badges(video.original_path)

    return VideoOut(
        id=video.id,
        folder_id=video.folder_id,
        original_path=video.original_path,
        derived_path=video.derived_path,
        thumbnail_path=video.thumbnail_path,
        status=video.status,
        is_deleted=video.is_deleted,
        is_favorite=video.is_favorite,
        is_watched=video.is_watched,
        is_reddit_saved=bool(getattr(video, "is_reddit_saved", False)),
        duplicate_of_video_id=video.duplicate_of_video_id,
        duration_seconds=video.duration_seconds,
        width=video.width,
        height=video.height,
        video_codec=video.video_codec,
        has_audio=video.has_audio,
        audio_codec=video.audio_codec,
        container=video.container,
        bitrate=video.bitrate,
        source_size=video.source_size,
        processing_error=video.processing_error,
        detected_at=video.detected_at,
        processed_at=video.processed_at,
        watched_at=video.watched_at,
        updated_at=video.updated_at,
        stream_url=stream_url,
        thumbnail_url=thumb_url,
        path_category=path_category,
        path_date=path_date,
    )


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    media_output_dir = str(settings.media_output_dir.expanduser().resolve())
    return {
        "status": "ok",
        "media_output_dir": media_output_dir,
    }


@router.get("/control/status", response_model=ControlStatusOut)
def get_control_status(request: Request) -> ControlStatusOut:
    worker: JobWorker = request.app.state.job_worker  # type: ignore[assignment]
    watcher: FolderWatcherService = request.app.state.watcher_service  # type: ignore[assignment]
    return _build_control_status(worker, watcher)


@router.post("/control/start", response_model=ControlStatusOut)
async def start_pipeline(request: Request) -> ControlStatusOut:
    worker: JobWorker = request.app.state.job_worker  # type: ignore[assignment]
    watcher: FolderWatcherService = request.app.state.watcher_service  # type: ignore[assignment]
    settings = get_settings()

    if not worker.is_running:
        await worker.start()
    else:
        await worker.resume()

    if settings.folder_watcher_enabled:
        if not watcher.is_running:
            await watcher.start()
        else:
            await watcher.resume()

    return _build_control_status(worker, watcher)


@router.post("/control/pause", response_model=ControlStatusOut)
async def pause_pipeline(request: Request) -> ControlStatusOut:
    worker: JobWorker = request.app.state.job_worker  # type: ignore[assignment]
    watcher: FolderWatcherService = request.app.state.watcher_service  # type: ignore[assignment]

    await worker.pause()
    await watcher.pause()

    return _build_control_status(worker, watcher)


@router.post("/control/stop", response_model=ControlStatusOut)
async def stop_pipeline(request: Request) -> ControlStatusOut:
    worker: JobWorker = request.app.state.job_worker  # type: ignore[assignment]
    watcher: FolderWatcherService = request.app.state.watcher_service  # type: ignore[assignment]

    if watcher.is_running:
        await watcher.stop()
    if worker.is_running:
        await worker.stop()

    return _build_control_status(worker, watcher)


@router.get("/config", response_model=ConfigOut)
def get_config(session: Session = Depends(get_session)) -> ConfigOut:
    configs = list(session.scalars(select(AppConfig)))
    values = {c.key: c.value for c in configs}
    return ConfigOut(values=values)


@router.put("/config", response_model=ConfigOut)
def put_config(payload: ConfigIn, session: Session = Depends(get_session)) -> ConfigOut:
    for key, value in payload.values.items():
        entry = session.scalar(select(AppConfig).where(AppConfig.key == key))
        if entry:
            entry.value = value
        else:
            session.add(AppConfig(key=key, value=value))
    session.commit()
    return get_config(session)


@router.get("/folders", response_model=list[FolderOut])
def get_folders(
    request: Request,
    session: Session = Depends(get_session),
) -> list[FolderOut]:
    folders = list(session.scalars(select(Folder).order_by(Folder.path.asc())))
    ingest: IngestService = request.app.state.ingest_service  # type: ignore[assignment]
    result: list[FolderOut] = []
    for folder in folders:
        counts = ingest.folder_video_stats(session, folder.id)
        result.append(
            FolderOut(
                id=folder.id,
                path=folder.path,
                is_active=folder.is_active,
                serve_files=folder.serve_files,
                scan_subdirs=folder.scan_subdirs,
                rules_json=folder.rules_json,
                last_scan_at=folder.last_scan_at,
                created_at=folder.created_at,
                updated_at=folder.updated_at,
                **counts,
            )
        )
    return result


@router.post("/folders", response_model=FolderOut)
def create_folder(payload: FolderCreate, request: Request, session: Session = Depends(get_session)) -> FolderOut:
    existing = session.scalar(select(Folder).where(Folder.path == payload.path))
    if existing:
        raise HTTPException(status_code=409, detail="Folder already exists")
    if not Path(payload.path).exists():
        raise HTTPException(status_code=400, detail="Folder path does not exist")
    folder = Folder(
        path=payload.path,
        is_active=payload.is_active,
        serve_files=payload.serve_files,
        scan_subdirs=payload.scan_subdirs,
        rules_json=payload.rules_json,
    )
    session.add(folder)
    session.commit()
    session.refresh(folder)
    ingest: IngestService = request.app.state.ingest_service  # type: ignore[assignment]
    counts = ingest.folder_video_stats(session, folder.id)
    return FolderOut(
        id=folder.id,
        path=folder.path,
        is_active=folder.is_active,
        serve_files=folder.serve_files,
        scan_subdirs=folder.scan_subdirs,
        rules_json=folder.rules_json,
        last_scan_at=folder.last_scan_at,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
        **counts,
    )


@router.put("/folders/{folder_id}", response_model=FolderOut)
def update_folder(
    folder_id: int,
    payload: FolderUpdate,
    request: Request,
    session: Session = Depends(get_session),
) -> FolderOut:
    folder = session.get(Folder, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(folder, key, value)
    session.commit()
    session.refresh(folder)
    ingest: IngestService = request.app.state.ingest_service  # type: ignore[assignment]
    counts = ingest.folder_video_stats(session, folder.id)
    return FolderOut(
        id=folder.id,
        path=folder.path,
        is_active=folder.is_active,
        serve_files=folder.serve_files,
        scan_subdirs=folder.scan_subdirs,
        rules_json=folder.rules_json,
        last_scan_at=folder.last_scan_at,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
        **counts,
    )


@router.delete("/folders/{folder_id}", response_model=DeleteResponse)
def delete_folder(folder_id: int, session: Session = Depends(get_session)) -> DeleteResponse:
    folder = session.get(Folder, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    session.delete(folder)
    session.commit()
    return DeleteResponse(success=True)


@router.get("/videos", response_model=VideoListResponse)
def list_videos(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=200),
    status: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> VideoListResponse:
    ingest: IngestService = request.app.state.ingest_service  # type: ignore[assignment]
    videos, total = ingest.list_videos(session, page=page, page_size=page_size, status=status)
    return VideoListResponse(
        items=[_video_to_schema(v, request) for v in videos],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/videos/{video_id}", response_model=VideoOut)
def get_video(video_id: int, request: Request, session: Session = Depends(get_session)) -> VideoOut:
    video = session.get(Video, video_id)
    if not video or video.is_deleted:
        raise HTTPException(status_code=404, detail="Video not found")
    return _video_to_schema(video, request)


@router.api_route("/videos/{video_id}/stream", methods=["GET", "HEAD"], name="stream_video")
def stream_video(video_id: int, request: Request, session: Session = Depends(get_session)):
    video = session.get(Video, video_id)
    if not video or video.is_deleted:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.status not in {"ready", "duplicate"} or not video.derived_path:
        raise HTTPException(status_code=409, detail="Video is not ready for streaming")
    range_header = request.headers.get("range")
    if request.method == "HEAD":
        return head_stream_with_range(video.derived_path, range_header)
    return stream_with_range(video.derived_path, range_header)


@router.api_route("/videos/{video_id}/vod.mp4", methods=["GET", "HEAD"], name="get_video_vod_stream")
def get_video_vod_stream(video_id: int, request: Request, session: Session = Depends(get_session)):
    video = session.get(Video, video_id)
    if not video or video.is_deleted:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.status not in {"ready", "duplicate"} or not video.derived_path:
        raise HTTPException(status_code=409, detail="Video is not ready for VOD playback")

    vod_compat: VodCompatService = request.app.state.vod_compat_service  # type: ignore[assignment]
    try:
        compatible_path = vod_compat.ensure_compatible_path(video)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    range_header = request.headers.get("range")
    if request.method == "HEAD":
        return head_stream_with_range(str(compatible_path), range_header)
    return stream_with_range(str(compatible_path), range_header)


@router.api_route("/videos/{video_id}/vod/index.m3u8", methods=["GET", "HEAD"], name="get_video_vod_manifest")
def get_video_vod_manifest(video_id: int, request: Request, session: Session = Depends(get_session)):
    video = session.get(Video, video_id)
    if not video or video.is_deleted:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.status not in {"ready", "duplicate"} or not video.derived_path:
        raise HTTPException(status_code=409, detail="Video is not ready for VOD playback")

    vod_compat: VodCompatService = request.app.state.vod_compat_service  # type: ignore[assignment]
    try:
        manifest_path = vod_compat.ensure_hls_manifest(video)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if request.method == "HEAD":
        return _head_file_response(
            manifest_path,
            "application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )
    return FileResponse(
        manifest_path,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.api_route("/videos/{video_id}/vod/{filename}", methods=["GET", "HEAD"])
def get_video_vod_asset(video_id: int, filename: str, request: Request, session: Session = Depends(get_session)):
    video = session.get(Video, video_id)
    if not video or video.is_deleted:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.status not in {"ready", "duplicate"} or not video.derived_path:
        raise HTTPException(status_code=409, detail="Video is not ready for VOD playback")
    if filename == "index.m3u8":
        raise HTTPException(status_code=404, detail="Manifest route is /vod/index.m3u8")

    vod_compat: VodCompatService = request.app.state.vod_compat_service  # type: ignore[assignment]
    try:
        vod_compat.ensure_hls_manifest(video)
        asset_path = vod_compat.resolve_output_path(video_id, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not asset_path.exists() or not asset_path.is_file():
        raise HTTPException(status_code=404, detail="VOD asset not found")

    media_type = "video/mp2t"
    if asset_path.suffix.lower() == ".m3u8":
        media_type = "application/vnd.apple.mpegurl"
    if request.method == "HEAD":
        return _head_file_response(
            asset_path,
            media_type,
            headers={"Cache-Control": "public, max-age=3600"},
        )
    return FileResponse(
        asset_path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/videos/{video_id}/thumbnail", name="get_thumbnail")
def get_thumbnail(video_id: int, session: Session = Depends(get_session)):
    video = session.get(Video, video_id)
    if not video or video.is_deleted:
        raise HTTPException(status_code=404, detail="Video not found")
    if not video.thumbnail_path or not Path(video.thumbnail_path).exists():
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(video.thumbnail_path, media_type="image/jpeg")


@router.post("/videos/{video_id}/reprocess", response_model=ReprocessResponse)
def reprocess_video(video_id: int, request: Request, session: Session = Depends(get_session)) -> ReprocessResponse:
    video = session.get(Video, video_id)
    if not video or video.is_deleted:
        raise HTTPException(status_code=404, detail="Video not found")
    ingest: IngestService = request.app.state.ingest_service  # type: ignore[assignment]
    job_id = ingest.enqueue_reprocess(video_id)
    return ReprocessResponse(success=True, job_id=job_id)


@router.delete("/videos/{video_id}", response_model=DeleteResponse)
def delete_video(
    video_id: int,
    request: Request,
    permanent: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> DeleteResponse:
    ingest: IngestService = request.app.state.ingest_service  # type: ignore[assignment]
    success = ingest.permanently_delete_video(video_id) if permanent else ingest.remove_from_catalog(video_id)
    if not success:
        raise HTTPException(status_code=404, detail="Video not found")
    return DeleteResponse(success=True)


@router.post("/videos/{video_id}/blacklist-category", response_model=CategoryBlacklistOut)
def blacklist_video_category(
    video_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
) -> CategoryBlacklistOut:
    video = session.get(Video, video_id)
    if not video or video.is_deleted:
        raise HTTPException(status_code=404, detail="Video not found")

    category, _, _ = extract_path_badges(video.original_path)
    display_category = (category or "").strip()
    normalized_category = normalize_category_name(display_category)
    if not normalized_category:
        raise HTTPException(status_code=400, detail="Categoria do video nao encontrada.")

    add_blacklisted_category(session, display_category)

    selected = _load_selected_subreddits(session)
    removed_from_selection = normalized_category in selected
    if removed_from_selection:
        _save_selected_subreddits(session, [name for name in selected if name != normalized_category])
    _remove_subreddit_from_subscriptions_cache(session, normalized_category)
    _remove_subreddit_from_downloaded(session, normalized_category)

    ingest: IngestService = request.app.state.ingest_service  # type: ignore[assignment]
    deleted_videos = ingest.count_category_videos(display_category)
    background_tasks.add_task(ingest.permanently_delete_category, display_category)

    return CategoryBlacklistOut(
        success=True,
        category=display_category,
        deleted_videos=deleted_videos,
        removed_from_selection=removed_from_selection,
        queued=True,
    )


def _category_cover_url(request: Request, category: str, version: int) -> str:
    base_url = str(request.url_for("get_category_cover", category=quote(category, safe="")))
    return f"{base_url}?v={version}"


def _set_category_cover(
    request: Request,
    original_path: str,
    source_candidates: list[str | None],
) -> CategoryCoverOut:
    category, _, _ = extract_path_badges(original_path)
    display_category = (category or "").strip()
    if not normalize_category_name(display_category):
        raise HTTPException(status_code=400, detail="Categoria da midia nao encontrada.")

    source_path = next(
        (str(candidate) for candidate in source_candidates if candidate and Path(candidate).exists()),
        None,
    )
    if not source_path:
        raise HTTPException(status_code=404, detail="Arquivo da midia nao encontrado.")

    covers: CategoryCoverService = request.app.state.category_cover_service  # type: ignore[assignment]
    try:
        covers.save_from_source(display_category, source_path)
    except (RuntimeError, OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Falha ao gerar capa: {str(exc)[-300:]}") from exc

    version = covers.cover_version(display_category)
    return CategoryCoverOut(
        success=True,
        category=display_category,
        cover_url=_category_cover_url(request, display_category, version) if version is not None else None,
    )


@router.post("/videos/{video_id}/category-cover", response_model=CategoryCoverOut)
def set_video_category_cover(
    video_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> CategoryCoverOut:
    video = session.get(Video, video_id)
    if not video or video.is_deleted:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.status not in {"ready", "duplicate"}:
        raise HTTPException(status_code=409, detail="Video ainda nao esta pronto.")
    return _set_category_cover(request, video.original_path, [video.derived_path, video.original_path])


@router.post("/images/{image_id}/category-cover", response_model=CategoryCoverOut)
def set_image_category_cover(
    image_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> CategoryCoverOut:
    image = session.get(RedditImage, image_id)
    if not image or image.is_deleted or image.status != "ready":
        raise HTTPException(status_code=404, detail="Image not found")
    return _set_category_cover(request, image.original_path, [image.file_path])


@router.post("/videos/category/delete", response_model=CategoryDeleteOut)
def delete_category_videos(
    payload: CategoryDeleteIn,
    request: Request,
    background_tasks: BackgroundTasks,
) -> CategoryDeleteOut:
    display_category = str(payload.category or "").strip()
    if not display_category:
        raise HTTPException(status_code=400, detail="Categoria do video nao encontrada.")

    ingest: IngestService = request.app.state.ingest_service  # type: ignore[assignment]
    deleted_videos = ingest.count_category_videos(display_category)
    deleted_images = ingest.count_category_images(display_category)
    covers: CategoryCoverService = request.app.state.category_cover_service  # type: ignore[assignment]
    covers.remove(display_category)
    if deleted_videos > 0:
        background_tasks.add_task(ingest.permanently_delete_category, display_category)
    if deleted_images > 0:
        background_tasks.add_task(ingest.permanently_delete_category_images, display_category)

    return CategoryDeleteOut(
        success=True,
        category=display_category,
        deleted_videos=deleted_videos,
        deleted_images=deleted_images,
        queued=(deleted_videos + deleted_images) > 0,
    )


@router.post("/videos/cleanup/watched-unfavorited", response_model=VideoCleanupOut)
def cleanup_watched_unfavorited_videos(
    request: Request,
) -> VideoCleanupOut:
    ingest: IngestService = request.app.state.ingest_service  # type: ignore[assignment]
    deleted_videos = ingest.permanently_delete_watched_not_favorited()
    return VideoCleanupOut(success=True, deleted_videos=deleted_videos)


@router.post("/videos/cleanup/downloaded-all", response_model=VideoCleanupOut)
def cleanup_all_downloaded_videos(
    request: Request,
    session: Session = Depends(get_session),
) -> VideoCleanupOut:
    reddit_download: RedditDownloadService = request.app.state.reddit_download_service  # type: ignore[assignment]
    download_state = str(reddit_download.get_status().get("state") or "").lower()
    if download_state == "running":
        raise HTTPException(status_code=409, detail="Pare o download Reddit atual antes de limpar todos os videos baixados.")

    ingest: IngestService = request.app.state.ingest_service  # type: ignore[assignment]
    deleted_videos = ingest.permanently_delete_all_downloaded_videos()
    _clear_downloaded_subreddits(session)
    reddit_download.clear_media_history(media_type="video")
    return VideoCleanupOut(success=True, deleted_videos=deleted_videos)


@router.post("/images/cleanup/downloaded-all", response_model=ImageCleanupOut)
def cleanup_all_downloaded_images(
    request: Request,
    session: Session = Depends(get_session),
) -> ImageCleanupOut:
    reddit_download: RedditDownloadService = request.app.state.reddit_download_service  # type: ignore[assignment]
    download_state = str(reddit_download.get_status().get("state") or "").lower()
    if download_state == "running":
        raise HTTPException(status_code=409, detail="Pare o download Reddit atual antes de limpar todas as imagens baixadas.")

    ingest: IngestService = request.app.state.ingest_service  # type: ignore[assignment]
    deleted_images = ingest.permanently_delete_all_downloaded_images()
    reddit_download.clear_downloaded_image_subreddits()
    reddit_download.clear_media_history(media_type="image")
    return ImageCleanupOut(success=True, deleted_images=deleted_images)


@router.post("/library/reset", response_model=LibraryResetOut)
async def reset_library_preserving_config(
    request: Request,
    session: Session = Depends(get_session),
) -> LibraryResetOut:
    worker: JobWorker = request.app.state.job_worker  # type: ignore[assignment]
    watcher: FolderWatcherService = request.app.state.watcher_service  # type: ignore[assignment]
    reddit_download: RedditDownloadService = request.app.state.reddit_download_service  # type: ignore[assignment]

    if watcher.is_running:
        await watcher.stop()
    if worker.is_running:
        await worker.stop()
    if str(reddit_download.get_status().get("state") or "").lower() == "running":
        await reddit_download.stop_download(reason="Download interrompido para reset da base.")

    ingest: IngestService = request.app.state.ingest_service  # type: ignore[assignment]
    result = ingest.reset_library_data()
    cleared_runtime_config_keys = _clear_runtime_app_state(session)
    return LibraryResetOut(success=True, cleared_runtime_config_keys=cleared_runtime_config_keys, **result)


@router.post("/videos/backfill-audio", response_model=AudioBackfillOut)
def backfill_audio_metadata(
    request: Request,
    limit: int = Query(default=200, ge=1, le=1000),
) -> AudioBackfillOut:
    ingest: IngestService = request.app.state.ingest_service  # type: ignore[assignment]
    result = ingest.backfill_missing_audio_metadata(limit=limit)
    return AudioBackfillOut(success=True, **result)


@router.post("/videos/slideshow", response_model=SlideshowOut)
def create_thumbs_slideshow(request: Request) -> SlideshowOut:
    slideshow: SlideshowService = request.app.state.slideshow_service  # type: ignore[assignment]
    try:
        result = slideshow.generate()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Falha ao gerar slideshow: {exc}") from exc
    return SlideshowOut(**result)


@router.put("/videos/{video_id}/favorite", response_model=FavoriteUpdateOut)
def update_video_favorite(
    video_id: int,
    payload: FavoriteUpdateIn,
    request: Request,
    session: Session = Depends(get_session),
) -> FavoriteUpdateOut:
    video = session.get(Video, video_id)
    if not video or video.is_deleted:
        raise HTTPException(status_code=404, detail="Video not found")

    video.is_favorite = payload.is_favorite
    video.favorite_at = utcnow() if payload.is_favorite else None
    minio_sync = getattr(request.app.state, "minio_sync_service", None)
    if minio_sync is not None:
        minio_sync.schedule_favorite(session, "video", video, payload.is_favorite)
    else:
        session.commit()

    return FavoriteUpdateOut(
        success=True,
        video_id=video.id,
        is_favorite=video.is_favorite,
    )


@router.put("/videos/{video_id}/watched", response_model=WatchedUpdateOut)
def update_video_watched(
    video_id: int,
    payload: WatchedUpdateIn,
    session: Session = Depends(get_session),
) -> WatchedUpdateOut:
    video = session.get(Video, video_id)
    if not video or video.is_deleted:
        raise HTTPException(status_code=404, detail="Video not found")

    video.is_watched = payload.is_watched
    video.watched_at = utcnow() if payload.is_watched else None
    if payload.watched_ratio is not None:
        video.watched_ratio = max(0.0, min(payload.watched_ratio, 1.0))
    elif not payload.is_watched:
        video.watched_ratio = None
    session.commit()

    return WatchedUpdateOut(
        success=True,
        video_id=video.id,
        is_watched=video.is_watched,
        watched_ratio=video.watched_ratio,
    )


@router.get("/feed", response_model=FeedResponse)
def get_feed(
    request: Request,
    cursor: str | None = Query(default=None),
    page_size: int = Query(default=15, ge=1, le=100),
    random_order: bool = Query(default=False),
    exclude_favorites: bool = Query(default=False),
    only_favorites: bool = Query(default=False),
    exclude_watched: bool = Query(default=False),
    only_watched: bool = Query(default=False),
    only_reddit_saved: bool = Query(default=False),
    order_by: Literal["processed_desc", "detected_desc", "favorite_desc"] = Query(default="processed_desc"),
    category: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> FeedResponse:
    blacklisted_categories = load_blacklisted_categories(session)
    if not only_reddit_saved and normalize_category_name(category or "") in blacklisted_categories:
        return FeedResponse(items=[], next_cursor=None)

    items, next_cursor = load_feed(
        session,
        cursor=cursor,
        page_size=page_size,
        random_order=random_order,
        exclude_favorites=exclude_favorites,
        only_favorites=only_favorites,
        exclude_watched=exclude_watched,
        only_watched=only_watched,
        order_by=order_by,
        category=None if only_reddit_saved else category,
        blacklisted_categories=blacklisted_categories,
        only_reddit_saved=only_reddit_saved,
    )
    return FeedResponse(items=[_video_to_schema(v, request) for v in items], next_cursor=next_cursor)


def _image_to_schema(image: RedditImage, request: Request) -> ImageOut:
    file_url = None
    if image.status == "ready" and image.file_path and Path(image.file_path).exists():
        file_url = str(request.url_for("get_image_file", image_id=image.id))
    path_category, path_date, _ = extract_path_badges(image.original_path)
    return ImageOut(
        id=image.id,
        original_path=image.original_path,
        file_path=image.file_path,
        mime_type=image.mime_type,
        status=image.status,
        is_deleted=image.is_deleted,
        is_favorite=image.is_favorite,
        is_watched=image.is_watched,
        is_reddit_saved=bool(getattr(image, "is_reddit_saved", False)),
        source_size=image.source_size,
        width=image.width,
        height=image.height,
        post_id=image.post_id,
        post_url=image.post_url,
        title=image.title,
        detected_at=image.detected_at,
        processed_at=image.processed_at,
        watched_at=image.watched_at,
        updated_at=image.updated_at,
        file_url=file_url,
        path_category=path_category,
        path_date=path_date,
    )


@router.get("/feed/images", response_model=ImageFeedResponse)
def get_image_feed(
    request: Request,
    cursor: str | None = Query(default=None),
    page_size: int = Query(default=15, ge=1, le=100),
    random_order: bool = Query(default=False),
    only_favorites: bool = Query(default=False),
    only_watched: bool = Query(default=False),
    only_reddit_saved: bool = Query(default=False),
    order_by: Literal["processed_desc", "detected_desc", "favorite_desc"] = Query(default="detected_desc"),
    category: str | None = Query(default=None),
    include_total: bool = Query(default=True),
    session: Session = Depends(get_session),
) -> ImageFeedResponse:
    blacklisted_categories = load_blacklisted_categories(session)
    if not only_reddit_saved and normalize_category_name(category or "") in blacklisted_categories:
        return ImageFeedResponse(items=[], next_cursor=None, total=0 if include_total else None)

    items, next_cursor, total = load_image_feed(
        session,
        cursor=cursor,
        page_size=page_size,
        category=None if only_reddit_saved else category,
        blacklisted_categories=blacklisted_categories,
        only_favorites=only_favorites,
        only_watched=only_watched,
        only_reddit_saved=only_reddit_saved,
        order_by=order_by,
        random_order=random_order,
        include_total=include_total,
    )
    return ImageFeedResponse(
        items=[_image_to_schema(item, request) for item in items],
        next_cursor=next_cursor,
        total=total,
    )


@router.get("/feed/categories/{category}/media-counts", response_model=CategoryMediaCountsOut)
def get_category_media_counts(
    category: str,
    session: Session = Depends(get_session),
) -> CategoryMediaCountsOut:
    normalized_category = normalize_category_name(category)
    blacklisted_categories = load_blacklisted_categories(session)
    if not normalized_category or normalized_category in blacklisted_categories:
        return CategoryMediaCountsOut(category=category, videos=0, images=0)

    video_count = count_category_videos(
        session,
        category=category,
        blacklisted_categories=blacklisted_categories,
    )
    image_count = count_category_images(
        session,
        category=category,
        blacklisted_categories=blacklisted_categories,
    )
    return CategoryMediaCountsOut(category=category, videos=video_count, images=image_count)


@router.get("/feed/categories/{category}/progress", response_model=CategoryProgressOut)
def get_category_progress(
    category: str,
    session: Session = Depends(get_session),
) -> CategoryProgressOut:
    normalized_category = normalize_category_name(category)
    blacklisted_categories = load_blacklisted_categories(session)
    if not normalized_category or normalized_category in blacklisted_categories:
        return CategoryProgressOut(
            category=category,
            total=0,
            watched=0,
            videos=0,
            images=0,
            videos_watched=0,
            images_watched=0,
        )

    video_count = count_category_videos(
        session,
        category=category,
        blacklisted_categories=blacklisted_categories,
    )
    image_count = count_category_images(
        session,
        category=category,
        blacklisted_categories=blacklisted_categories,
    )
    videos_watched = count_category_watched_videos(
        session,
        category=category,
        blacklisted_categories=blacklisted_categories,
    )
    images_watched = count_category_watched_images(
        session,
        category=category,
        blacklisted_categories=blacklisted_categories,
    )
    return CategoryProgressOut(
        category=category,
        total=video_count + image_count,
        watched=videos_watched + images_watched,
        videos=video_count,
        images=image_count,
        videos_watched=videos_watched,
        images_watched=images_watched,
    )


@router.get("/images/{image_id}/file", name="get_image_file")
def get_image_file(image_id: int, session: Session = Depends(get_session)) -> FileResponse:
    image = session.get(RedditImage, image_id)
    if not image or image.is_deleted or image.status != "ready":
        raise HTTPException(status_code=404, detail="Image not found")
    file_path = Path(image.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Image file missing")
    media_type = image.mime_type or "application/octet-stream"
    return FileResponse(file_path, media_type=media_type)


@router.put("/images/{image_id}/favorite", response_model=FavoriteUpdateOut)
def update_image_favorite(
    image_id: int,
    payload: FavoriteUpdateIn,
    request: Request,
    session: Session = Depends(get_session),
) -> FavoriteUpdateOut:
    image = session.get(RedditImage, image_id)
    if not image or image.is_deleted:
        raise HTTPException(status_code=404, detail="Image not found")

    image.is_favorite = payload.is_favorite
    image.favorite_at = utcnow() if payload.is_favorite else None
    minio_sync = getattr(request.app.state, "minio_sync_service", None)
    if minio_sync is not None:
        minio_sync.schedule_favorite(session, "image", image, payload.is_favorite)
    else:
        session.commit()
    return FavoriteUpdateOut(success=True, video_id=image.id, is_favorite=image.is_favorite)


@router.put("/images/{image_id}/watched", response_model=WatchedUpdateOut)
def update_image_watched(
    image_id: int,
    payload: WatchedUpdateIn,
    session: Session = Depends(get_session),
) -> WatchedUpdateOut:
    image = session.get(RedditImage, image_id)
    if not image or image.is_deleted:
        raise HTTPException(status_code=404, detail="Image not found")

    image.is_watched = payload.is_watched
    image.watched_at = utcnow() if payload.is_watched else None
    session.commit()
    return WatchedUpdateOut(
        success=True,
        video_id=image.id,
        is_watched=image.is_watched,
        watched_ratio=None,
    )


@router.get("/feed/categories", response_model=list[CategoryCountOut])
def get_feed_categories(request: Request, session: Session = Depends(get_session)) -> list[CategoryCountOut]:
    blacklisted_categories = load_blacklisted_categories(session)
    covers: CategoryCoverService = request.app.state.category_cover_service  # type: ignore[assignment]
    results: list[CategoryCountOut] = []
    for item in list_category_summaries(session, blacklisted_categories):
        cover_version = covers.cover_version(item.category)
        results.append(
            CategoryCountOut(
                category=item.category,
                count=item.count,
                video_count=int(getattr(item, "video_count", 0) or 0),
                image_count=int(getattr(item, "image_count", 0) or 0),
                favorites_count=item.favorites_count,
                watched_count=item.watched_count,
                cover_url=(
                    _category_cover_url(request, item.category, cover_version)
                    if cover_version is not None
                    else None
                ),
            )
        )
    return results


@router.get("/feed/categories/{category}/cover", name="get_category_cover")
def get_category_cover(category: str, request: Request) -> FileResponse:
    covers: CategoryCoverService = request.app.state.category_cover_service  # type: ignore[assignment]
    cover_path = covers.cover_path(category)
    if cover_path is None or not cover_path.exists():
        raise HTTPException(status_code=404, detail="Cover not found")
    return FileResponse(
        cover_path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/gallery/download-list", response_model=GalleryDownloadListOut)
def get_gallery_download_list(session: Session = Depends(get_session)) -> GalleryDownloadListOut:
    items = [
        GalleryDownloadListItemOut(**item)
        for item in list_gallery_download_list_items(session)
    ]
    return GalleryDownloadListOut(items=items, total=len(items))


@router.post("/gallery/download-list", response_model=GalleryDownloadListAddOut)
def post_gallery_download_list_item(
    payload: GalleryDownloadListAddIn,
    session: Session = Depends(get_session),
) -> GalleryDownloadListAddOut:
    blacklisted_categories = load_blacklisted_categories(session)
    try:
        item, created = add_gallery_download_list_item(
            session,
            payload.name,
            payload.media_type,
            blacklisted_categories=blacklisted_categories,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    parsed_item = GalleryDownloadListItemOut(**item)
    if created:
        return GalleryDownloadListAddOut(
            added=True,
            item=parsed_item,
            message="Galeria adicionada a lista de downloads.",
        )
    return GalleryDownloadListAddOut(
        added=False,
        item=parsed_item,
        message="Galeria ja estava na lista de downloads.",
    )


@router.delete("/gallery/download-list/{name}", response_model=GalleryDownloadListRemoveOut)
def delete_gallery_download_list_item(
    name: str,
    media_type: Literal["images", "videos"] = Query(default="images"),
    session: Session = Depends(get_session),
) -> GalleryDownloadListRemoveOut:
    removed = remove_gallery_download_list_item(session, name, media_type)
    return GalleryDownloadListRemoveOut(success=True, removed=removed)


@router.get("/channels", response_model=list[ChannelOut])
def get_channels(request: Request, session: Session = Depends(get_session)) -> list[ChannelOut]:
    blacklisted_categories = load_blacklisted_categories(session)
    summaries = list_category_summaries(session, blacklisted_categories)
    return [_category_summary_to_channel(summary, request) for summary in summaries]


@router.get("/channels/master.m3u", response_class=PlainTextResponse)
def get_master_channels_playlist(request: Request, session: Session = Depends(get_session)) -> PlainTextResponse:
    channels = get_channels(request, session)
    if not channels:
        raise HTTPException(status_code=404, detail="No channels available")
    playlist = _build_master_channels_m3u(request, channels)
    return PlainTextResponse(
        playlist,
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": 'attachment; filename="medialibrary-master.m3u"'},
    )


@router.get("/channels/vod.m3u", response_class=PlainTextResponse)
def get_vod_playlist_m3u(
    request: Request,
    category: str | None = Query(default=None),
    order_by: Literal["processed_desc", "detected_desc", "favorite_desc"] = Query(default="detected_desc"),
    limit: int = Query(default=5000, ge=1, le=20000),
    session: Session = Depends(get_session),
) -> PlainTextResponse:
    requested_category = str(category or "").strip() or None
    blacklisted_categories = load_blacklisted_categories(session)
    videos = load_vod_videos(
        session,
        category=requested_category,
        order_by=order_by,
        limit=limit,
        blacklisted_categories=blacklisted_categories,
    )
    if not videos:
        raise HTTPException(status_code=404, detail="No VOD videos available")

    safe_name = "medialibrary-vod"
    if requested_category:
        normalized_category = normalize_category_name(requested_category).replace("/", "-").replace("\\", "-")
        if normalized_category:
            safe_name = f"{safe_name}-{normalized_category}"
    playlist = _build_vod_playlist_m3u(request, videos)
    return PlainTextResponse(
        playlist,
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.m3u"'},
    )


@router.get("/channels/playlist.m3u", response_class=PlainTextResponse)
def get_channel_playlist_m3u(
    request: Request,
    category: str = Query(..., min_length=1),
    order_by: Literal["processed_desc", "detected_desc", "favorite_desc"] = Query(default="detected_desc"),
    limit: int = Query(default=300, ge=1, le=2000),
    session: Session = Depends(get_session),
) -> PlainTextResponse:
    normalized_category = category.strip()
    if not normalized_category:
        raise HTTPException(status_code=400, detail="Category is required")

    blacklisted_categories = load_blacklisted_categories(session)
    videos = load_channel_videos(
        session,
        normalized_category,
        order_by=order_by,
        limit=limit,
        blacklisted_categories=blacklisted_categories,
    )
    if not videos:
        raise HTTPException(status_code=404, detail="Channel not found or empty")

    safe_name = normalize_category_name(normalized_category).replace("/", "-").replace("\\", "-") or "channel"
    playlist = _build_category_playlist_m3u(request, normalized_category, videos)
    return PlainTextResponse(
        playlist,
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.m3u"'},
    )


@router.get("/channels/{channel_key}/hls/index.m3u8")
def get_channel_live_manifest(
    channel_key: str,
    request: Request,
    session: Session = Depends(get_session),
):
    blacklisted_categories = load_blacklisted_categories(session)
    summary = find_category_summary_by_key(session, channel_key, blacklisted_categories)
    if not summary:
        raise HTTPException(status_code=404, detail="Channel not found")

    channel_runtime: ChannelRuntimeService = request.app.state.channel_runtime_service  # type: ignore[assignment]
    videos = load_channel_videos(
        session,
        summary.category,
        order_by="detected_desc",
        limit=channel_runtime.settings.channel_hls_source_limit,
        blacklisted_categories=blacklisted_categories,
    )
    if not videos:
        raise HTTPException(status_code=404, detail="Channel has no playable videos")

    try:
        manifest_path = channel_runtime.ensure_channel(summary.channel_key, summary.category, videos)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unable to prepare live channel: {exc}") from exc

    return FileResponse(
        manifest_path,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.get("/channels/{channel_key}/hls/{filename}")
def get_channel_live_asset(
    channel_key: str,
    filename: str,
    request: Request,
):
    if filename == "index.m3u8":
        raise HTTPException(status_code=404, detail="Manifest route is /index.m3u8")
    channel_runtime: ChannelRuntimeService = request.app.state.channel_runtime_service  # type: ignore[assignment]
    try:
        asset_path = channel_runtime.resolve_output_path(channel_key, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not asset_path.exists() or not asset_path.is_file():
        raise HTTPException(status_code=404, detail="Channel asset not found")

    media_type = "video/mp2t"
    if asset_path.suffix.lower() == ".m3u8":
        media_type = "application/vnd.apple.mpegurl"
    return FileResponse(
        asset_path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=10"},
    )


@router.get("/jobs", response_model=list[JobOut])
def get_jobs(request: Request, session: Session = Depends(get_session), limit: int = Query(100, ge=1, le=500)) -> list[JobOut]:
    jobs: JobService = request.app.state.job_service  # type: ignore[assignment]
    rows = jobs.list_jobs(session, limit=limit)
    return [
        JobOut(
            id=j.id,
            type=j.type,
            status=j.status,
            video_id=j.video_id,
            attempts=j.attempts,
            max_attempts=j.max_attempts,
            error_message=j.error_message,
            created_at=j.created_at,
            started_at=j.started_at,
            finished_at=j.finished_at,
            payload=jobs.decode_payload(j.payload),
        )
        for j in rows
    ]


@router.get("/stats", response_model=StatsOut)
def get_stats(request: Request, session: Session = Depends(get_session)) -> StatsOut:
    worker: JobWorker = request.app.state.job_worker  # type: ignore[assignment]
    jobs: JobService = request.app.state.job_service  # type: ignore[assignment]
    queue_stats = worker.queue_stats(session)
    ready_storage_bytes = _ready_storage_bytes(session)
    images_storage_bytes = _images_storage_bytes(session)
    ready_image_filter = (
        RedditImage.is_deleted.is_(False),
        RedditImage.status == "ready",
    )
    ready_video_filter = (
        Video.is_deleted.is_(False),
        Video.status.in_(["ready", "duplicate"]),
    )
    total_duration_seconds = session.scalar(
        select(func.coalesce(func.sum(Video.duration_seconds), 0.0)).where(*ready_video_filter)
    )
    total_watched_seconds = session.scalar(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (
                            Video.duration_seconds.is_(None),
                            0.0,
                        ),
                        (
                            Video.watched_ratio.is_not(None),
                            Video.duration_seconds * Video.watched_ratio,
                        ),
                        (
                            Video.is_watched.is_(True),
                            Video.duration_seconds,
                        ),
                        else_=0.0,
                    )
                ),
                0.0,
            )
        ).where(*ready_video_filter)
    )
    current_category, current_folder, current_video_id = _current_processing_context(
        session=session,
        job_service=jobs,
    )
    return StatsOut(
        folders_total=session.scalar(select(func.count(Folder.id))) or 0,
        folders_active=session.scalar(select(func.count(Folder.id)).where(Folder.is_active.is_(True))) or 0,
        videos_total=session.scalar(select(func.count(Video.id)).where(Video.is_deleted.is_(False))) or 0,
        videos_ready=session.scalar(
            select(func.count(Video.id)).where(*ready_video_filter)
        )
        or 0,
        videos_total_duration_seconds=float(total_duration_seconds or 0.0),
        videos_total_watched_seconds=float(total_watched_seconds or 0.0),
        videos_with_audio=session.scalar(
            select(func.count(Video.id)).where(
                *ready_video_filter,
                Video.has_audio.is_(True),
            )
        )
        or 0,
        videos_without_audio=session.scalar(
            select(func.count(Video.id)).where(
                *ready_video_filter,
                Video.has_audio.is_(False),
            )
        )
        or 0,
        videos_watched=session.scalar(
            select(func.count(Video.id)).where(
                *ready_video_filter,
                Video.is_watched.is_(True),
            )
        )
        or 0,
        videos_watched_not_favorited=session.scalar(
            select(func.count(Video.id)).where(
                *ready_video_filter,
                Video.is_watched.is_(True),
                Video.is_favorite.is_(False),
            )
        )
        or 0,
        videos_favorites=session.scalar(
            select(func.count(Video.id)).where(
                *ready_video_filter,
                Video.is_favorite.is_(True),
            )
        )
        or 0,
        videos_reddit_saved=session.scalar(
            select(func.count(Video.id)).where(
                *ready_video_filter,
                Video.is_reddit_saved.is_(True),
            )
        )
        or 0,
        processing_current_category=current_category,
        processing_current_folder=current_folder,
        processing_current_video_id=current_video_id,
        ready_storage_bytes=ready_storage_bytes,
        images_total=session.scalar(select(func.count(RedditImage.id)).where(*ready_image_filter)) or 0,
        images_reddit_saved=session.scalar(
            select(func.count(RedditImage.id)).where(
                *ready_image_filter,
                RedditImage.is_reddit_saved.is_(True),
            )
        )
        or 0,
        images_storage_bytes=images_storage_bytes,
        videos_pending=session.scalar(
            select(func.count(Video.id)).where(Video.is_deleted.is_(False), Video.status.in_(["new", "pending"]))
        )
        or 0,
        videos_processing=session.scalar(
            select(func.count(Video.id)).where(Video.is_deleted.is_(False), Video.status == "processing")
        )
        or 0,
        videos_error=session.scalar(
            select(func.count(Video.id)).where(Video.is_deleted.is_(False), Video.status == "error")
        )
        or 0,
        jobs_pending=queue_stats["pending"],
        jobs_running=queue_stats["running"],
        jobs_failed=queue_stats["failed"],
    )


@router.get("/debug/ping")
def debug_ping() -> JSONResponse:
    return JSONResponse(content={"ok": True})


@router.get("/reddit/config-status", response_model=RedditConfigStatusOut)
def get_reddit_config_status(request: Request) -> RedditConfigStatusOut:
    reddit: RedditService = request.app.state.reddit_service  # type: ignore[assignment]
    missing = reddit.missing_config_fields()
    return RedditConfigStatusOut(
        configured=len(missing) == 0,
        missing=missing,
    )


@router.get("/reddit/subscriptions", response_model=RedditSubscriptionsOut)
async def get_reddit_subscriptions(
    request: Request,
    refresh: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> RedditSubscriptionsOut:
    reddit: RedditService = request.app.state.reddit_service  # type: ignore[assignment]
    reddit_download: RedditDownloadService = request.app.state.reddit_download_service  # type: ignore[assignment]
    blacklisted_categories = load_blacklisted_categories(session)
    selected = _load_selected_subreddits(session)
    downloaded = reddit_download.get_downloaded_subreddits()
    year_image_counts = reddit_download.get_user_year_image_counts_map()
    cached_subscriptions, cached_at = _load_reddit_subscriptions_cache(session)
    subscriptions: list[dict[str, object]]
    from_cache = False
    warning: str | None = None

    if not refresh:
        subscriptions = cached_subscriptions
        from_cache = True
        if not cached_subscriptions:
            warning = "Cache de assinaturas vazio. Clique em 'Buscar assinaturas' para atualizar."
    else:
        try:
            subscriptions = await reddit.list_subscribed_subreddits()
            cached_at = _save_reddit_subscriptions_cache(session, subscriptions)
        except RedditConfigError as exc:
            if cached_subscriptions:
                subscriptions = cached_subscriptions
                from_cache = True
                warning = f"Config Reddit invalida; exibindo cache: {exc}"
            else:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            if cached_subscriptions:
                subscriptions = cached_subscriptions
                from_cache = True
                warning = f"Falha ao atualizar assinaturas; exibindo cache: {exc}"
            else:
                raise HTTPException(status_code=502, detail=f"Falha ao carregar subreddits: {exc}") from exc

    items: list[RedditSubredditOut] = []
    for row in subscriptions:
        name = str(row.get("name") or "").strip()
        normalized = _normalize_subreddit_name(name).lower()
        if normalized in blacklisted_categories:
            continue
        year_count_verified = normalized in year_image_counts
        items.append(
            RedditSubredditOut(
                name=name,
                title=str(row.get("title") or ""),
                over18=bool(row.get("over18")),
                subscribers=int(row.get("subscribers") or 0),
                url=str(row.get("url") or ""),
                is_selected=normalized in selected,
                is_downloaded=normalized in downloaded,
                year_image_count=year_image_counts.get(normalized) if year_count_verified else None,
                year_image_count_verified=year_count_verified,
            )
        )
    items.sort(key=lambda item: (-item.subscribers, item.name.lower()))

    return RedditSubscriptionsOut(
        total=len(items),
        selected_total=sum(1 for item in items if item.is_selected),
        items=items,
        from_cache=from_cache,
        cached_at=cached_at,
        warning=warning,
    )


@router.put("/reddit/subscriptions/selection", response_model=RedditSelectionOut)
def put_reddit_subscriptions_selection(
    payload: RedditSelectionIn,
    session: Session = Depends(get_session),
) -> RedditSelectionOut:
    blacklisted_categories = load_blacklisted_categories(session)
    selected = _save_selected_subreddits(
        session,
        [
            name
            for name in payload.names
            if normalize_category_name(str(name)) not in blacklisted_categories
        ],
    )
    return RedditSelectionOut(
        success=True,
        selected_total=len(selected),
    )


@router.post("/reddit/download/start", response_model=RedditDownloadStartOut)
async def start_reddit_download(
    payload: RedditDownloadStartIn,
    request: Request,
    session: Session = Depends(get_session),
) -> RedditDownloadStartOut:
    reddit_download: RedditDownloadService = request.app.state.reddit_download_service  # type: ignore[assignment]
    blacklisted_categories = load_blacklisted_categories(session)

    selected_names = payload.names or sorted(_load_selected_subreddits(session))
    if not selected_names:
        raise HTTPException(status_code=400, detail="Nenhum subreddit selecionado para download.")

    selected_names = [
        name
        for name in selected_names
        if normalize_category_name(str(name)) not in blacklisted_categories
    ]
    if not selected_names:
        raise HTTPException(status_code=400, detail="Todos os subreddits selecionados estao na blacklist.")
    if payload.period_months is not None and len(selected_names) != 1:
        raise HTTPException(status_code=400, detail="Selecione exatamente 1 subreddit para baixar por periodo.")

    try:
        status = await reddit_download.start_download(
            subreddit_names=selected_names,
            per_subreddit_limit=payload.per_subreddit_limit,
            period_months=payload.period_months,
            media_type=payload.media_type,
        )
    except RedditConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except InsufficientDiskSpaceError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedditDownloadStartOut(
        started=True,
        task_id=status.get("task_id"),
        state=str(status.get("state") or "running"),
        message=str(status.get("message") or ""),
    )


@router.post("/reddit/saved/download/start", response_model=RedditDownloadStartOut)
async def start_reddit_saved_download(
    payload: RedditSavedDownloadStartIn,
    request: Request,
) -> RedditDownloadStartOut:
    reddit_download: RedditDownloadService = request.app.state.reddit_download_service  # type: ignore[assignment]
    try:
        status = await reddit_download.start_saved_download(media_type=payload.media_type)
    except RedditConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RedditApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except InsufficientDiskSpaceError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedditDownloadStartOut(
        started=True,
        task_id=status.get("task_id"),
        state=str(status.get("state") or "running"),
        message=str(status.get("message") or ""),
    )


@router.post("/reddit/download/start-empty-categories", response_model=RedditDownloadStartOut)
async def start_reddit_empty_categories_download(request: Request) -> RedditDownloadStartOut:
    reddit_download: RedditDownloadService = request.app.state.reddit_download_service  # type: ignore[assignment]

    try:
        status = await reddit_download.start_download_for_empty_categories()
    except RedditConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except InsufficientDiskSpaceError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedditDownloadStartOut(
        started=True,
        task_id=status.get("task_id"),
        state=str(status.get("state") or "running"),
        message=str(status.get("message") or ""),
    )


@router.post("/reddit/download/start-empty-user-images", response_model=RedditDownloadStartOut)
async def start_reddit_empty_user_images_download(
    request: Request,
    subreddit_limit: int = Query(default=10, ge=1, le=500),
) -> RedditDownloadStartOut:
    reddit_download: RedditDownloadService = request.app.state.reddit_download_service  # type: ignore[assignment]

    try:
        status = await reddit_download.start_download_for_empty_user_images(
            subreddit_limit=subreddit_limit,
            per_subreddit_limit=10,
        )
    except RedditConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except InsufficientDiskSpaceError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedditDownloadStartOut(
        started=True,
        task_id=status.get("task_id"),
        state=str(status.get("state") or "running"),
        message=str(status.get("message") or ""),
    )


def _build_reddit_download_status_out(status: dict[str, object]) -> RedditDownloadStatusOut:
    raw_counts = status.get("user_year_image_counts") or {}
    user_year_image_counts: dict[str, int] = {}
    if isinstance(raw_counts, dict):
        for key, value in raw_counts.items():
            try:
                user_year_image_counts[str(key)] = max(0, int(value))
            except (TypeError, ValueError):
                continue
    raw_scan = status.get("image_count_scan") or {}
    scan_payload = raw_scan if isinstance(raw_scan, dict) else {}
    raw_saved_sync = status.get("saved_sync")
    saved_sync = None
    if isinstance(raw_saved_sync, dict):
        saved_sync = RedditSavedSyncStatsOut(
            listing_items=int(raw_saved_sync.get("listing_items") or 0),
            posts=int(raw_saved_sync.get("posts") or 0),
            videos_found=int(raw_saved_sync.get("videos_found") or 0),
            images_found=int(raw_saved_sync.get("images_found") or 0),
            already_marked=int(raw_saved_sync.get("already_marked") or 0),
            api_limit_likely_reached=bool(raw_saved_sync.get("api_limit_likely_reached")),
        )
    return RedditDownloadStatusOut(
        task_id=status.get("task_id"),
        auto_enabled=bool(status.get("auto_enabled", True)),
        auto_image_enabled=bool(status.get("auto_image_enabled", True)),
        idle_image_count_enabled=bool(status.get("idle_image_count_enabled", False)),
        state=str(status.get("state") or "idle"),
        message=str(status.get("message") or ""),
        media_type=str(status.get("media_type") or "videos"),
        trigger=str(status.get("trigger") or "").strip() or None,
        started_at=status.get("started_at"),
        finished_at=status.get("finished_at"),
        total_subreddits=int(status.get("total_subreddits") or 0),
        completed_subreddits=int(status.get("completed_subreddits") or 0),
        total_videos_planned=int(status.get("total_videos_planned") or 0),
        processed_videos=int(status.get("processed_videos") or 0),
        failed_videos=int(status.get("failed_videos") or 0),
        items=list(status.get("items") or []),
        logs=[str(line) for line in list(status.get("logs") or [])],
        user_year_image_counts=user_year_image_counts,
        image_count_scan={
            "active": bool(scan_payload.get("active")),
            "current": str(scan_payload.get("current") or "").strip() or None,
            "message": str(scan_payload.get("message") or "").strip() or None,
        },
        saved_sync=saved_sync,
    )


@router.post("/reddit/download/stop", response_model=RedditDownloadStatusOut)
async def stop_reddit_download(request: Request) -> RedditDownloadStatusOut:
    reddit_download: RedditDownloadService = request.app.state.reddit_download_service  # type: ignore[assignment]
    status = await reddit_download.stop_download()
    return _build_reddit_download_status_out(status)


@router.put("/reddit/download/auto-enabled", response_model=RedditDownloadStatusOut)
def update_reddit_auto_download_enabled(
    payload: RedditAutoDownloadUpdateIn,
    request: Request,
) -> RedditDownloadStatusOut:
    reddit_download: RedditDownloadService = request.app.state.reddit_download_service  # type: ignore[assignment]
    reddit_download.set_auto_download_enabled(payload.enabled)
    return _build_reddit_download_status_out(reddit_download.get_status())


@router.put("/reddit/download/auto-image-enabled", response_model=RedditDownloadStatusOut)
async def update_reddit_auto_image_download_enabled(
    payload: RedditAutoDownloadUpdateIn,
    request: Request,
) -> RedditDownloadStatusOut:
    reddit_download: RedditDownloadService = request.app.state.reddit_download_service  # type: ignore[assignment]
    reddit_download.set_auto_image_download_enabled(payload.enabled)
    if payload.enabled:
        # Fire-and-forget so the admin checkbox stays responsive while Reddit listing runs.
        status = reddit_download.schedule_auto_image_download_now()
        return _build_reddit_download_status_out(status)
    return _build_reddit_download_status_out(reddit_download.get_status())


@router.post("/reddit/image-count/start", response_model=RedditDownloadStatusOut)
async def start_reddit_image_count_scan(request: Request) -> RedditDownloadStatusOut:
    reddit_download: RedditDownloadService = request.app.state.reddit_download_service  # type: ignore[assignment]
    try:
        status = await reddit_download.start_image_count_scan()
    except RedditConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _build_reddit_download_status_out(status)


@router.put("/reddit/idle-image-count/enabled", response_model=RedditDownloadStatusOut)
def update_reddit_idle_image_count_enabled(
    payload: RedditIdleImageCountUpdateIn,
    request: Request,
) -> RedditDownloadStatusOut:
    # Mantido por compatibilidade; a contagem passou a ser manual via POST /reddit/image-count/start.
    reddit_download: RedditDownloadService = request.app.state.reddit_download_service  # type: ignore[assignment]
    reddit_download.set_idle_image_count_enabled(False)
    return _build_reddit_download_status_out(reddit_download.get_status())


@router.get("/reddit/download/status", response_model=RedditDownloadStatusOut)
def get_reddit_download_status(request: Request) -> RedditDownloadStatusOut:
    reddit_download: RedditDownloadService = request.app.state.reddit_download_service  # type: ignore[assignment]
    status = reddit_download.get_status()
    return _build_reddit_download_status_out(status)
