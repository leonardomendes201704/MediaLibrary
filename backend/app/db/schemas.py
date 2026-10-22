from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class FolderCreate(BaseModel):
    path: str
    is_active: bool = True
    serve_files: bool = True
    scan_subdirs: bool = True
    rules_json: str | None = None


class FolderUpdate(BaseModel):
    path: str | None = None
    is_active: bool | None = None
    serve_files: bool | None = None
    scan_subdirs: bool | None = None
    rules_json: str | None = None


class FolderOut(BaseModel):
    id: int
    path: str
    is_active: bool
    serve_files: bool
    scan_subdirs: bool
    rules_json: str | None
    last_scan_at: datetime | None
    created_at: datetime
    updated_at: datetime
    video_count: int = 0
    ready_count: int = 0
    pending_count: int = 0
    error_count: int = 0


class VideoOut(BaseModel):
    id: int
    folder_id: int | None
    original_path: str
    derived_path: str | None
    thumbnail_path: str | None
    status: str
    is_deleted: bool
    is_favorite: bool
    is_watched: bool
    is_reddit_saved: bool = False
    duplicate_of_video_id: int | None
    duration_seconds: float | None
    width: int | None
    height: int | None
    video_codec: str | None
    has_audio: bool | None = None
    audio_codec: str | None
    container: str | None
    bitrate: int | None
    source_size: int | None
    processing_error: str | None
    detected_at: datetime
    processed_at: datetime | None
    watched_at: datetime | None
    updated_at: datetime
    stream_url: str | None = None
    thumbnail_url: str | None = None
    path_category: str | None = None
    path_date: str | None = None


class VideoListResponse(BaseModel):
    items: list[VideoOut]
    total: int
    page: int
    page_size: int


class FeedResponse(BaseModel):
    items: list[VideoOut]
    next_cursor: str | None


class ImageOut(BaseModel):
    id: int
    original_path: str
    file_path: str
    mime_type: str | None = None
    status: str
    is_deleted: bool
    is_favorite: bool
    is_watched: bool
    is_reddit_saved: bool = False
    source_size: int | None = None
    width: int | None = None
    height: int | None = None
    post_id: str | None = None
    post_url: str | None = None
    title: str | None = None
    detected_at: datetime
    processed_at: datetime | None = None
    watched_at: datetime | None = None
    updated_at: datetime
    file_url: str | None = None
    path_category: str | None = None
    path_date: str | None = None


class ImageFeedResponse(BaseModel):
    items: list[ImageOut]
    next_cursor: str | None
    total: int | None = None


class CategoryMediaCountsOut(BaseModel):
    category: str
    videos: int
    images: int


class CategoryProgressOut(BaseModel):
    category: str
    total: int
    watched: int
    videos: int
    images: int
    videos_watched: int
    images_watched: int


class CategoryCountOut(BaseModel):
    category: str
    count: int
    video_count: int = 0
    image_count: int = 0
    favorites_count: int = 0
    watched_count: int = 0
    cover_url: str | None = None


class GalleryDownloadListItemOut(BaseModel):
    name: str
    media_type: Literal["images", "videos"]
    added_at: datetime
    gallery_url: str


class GalleryDownloadListOut(BaseModel):
    items: list[GalleryDownloadListItemOut] = Field(default_factory=list)
    total: int = 0


class GalleryDownloadListAddIn(BaseModel):
    name: str
    media_type: Literal["images", "videos"] = "images"


class GalleryDownloadListAddOut(BaseModel):
    success: bool = True
    added: bool
    item: GalleryDownloadListItemOut
    message: str = ""


class GalleryDownloadListRemoveOut(BaseModel):
    success: bool
    removed: bool


class ChannelOut(BaseModel):
    category: str
    channel_key: str
    video_count: int
    image_count: int = 0
    favorites_count: int = 0
    watched_count: int = 0
    is_user_category: bool = False
    gallery_url: str
    play_url: str
    feed_url: str
    playlist_url: str
    live_url: str


class ReprocessResponse(BaseModel):
    success: bool
    job_id: int


class DeleteResponse(BaseModel):
    success: bool


class CategoryDeleteIn(BaseModel):
    category: str


class CategoryDeleteOut(BaseModel):
    success: bool
    category: str
    deleted_videos: int
    deleted_images: int = 0
    queued: bool = False


class CategoryCoverIn(BaseModel):
    # Playback position of the frame to use; omitted means the first frame.
    position_seconds: float | None = Field(default=None, ge=0)


class CategoryCoverOut(BaseModel):
    success: bool
    category: str
    cover_url: str | None = None
    position_seconds: float | None = None


class CategoryBlacklistOut(BaseModel):
    success: bool
    category: str
    deleted_videos: int
    removed_from_selection: bool = False
    queued: bool = False


class VideoCleanupOut(BaseModel):
    success: bool
    deleted_videos: int


class ImageCleanupOut(BaseModel):
    success: bool
    deleted_images: int


