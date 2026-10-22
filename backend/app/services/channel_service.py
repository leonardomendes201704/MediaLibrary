import hashlib
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import Folder, RedditImage, Video
from app.services.category_blacklist import extract_path_badges, normalize_category_name
from app.services.feed_service import load_feed


@dataclass(slots=True)
class CategorySummary:
    category: str
    channel_key: str
    count: int
    video_count: int = 0
    image_count: int = 0
    favorites_count: int = 0
    watched_count: int = 0
    is_user_category: bool = False
    latest_at: datetime | None = None


CHANNEL_KEY_SLUG_RE = re.compile(r"[^a-z0-9]+")
_CATEGORY_SUMMARIES_CACHE_TTL_SECONDS = 60.0
_category_summaries_cache: dict[frozenset[str], tuple[float, list["CategorySummary"]]] = {}


def invalidate_category_summaries_cache() -> None:
    _category_summaries_cache.clear()


def is_user_category_name(name: str) -> bool:
    return name.strip().lower().startswith("u_")


def channel_key_for_category(category: str) -> str:
    normalized = normalize_category_name(category)
    if not normalized:
        return ""
    slug = CHANNEL_KEY_SLUG_RE.sub("-", normalized).strip("-")
    slug = slug[:48] if slug else "channel"
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def _normalize_blacklisted_categories(blacklisted_categories: set[str] | None) -> set[str]:
    normalized: set[str] = set()
    for name in blacklisted_categories or set():
        normalized_name = normalize_category_name(name)
        if normalized_name:
            normalized.add(normalized_name)
    return normalized


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _category_activity_at(
    sort_timestamp: datetime | None,
    processed_at: datetime | None,
    detected_at: datetime | None,
) -> datetime | None:
    candidates = [
        ts
        for ts in (
            _as_utc(sort_timestamp),
            _as_utc(processed_at),
            _as_utc(detected_at),
        )
        if ts is not None
    ]
    if not candidates:
        return None
    return max(candidates)


def list_category_summaries(
    session: Session,
    blacklisted_categories: set[str] | None = None,
) -> list[CategorySummary]:
    normalized_blacklist = _normalize_blacklisted_categories(blacklisted_categories)
    cache_key = frozenset(normalized_blacklist)
    now = time.monotonic()
    cached = _category_summaries_cache.get(cache_key)
    if cached and (now - cached[0]) < _CATEGORY_SUMMARIES_CACHE_TTL_SECONDS:
        return cached[1]

    rows = session.execute(
        select(
            Video.original_path,
            Video.is_favorite,
            Video.is_watched,
            Video.sort_timestamp,
            Video.processed_at,
            Video.detected_at,
        )
        .join(Folder, Video.folder_id == Folder.id, isouter=True)
        .where(
            Video.is_deleted.is_(False),
            Video.status.in_(["ready", "duplicate"]),
            or_(Folder.id.is_(None), Folder.serve_files.is_(True)),
        )
    )

    video_counts: Counter[str] = Counter()
    image_counts: Counter[str] = Counter()
    favorites_counts: Counter[str] = Counter()
    watched_counts: Counter[str] = Counter()
    display_names: dict[str, str] = {}
    latest_at: dict[str, datetime] = {}

    for original_path, is_favorite, is_watched, sort_timestamp, processed_at, detected_at in rows:
        category, _, _ = extract_path_badges(str(original_path or ""))
        display_name = str(category or "").strip()
        normalized_name = normalize_category_name(display_name)
        if not normalized_name or normalized_name in normalized_blacklist:
            continue

        video_counts[normalized_name] += 1
        if is_favorite:
            favorites_counts[normalized_name] += 1
        if is_watched:
            watched_counts[normalized_name] += 1
        display_names.setdefault(normalized_name, display_name)

        activity_at = _category_activity_at(sort_timestamp, processed_at, detected_at)
        if activity_at is not None:
            previous = latest_at.get(normalized_name)
            if previous is None or activity_at > previous:
                latest_at[normalized_name] = activity_at

    image_rows = session.execute(
        select(
            RedditImage.original_path,
            RedditImage.is_favorite,
            RedditImage.is_watched,
            RedditImage.sort_timestamp,
            RedditImage.processed_at,
            RedditImage.detected_at,
        ).where(
            RedditImage.is_deleted.is_(False),
            RedditImage.status == "ready",
        )
    )
    for original_path, is_favorite, is_watched, sort_timestamp, processed_at, detected_at in image_rows:
        category, _, _ = extract_path_badges(str(original_path or ""))
        display_name = str(category or "").strip()
        normalized_name = normalize_category_name(display_name)
        if not normalized_name or normalized_name in normalized_blacklist:
            continue

        image_counts[normalized_name] += 1
        if is_favorite:
            favorites_counts[normalized_name] += 1
        if is_watched:
            watched_counts[normalized_name] += 1
        display_names.setdefault(normalized_name, display_name)

        activity_at = _category_activity_at(sort_timestamp, processed_at, detected_at)
        if activity_at is not None:
            previous = latest_at.get(normalized_name)
            if previous is None or activity_at > previous:
                latest_at[normalized_name] = activity_at

    all_keys = set(video_counts) | set(image_counts)
    epoch = datetime.fromtimestamp(0, tz=timezone.utc)

    summaries = [
        CategorySummary(
            category=display_names[key],
            channel_key=channel_key_for_category(display_names[key]),
            count=int(video_counts[key] + image_counts[key]),
            video_count=int(video_counts[key]),
            image_count=int(image_counts[key]),
            favorites_count=favorites_counts[key],
            watched_count=watched_counts[key],
            is_user_category=is_user_category_name(display_names[key]),
            latest_at=latest_at.get(key),
        )
        for key in sorted(
            all_keys,
            key=lambda item: (
                -(latest_at.get(item, epoch).timestamp()),
                display_names[item].lower(),
            ),
        )
    ]
    _category_summaries_cache[cache_key] = (now, summaries)
    return summaries


