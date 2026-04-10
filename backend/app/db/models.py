from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Folder(Base):
    __tablename__ = "folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    path: Mapped[str] = mapped_column(String(1024), unique=True, index=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    serve_files: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    scan_subdirs: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rules_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )

    videos: Mapped[list["Video"]] = relationship(back_populates="folder")


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("folders.id", ondelete="SET NULL"), nullable=True, index=True)
    original_path: Mapped[str] = mapped_column(String(2048), unique=True, nullable=False, index=True)
    derived_path: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    thumbnail_path: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="new", nullable=False, index=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    favorite_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    is_watched: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    watched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    watched_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    duplicate_of_video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id", ondelete="SET NULL"), nullable=True)
    source_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_mtime: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    video_codec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    has_audio: Mapped[bool | None] = mapped_column(Boolean, nullable=True, index=True)
    audio_codec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    container: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sort_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )

    folder: Mapped[Folder | None] = relationship(back_populates="videos")
    duplicate_of_video: Mapped["Video | None"] = relationship(remote_side="Video.id", uselist=False)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id", ondelete="SET NULL"), nullable=True, index=True)
    payload: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )

    video: Mapped[Video | None] = relationship()


class AppConfig(Base):
    __tablename__ = "app_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


class RedditMediaHistory(Base):
    __tablename__ = "reddit_media_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    video_url_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subreddit: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    post_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    post_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    video_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


class FeedState(Base):
    __tablename__ = "feed_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cursor: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


class ErrorLog(Base):
    __tablename__ = "error_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    context: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