class LibraryResetOut(BaseModel):
    success: bool
    deleted_videos: int
    deleted_images: int = 0
    deleted_jobs: int
    deleted_error_logs: int
    deleted_feed_states: int
    deleted_reddit_history: int
    deleted_files: int
    cleared_runtime_config_keys: int


class AudioBackfillOut(BaseModel):
    success: bool
    scanned_videos: int
    updated_videos: int
    videos_with_audio: int
    videos_without_audio: int
    failed_videos: int
    remaining_videos: int


class SlideshowOut(BaseModel):
    success: bool
    video_id: int
    category: str
    images_available: int
    images_used: int
    interval_seconds: float
    duration_seconds: float
    max_duration_seconds: float
    source_path: str
    gallery_url: str


class ControlStatusOut(BaseModel):
    state: str
    worker_running: bool
    worker_paused: bool
    watcher_running: bool
    watcher_paused: bool


class FavoriteUpdateIn(BaseModel):
    is_favorite: bool


class FavoriteUpdateOut(BaseModel):
    success: bool
    video_id: int
    is_favorite: bool


class WatchedUpdateIn(BaseModel):
    is_watched: bool
    watched_ratio: float | None = None


class WatchedUpdateOut(BaseModel):
    success: bool
    video_id: int
    is_watched: bool
    watched_ratio: float | None = None


class JobOut(BaseModel):
    id: int
    type: str
    status: str
    video_id: int | None
    attempts: int
    max_attempts: int
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    payload: dict[str, Any] | None = None


class StatsOut(BaseModel):
    folders_total: int
    folders_active: int
    videos_total: int
    videos_ready: int
    videos_total_duration_seconds: float
    videos_total_watched_seconds: float
    videos_with_audio: int
    videos_without_audio: int
    videos_watched: int
    videos_watched_not_favorited: int
    videos_favorites: int
    videos_reddit_saved: int = 0
    processing_current_category: str | None
    processing_current_folder: str | None
    processing_current_video_id: int | None
    ready_storage_bytes: int
    images_total: int = 0
    images_reddit_saved: int = 0
    images_storage_bytes: int = 0
    videos_pending: int
    videos_processing: int
    videos_error: int
    jobs_pending: int
    jobs_running: int
    jobs_failed: int


class ConfigOut(BaseModel):
    values: dict[str, str]


class ConfigIn(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


class RedditConfigStatusOut(BaseModel):
    configured: bool
    missing: list[str] = Field(default_factory=list)


class RedditSubredditOut(BaseModel):
    name: str
    title: str
    over18: bool
    subscribers: int
    url: str
    is_selected: bool = False
    is_downloaded: bool = False
    year_image_count: int | None = None
    year_image_count_verified: bool = False


class RedditSubscriptionsOut(BaseModel):
    total: int
    selected_total: int
    items: list[RedditSubredditOut] = Field(default_factory=list)
    from_cache: bool = False
    cached_at: datetime | None = None
    warning: str | None = None


class RedditSelectionIn(BaseModel):
    names: list[str] = Field(default_factory=list)


class RedditSelectionOut(BaseModel):
    success: bool
    selected_total: int


class RedditDownloadStartIn(BaseModel):
    names: list[str] = Field(default_factory=list)
    per_subreddit_limit: int | None = 10
    period_months: int | None = None
    media_type: Literal["videos", "images", "both"] = "videos"


class RedditSavedDownloadStartIn(BaseModel):
    media_type: Literal["videos", "images", "both"] = "both"


class RedditDownloadStartOut(BaseModel):
    started: bool
    task_id: str | None = None
    state: str
    message: str | None = None


class RedditSavedSyncStatsOut(BaseModel):
    listing_items: int = 0
    posts: int = 0
    videos_found: int = 0
    images_found: int = 0
    already_marked: int = 0
    api_limit_likely_reached: bool = False


class RedditAutoDownloadUpdateIn(BaseModel):
    enabled: bool


class RedditIdleImageCountUpdateIn(BaseModel):
    enabled: bool


class RedditDownloadItemOut(BaseModel):
    name: str
    status: str
    planned: int = 0
    completed: int = 0
    failed: int = 0
    marked_downloaded: bool = False


class RedditImageCountScanOut(BaseModel):
    active: bool = False
    current: str | None = None
    message: str | None = None


class RedditDownloadStatusOut(BaseModel):
    task_id: str | None = None
    auto_enabled: bool = True
    auto_image_enabled: bool = True
    idle_image_count_enabled: bool = False
    state: str
    message: str | None = None
    media_type: str = "videos"
    trigger: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    total_subreddits: int = 0
    completed_subreddits: int = 0
    total_videos_planned: int = 0
    processed_videos: int = 0
    failed_videos: int = 0
    items: list[RedditDownloadItemOut] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
    user_year_image_counts: dict[str, int] = Field(default_factory=dict)
    image_count_scan: RedditImageCountScanOut = Field(default_factory=RedditImageCountScanOut)
    saved_sync: RedditSavedSyncStatsOut | None = None
