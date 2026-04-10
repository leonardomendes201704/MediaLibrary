from sqlalchemy import Engine, inspect, text


def ensure_runtime_schema(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "videos" not in table_names:
        return

    columns = {col["name"] for col in inspector.get_columns("videos")}
    with engine.begin() as conn:
        if "is_favorite" not in columns:
            conn.execute(text("ALTER TABLE videos ADD COLUMN is_favorite BOOLEAN NOT NULL DEFAULT 0"))
        if "favorite_at" not in columns:
            conn.execute(text("ALTER TABLE videos ADD COLUMN favorite_at DATETIME NULL"))
        if "is_watched" not in columns:
            conn.execute(text("ALTER TABLE videos ADD COLUMN is_watched BOOLEAN NOT NULL DEFAULT 0"))
        if "watched_at" not in columns:
            conn.execute(text("ALTER TABLE videos ADD COLUMN watched_at DATETIME NULL"))
        if "watched_ratio" not in columns:
            conn.execute(text("ALTER TABLE videos ADD COLUMN watched_ratio FLOAT NULL"))
        if "has_audio" not in columns:
            conn.execute(text("ALTER TABLE videos ADD COLUMN has_audio BOOLEAN NULL"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_videos_is_favorite ON videos (is_favorite)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_videos_favorite_at ON videos (favorite_at)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_videos_is_watched ON videos (is_watched)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_videos_watched_at ON videos (watched_at)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_videos_has_audio ON videos (has_audio)"))
        if "reddit_media_history" not in table_names:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS reddit_media_history (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        item_key VARCHAR(64) NOT NULL UNIQUE,
                        video_url_key VARCHAR(64) NOT NULL,
                        subreddit VARCHAR(255) NOT NULL,
                        post_id VARCHAR(64) NULL,
                        post_url VARCHAR(2048) NULL,
                        video_url VARCHAR(2048) NOT NULL,
                        title TEXT NULL,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL
                    )
                    """
                )
            )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_reddit_media_history_video_url_key ON reddit_media_history (video_url_key)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_reddit_media_history_subreddit ON reddit_media_history (subreddit)")
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reddit_media_history_post_id ON reddit_media_history (post_id)"))
