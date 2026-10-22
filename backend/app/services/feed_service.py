from datetime import datetime, timezone

from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Folder, Video
from app.services.category_blacklist import normalize_category_name


def encode_cursor(sort_timestamp: datetime, video_id: int) -> str:
    return f"{sort_timestamp.isoformat()}|{video_id}"


def decode_cursor(cursor: str) -> tuple[datetime, int] | None:
    try:
        ts_raw, id_raw = cursor.split("|", maxsplit=1)
        return datetime.fromisoformat(ts_raw), int(id_raw)
    except Exception:  # noqa: BLE001
        return None


def encode_random_cursor(seed: int, rand_key: int, video_id: int) -> str:
    return f"r|{seed}|{rand_key}|{video_id}"


def decode_random_cursor(cursor: str) -> tuple[int, int, int] | None:
    try:
        kind, seed_raw, key_raw, id_raw = cursor.split("|", maxsplit=3)
        if kind != "r":
            return None
        return int(seed_raw), int(key_raw), int(id_raw)
    except Exception:  # noqa: BLE001
        return None


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _base_feed_stmt(
    exclude_favorites: bool,
    only_favorites: bool,
    exclude_watched: bool,
    only_watched: bool,
    category: str | None,
    blacklisted_categories: set[str] | None,
    *,
    only_reddit_saved: bool = False,
):
    stmt = (
        select(Video)
        .join(Folder, Video.folder_id == Folder.id, isouter=True)
        .where(
            Video.is_deleted.is_(False),
            Video.status.in_(["ready", "duplicate"]),
            or_(Folder.id.is_(None), Folder.serve_files.is_(True)),
        )
    )
    if only_favorites:
        stmt = stmt.where(Video.is_favorite.is_(True))
    elif exclude_favorites:
        stmt = stmt.where(Video.is_favorite.is_(False))
    if only_watched:
        stmt = stmt.where(Video.is_watched.is_(True))
    elif exclude_watched:
        stmt = stmt.where(Video.is_watched.is_(False))
    if only_reddit_saved:
        stmt = stmt.where(Video.is_reddit_saved.is_(True))
    path_lower = func.lower(Video.original_path)
    normalized_category = category.strip().lower() if category else ""
    if normalized_category and not only_reddit_saved:
        escaped_category = _escape_like(normalized_category)
        windows_pattern = f"%\\\\{escaped_category}\\\\videos\\\\%"
        unix_pattern = f"%/{escaped_category}/videos/%"
        stmt = stmt.where(
            or_(
                path_lower.like(windows_pattern, escape="\\"),
                path_lower.like(unix_pattern, escape="\\"),
            )
        )
    normalized_blacklist: set[str] = set()
    for name in blacklisted_categories or set():
        normalized_name = normalize_category_name(name)
        if normalized_name:
            normalized_blacklist.add(normalized_name)
    if normalized_blacklist:
        blocked_patterns = []
        for blocked_name in sorted(normalized_blacklist):
            escaped_blocked_name = _escape_like(blocked_name)
            blocked_patterns.extend(
                [
                    path_lower.like(f"%\\\\{escaped_blocked_name}\\\\videos\\\\%", escape="\\"),
                    path_lower.like(f"%/{escaped_blocked_name}/videos/%", escape="\\"),
                ]
            )
        stmt = stmt.where(not_(or_(*blocked_patterns)))
    return stmt


def load_feed(
    session: Session,
    cursor: str | None,
    page_size: int,
    random_order: bool = False,
    exclude_favorites: bool = False,
    only_favorites: bool = False,
    exclude_watched: bool = False,
    only_watched: bool = False,
    order_by: str = "processed_desc",
    category: str | None = None,
    blacklisted_categories: set[str] | None = None,
    only_reddit_saved: bool = False,
) -> tuple[list[Video], str | None]:
    settings = get_settings()
    page_size = max(1, min(page_size, settings.max_feed_page_size))

    base_stmt = _base_feed_stmt(
        exclude_favorites=exclude_favorites,
        only_favorites=only_favorites,
        exclude_watched=exclude_watched,
        only_watched=only_watched,
        category=category,
        blacklisted_categories=blacklisted_categories,
        only_reddit_saved=only_reddit_saved,
    )

    if random_order:
        decoded_random = decode_random_cursor(cursor) if cursor else None
        if decoded_random:
            seed, last_rand_key, last_video_id = decoded_random
        else:
            seed = int(datetime.now(timezone.utc).timestamp() * 1000) % 2_147_483_647
            last_rand_key = None
            last_video_id = None

        rand_key_expr = ((Video.id * 1103515245 + seed) % 2_147_483_647).label("rand_key")
        random_stmt = base_stmt.add_columns(rand_key_expr)
        if last_rand_key is not None and last_video_id is not None:
            random_stmt = random_stmt.where(
                or_(
                    rand_key_expr > last_rand_key,
                    and_(rand_key_expr == last_rand_key, Video.id > last_video_id),
                )
            )

        random_stmt = random_stmt.order_by(rand_key_expr.asc(), Video.id.asc()).limit(page_size + 1)
        rows = list(session.execute(random_stmt).all())
        has_more = len(rows) > page_size
        rows_page = rows[:page_size]
        items = [row[0] for row in rows_page]
        next_cursor = None
        if has_more and rows_page:
            last_row = rows_page[-1]
            next_cursor = encode_random_cursor(seed, int(last_row[1]), int(last_row[0].id))
        return items, next_cursor

    stmt = base_stmt
    if order_by == "detected_desc":
        order_column = Video.detected_at
        last_value_from_video = lambda video: video.detected_at
    elif order_by == "favorite_desc":
        order_column = func.coalesce(
            Video.favorite_at,
            Video.updated_at,
            Video.detected_at,
            Video.sort_timestamp,
        )
        last_value_from_video = (
            lambda video: (
                video.favorite_at
                or video.updated_at
                or video.detected_at
                or video.sort_timestamp
            )
        )
    else:
        order_column = Video.sort_timestamp
        last_value_from_video = lambda video: video.sort_timestamp

    decoded = decode_cursor(cursor) if cursor else None
    if decoded:
        ts, video_id = decoded
        stmt = stmt.where(
            or_(
                order_column < ts,
                and_(order_column == ts, Video.id < video_id),
            )
        )

    stmt = stmt.order_by(order_column.desc(), Video.id.desc()).limit(page_size + 1)
    rows = list(session.scalars(stmt))
    has_more = len(rows) > page_size
    items = rows[:page_size]

    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor(last_value_from_video(last), last.id)
    return items, next_cursor


def count_category_videos(
    session: Session,
    category: str,
    blacklisted_categories: set[str] | None = None,
) -> int:
    stmt = _base_feed_stmt(
        exclude_favorites=False,
        only_favorites=False,
        exclude_watched=False,
        only_watched=False,
        category=category,
        blacklisted_categories=blacklisted_categories,
    )
    return int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)


def count_category_watched_videos(
    session: Session,
    category: str,
    blacklisted_categories: set[str] | None = None,
) -> int:
    stmt = _base_feed_stmt(
        exclude_favorites=False,
        only_favorites=False,
        exclude_watched=False,
        only_watched=True,
        category=category,
        blacklisted_categories=blacklisted_categories,
    )
    return int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
