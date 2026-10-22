from datetime import datetime, timezone

from sqlalchemy import and_, case, func, not_, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import RedditImage
from app.services.category_blacklist import normalize_category_name


def encode_image_cursor(is_watched: bool, sort_timestamp: datetime, image_id: int) -> str:
    watched_flag = 1 if is_watched else 0
    return f"w|{watched_flag}|{sort_timestamp.isoformat()}|{image_id}"


def decode_image_cursor(cursor: str) -> tuple[bool, datetime, int] | None:
    try:
        kind, watched_raw, ts_raw, id_raw = cursor.split("|", maxsplit=3)
        if kind != "w":
            return None
        return bool(int(watched_raw)), datetime.fromisoformat(ts_raw), int(id_raw)
    except Exception:  # noqa: BLE001
        return None


def encode_image_random_cursor(seed: int, is_watched: bool, rand_key: int, image_id: int) -> str:
    watched_flag = 1 if is_watched else 0
    return f"rw|{seed}|{watched_flag}|{rand_key}|{image_id}"


def decode_image_random_cursor(cursor: str) -> tuple[int, bool, int, int] | None:
    try:
        kind, seed_raw, watched_raw, key_raw, id_raw = cursor.split("|", maxsplit=4)
        if kind != "rw":
            return None
        return int(seed_raw), bool(int(watched_raw)), int(key_raw), int(id_raw)
    except Exception:  # noqa: BLE001
        return None


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _base_image_feed_stmt(
    category: str | None,
    blacklisted_categories: set[str] | None,
    *,
    only_favorites: bool = False,
    only_watched: bool = False,
    only_reddit_saved: bool = False,
):
    stmt = select(RedditImage).where(
        RedditImage.is_deleted.is_(False),
        RedditImage.status == "ready",
    )
    if only_favorites:
        stmt = stmt.where(RedditImage.is_favorite.is_(True))
    if only_watched:
        stmt = stmt.where(RedditImage.is_watched.is_(True))
    if only_reddit_saved:
        stmt = stmt.where(RedditImage.is_reddit_saved.is_(True))

    normalized_category = normalize_category_name(category) if category else ""
    if normalized_category and not only_reddit_saved:
        stmt = stmt.where(RedditImage.category_name == normalized_category)

    normalized_blacklist: set[str] = set()
    for name in blacklisted_categories or set():
        normalized_name = normalize_category_name(name)
        if normalized_name:
            normalized_blacklist.add(normalized_name)
    if normalized_blacklist:
        stmt = stmt.where(
            or_(
                RedditImage.category_name.is_(None),
                RedditImage.category_name.notin_(normalized_blacklist),
            )
        )
    return stmt


def _watched_rank_expr():
    # 0 = nao vista (primeiro), 1 = vista (depois)
    return case((RedditImage.is_watched.is_(True), 1), else_=0)


