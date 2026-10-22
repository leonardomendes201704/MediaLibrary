from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(_BACKEND_ENV), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "dev"
    backend_host: str = "0.0.0.0"
    backend_port: int = 8077
    database_url: str = "sqlite:///./media_library.db"
    media_output_dir: Path = Path("./media_optimized")
    thumbnail_dir: Path = Path("./media_thumbs")
    category_cover_dir: Path = Path("./media_category_covers")
    folder_watcher_enabled: bool = False
    watched_cleanup_enabled: bool = False
    watcher_poll_seconds: int = 4
    file_stable_seconds: int = 8
    full_scan_seconds: int = 45
    stable_checks_required: int = 2
    job_workers: int = 2

    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    max_feed_page_size: int = 50
    default_feed_page_size: int = 15

    transcode_max_width: int = 1920
    transcode_max_height: int = 1080
    transcode_compatible_bitrate_limit: int = 7_500_000
    transcode_video_maxrate: str = "4500k"
    transcode_video_bufsize: str = "9000k"
    transcode_audio_bitrate: str = "128k"
    transcode_preset: str = "superfast"
    transcode_crf: int = 22
    transcode_threads: int = 0
    delete_source_after_transcode: bool = True

    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_username: str | None = None
    reddit_password: str | None = None
    reddit_user_agent: str = "media-library-backend/1.0 by u/local"
    reddit_timeout_seconds: int = 25
    reddit_download_dir: Path = Path("./reddit_downloads")

    minio_endpoint: str = ""
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "medialibrary"
    minio_region: str = "us-east-1"
    minio_secure: bool = True
    channel_hls_dir: Path = Path("./channel_hls")
    video_vod_dir: Path = Path("./video_vod")
    channel_hls_time_seconds: int = 4
    channel_hls_list_size: int = 6
    channel_hls_width: int = 1280
    channel_hls_height: int = 720
    channel_hls_frame_rate: int = 30
    channel_hls_audio_bitrate: str = "128k"
    channel_hls_preset: str = "veryfast"
    channel_hls_idle_seconds: int = 180
    channel_hls_startup_timeout_seconds: int = 20
    channel_hls_source_limit: int = 500

    allowed_video_extensions: tuple[str, ...] = (
        ".mp4",
        ".mkv",
        ".mov",
        ".avi",
        ".webm",
        ".m4v",
        ".ts",
        ".m2ts",
    )

    @property
    def minio_enabled(self) -> bool:
        return bool(
            self.minio_endpoint.strip()
            and self.minio_access_key.strip()
            and self.minio_secret_key.strip()
            and self.minio_bucket.strip()
        )

    def ensure_dirs(self) -> None:
        self.media_output_dir.mkdir(parents=True, exist_ok=True)
        self.thumbnail_dir.mkdir(parents=True, exist_ok=True)
        self.category_cover_dir.mkdir(parents=True, exist_ok=True)
        self.reddit_download_dir.mkdir(parents=True, exist_ok=True)
        self.channel_hls_dir.mkdir(parents=True, exist_ok=True)
        self.video_vod_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