def load_channel_videos(
    session: Session,
    category: str,
    *,
    order_by: Literal["processed_desc", "detected_desc", "favorite_desc"] = "detected_desc",
    limit: int = 300,
    blacklisted_categories: set[str] | None = None,
) -> list[Video]:
    normalized_category = normalize_category_name(category)
    normalized_blacklist = _normalize_blacklisted_categories(blacklisted_categories)
    if not normalized_category or normalized_category in normalized_blacklist:
        return []

    return _load_playlist_videos(
        session,
        order_by=order_by,
        limit=limit,
        category=category,
        blacklisted_categories=normalized_blacklist,
    )


def load_vod_videos(
    session: Session,
    *,
    order_by: Literal["processed_desc", "detected_desc", "favorite_desc"] = "detected_desc",
    limit: int = 5000,
    category: str | None = None,
    blacklisted_categories: set[str] | None = None,
) -> list[Video]:
    normalized_blacklist = _normalize_blacklisted_categories(blacklisted_categories)
    normalized_category = normalize_category_name(category or "")
    if category and (not normalized_category or normalized_category in normalized_blacklist):
        return []

    return _load_playlist_videos(
        session,
        order_by=order_by,
        limit=limit,
        category=category,
        blacklisted_categories=normalized_blacklist,
    )


def _load_playlist_videos(
    session: Session,
    *,
    order_by: Literal["processed_desc", "detected_desc", "favorite_desc"],
    limit: int,
    category: str | None,
    blacklisted_categories: set[str],
) -> list[Video]:
    remaining = max(1, int(limit))
    cursor: str | None = None
    items: list[Video] = []

    while remaining > 0:
        batch, next_cursor = load_feed(
            session,
            cursor=cursor,
            page_size=remaining,
            random_order=False,
            exclude_favorites=False,
            only_favorites=False,
            exclude_watched=False,
            only_watched=False,
            order_by=order_by,
            category=category,
            blacklisted_categories=blacklisted_categories,
        )
        if not batch:
            break

        items.extend(batch)
        remaining = limit - len(items)
        if not next_cursor or remaining <= 0:
            break
        cursor = next_cursor

    return items[:limit]


def find_category_summary_by_key(
    session: Session,
    channel_key: str,
    blacklisted_categories: set[str] | None = None,
) -> CategorySummary | None:
    normalized_key = str(channel_key or "").strip().lower()
    if not normalized_key:
        return None
    for summary in list_category_summaries(session, blacklisted_categories):
        if summary.channel_key.lower() == normalized_key:
            return summary
    return None