def load_image_feed(
    session: Session,
    cursor: str | None,
    page_size: int,
    *,
    category: str | None = None,
    blacklisted_categories: set[str] | None = None,
    only_favorites: bool = False,
    only_watched: bool = False,
    only_reddit_saved: bool = False,
    order_by: str = "detected_desc",
    random_order: bool = False,
    include_total: bool = True,
) -> tuple[list[RedditImage], str | None, int | None]:
    settings = get_settings()
    page_size = max(1, min(page_size, settings.max_feed_page_size))

    base_stmt = _base_image_feed_stmt(
        category=category,
        blacklisted_categories=blacklisted_categories,
        only_favorites=only_favorites,
        only_watched=only_watched,
        only_reddit_saved=only_reddit_saved,
    )
    total: int | None = None
    if include_total:
        total = int(session.scalar(select(func.count()).select_from(base_stmt.subquery())) or 0)
    watched_rank = _watched_rank_expr()

    if random_order:
        decoded_random = decode_image_random_cursor(cursor) if cursor else None
        if decoded_random:
            seed, last_watched, last_rand_key, last_image_id = decoded_random
        else:
            seed = int(datetime.now(timezone.utc).timestamp() * 1000) % 2_147_483_647
            last_watched = None
            last_rand_key = None
            last_image_id = None

        rand_key_expr = ((RedditImage.id * 1103515245 + seed) % 2_147_483_647).label("rand_key")
        random_stmt = base_stmt.add_columns(rand_key_expr, watched_rank.label("watched_rank"))
        if last_watched is not None and last_rand_key is not None and last_image_id is not None:
            last_watched_rank = 1 if last_watched else 0
            random_stmt = random_stmt.where(
                or_(
                    watched_rank > last_watched_rank,
                    and_(
                        watched_rank == last_watched_rank,
                        or_(
                            rand_key_expr > last_rand_key,
                            and_(rand_key_expr == last_rand_key, RedditImage.id > last_image_id),
                        ),
                    ),
                )
            )

        random_stmt = random_stmt.order_by(
            watched_rank.asc(),
            rand_key_expr.asc(),
            RedditImage.id.asc(),
        ).limit(page_size + 1)
        rows = list(session.execute(random_stmt).all())
        has_more = len(rows) > page_size
        rows_page = rows[:page_size]
        items = [row[0] for row in rows_page]
        next_cursor = None
        if has_more and rows_page:
            last_row = rows_page[-1]
            next_cursor = encode_image_random_cursor(
                seed,
                bool(last_row[0].is_watched),
                int(last_row[1]),
                int(last_row[0].id),
            )
        return items, next_cursor, total

    if order_by == "favorite_desc":
        order_column = func.coalesce(
            RedditImage.favorite_at,
            RedditImage.updated_at,
            RedditImage.detected_at,
            RedditImage.sort_timestamp,
        )

        def last_value_from_image(image: RedditImage) -> datetime:
            return (
                image.favorite_at
                or image.updated_at
                or image.detected_at
                or image.sort_timestamp
            )
    else:
        order_column = RedditImage.sort_timestamp

        def last_value_from_image(image: RedditImage) -> datetime:
            return image.sort_timestamp

    stmt = base_stmt
    decoded = decode_image_cursor(cursor) if cursor else None
    if decoded:
        last_watched, last_ts, last_image_id = decoded
        last_watched_rank = 1 if last_watched else 0
        stmt = stmt.where(
            or_(
                watched_rank > last_watched_rank,
                and_(
                    watched_rank == last_watched_rank,
                    or_(
                        order_column < last_ts,
                        and_(order_column == last_ts, RedditImage.id < last_image_id),
                    ),
                ),
            )
        )

    stmt = stmt.order_by(
        watched_rank.asc(),
        order_column.desc(),
        RedditImage.id.desc(),
    ).limit(page_size + 1)
    rows = list(session.scalars(stmt))
    has_more = len(rows) > page_size
    items = rows[:page_size]

    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = encode_image_cursor(
            bool(last.is_watched),
            last_value_from_image(last),
            last.id,
        )
    return items, next_cursor, total


def count_category_images(
    session: Session,
    category: str,
    blacklisted_categories: set[str] | None = None,
) -> int:
    normalized_category = normalize_category_name(category)
    if not normalized_category:
        return 0
    normalized_blacklist = {
        normalize_category_name(name)
        for name in (blacklisted_categories or set())
        if normalize_category_name(name)
    }
    if normalized_category in normalized_blacklist:
        return 0

    stmt = _base_image_feed_stmt(category=category, blacklisted_categories=blacklisted_categories)
    return int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)


def count_category_watched_images(
    session: Session,
    category: str,
    blacklisted_categories: set[str] | None = None,
) -> int:
    normalized_category = normalize_category_name(category)
    if not normalized_category:
        return 0
    normalized_blacklist = {
        normalize_category_name(name)
        for name in (blacklisted_categories or set())
        if normalize_category_name(name)
    }
    if normalized_category in normalized_blacklist:
        return 0

    stmt = _base_image_feed_stmt(
        category=category,
        blacklisted_categories=blacklisted_categories,
        only_watched=True,
    )
    return int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
