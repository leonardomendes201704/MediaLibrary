import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import Folder, Video
from app.services.category_blacklist import extract_path_badges, normalize_category_name
from app.services.feed_service import load_feed


@dataclass(slots=True)
class CategorySummary:
    category: str
    channel_key: str
    count: int
    favorites_count: int = 0
    watched_count: int = 0
    is_user_category: bool = False


CHANNEL_KEY_SLUG_RE = re.compile(r"[^a-z0-9]+")


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


def list_category_summaries(
    session: Session,
    blacklisted_categories: set[str] | None = None,
) -> list[CategorySummary]:
    normalized_blacklist = _normalize_blacklisted_categories(blacklisted_categories)
    rows = session.execute(
        select(Video.original_path, Video.is_favorite, Video.is_watched)
        .join(Folder, Video.folder_id == Folder.id, isouter=True)
        .where(
            Video.is_deleted.is_(False),
            Video.status.in_(["ready", "duplicate"]),
            or_(Folder.id.is_(None), Folder.serve_files.is_(True)),
        )
    )

    counts: Counter[str] = Counter()
    favorites_counts: Counter[str] = Counter()
    watched_counts: Counter[str] = Counter()
    display_names: dict[str, str] = {}

    for original_path, is_favorite, is_watched in rows:
        category, _ = extract_path_badges(str(original_path or ""))
        display_name = str(category or "").strip()
        normalized_name = normalize_category_name(display_name)
        if not normalized_name or normalized_name in normalized_blacklist:
            continue

        counts[normalized_name] += 1
        if is_favorite:
            favorites_counts[normalized_name] += 1
        if is_watched:
            watched_counts[normalized_name] += 1
        display_names.setdefault(normalized_name, display_name)

    return [
        CategorySummary(
            category=display_names[key],
            channel_key=channel_key_for_category(display_names[key]),
            count=count,
            favorites_count=favorites_counts[key],
            watched_count=watched_counts[key],
            is_user_category=is_user_category_name(display_names[key]),
        )
        for key, count in sorted(
            counts.items(),
            key=lambda item: (-item[1], display_names[item[0]].lower()),
        )
    ]


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
