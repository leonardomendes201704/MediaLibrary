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
        if "is_reddit_saved" not in columns:
            conn.execute(text("ALTER TABLE videos ADD COLUMN is_reddit_saved BOOLEAN NOT NULL DEFAULT 0"))
        if "minio_object_key" not in columns:
            conn.execute(text("ALTER TABLE videos ADD COLUMN minio_object_key VARCHAR(1024) NULL"))
        if "minio_sync_status" not in columns:
            conn.execute(text("ALTER TABLE videos ADD COLUMN minio_sync_status VARCHAR(32) NULL"))
        if "minio_sync_token" not in columns:
            conn.execute(text("ALTER TABLE videos ADD COLUMN minio_sync_token INTEGER NOT NULL DEFAULT 0"))
        if "minio_source_size" not in columns:
            conn.execute(text("ALTER TABLE videos ADD COLUMN minio_source_size INTEGER NULL"))
        if "minio_error" not in columns:
            conn.execute(text("ALTER TABLE videos ADD COLUMN minio_error TEXT NULL"))
        if "minio_synced_at" not in columns:
            conn.execute(text("ALTER TABLE videos ADD COLUMN minio_synced_at DATETIME NULL"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_videos_is_favorite ON videos (is_favorite)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_videos_favorite_at ON videos (favorite_at)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_videos_is_watched ON videos (is_watched)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_videos_watched_at ON videos (watched_at)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_videos_has_audio ON videos (has_audio)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_videos_is_reddit_saved ON videos (is_reddit_saved)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_videos_minio_sync_status ON videos (minio_sync_status)"))
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
                        media_type VARCHAR(16) NOT NULL DEFAULT 'video',
                        is_reddit_saved BOOLEAN NOT NULL DEFAULT 0,
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
        if "reddit_media_history" in table_names or "reddit_media_history" in set(inspector.get_table_names()):
            # Re-inspect after possible create above.
            history_columns = {col["name"] for col in inspect(engine).get_columns("reddit_media_history")}
            if "media_type" not in history_columns:
                conn.execute(
                    text("ALTER TABLE reddit_media_history ADD COLUMN media_type VARCHAR(16) NOT NULL DEFAULT 'video'")
                )
                conn.execute(
                    text("CREATE INDEX IF NOT EXISTS ix_reddit_media_history_media_type ON reddit_media_history (media_type)")
                )
            if "is_reddit_saved" not in history_columns:
                conn.execute(
                    text("ALTER TABLE reddit_media_history ADD COLUMN is_reddit_saved BOOLEAN NOT NULL DEFAULT 0")
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_reddit_media_history_is_reddit_saved "
                        "ON reddit_media_history (is_reddit_saved)"
                    )
                )
        if "reddit_images" not in table_names:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS reddit_images (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        original_path VARCHAR(2048) NOT NULL UNIQUE,
                        file_path VARCHAR(2048) NOT NULL,
                        mime_type VARCHAR(64) NULL,
                        status VARCHAR(32) NOT NULL DEFAULT 'ready',
                        is_deleted BOOLEAN NOT NULL DEFAULT 0,
                        is_favorite BOOLEAN NOT NULL DEFAULT 0,
                        favorite_at DATETIME NULL,
                        minio_object_key VARCHAR(1024) NULL,
                        minio_sync_status VARCHAR(32) NULL,
                        minio_sync_token INTEGER NOT NULL DEFAULT 0,
                        minio_source_size INTEGER NULL,
                        minio_error TEXT NULL,
                        minio_synced_at DATETIME NULL,
                        is_watched BOOLEAN NOT NULL DEFAULT 0,
                        watched_at DATETIME NULL,
                        is_reddit_saved BOOLEAN NOT NULL DEFAULT 0,
                        source_size INTEGER NULL,
                        source_mtime FLOAT NULL,
                        source_fingerprint VARCHAR(128) NULL,
                        width INTEGER NULL,
                        height INTEGER NULL,
                        post_id VARCHAR(64) NULL,
                        post_url VARCHAR(2048) NULL,
                        title TEXT NULL,
                        detected_at DATETIME NOT NULL,
                        processed_at DATETIME NULL,
                        sort_timestamp DATETIME NOT NULL,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL
                    )
                    """
                )
            )
        if "reddit_images" in table_names or "reddit_images" in set(inspect(engine).get_table_names()):
            image_columns = {col["name"] for col in inspect(engine).get_columns("reddit_images")}
            if "is_reddit_saved" not in image_columns:
                conn.execute(
                    text("ALTER TABLE reddit_images ADD COLUMN is_reddit_saved BOOLEAN NOT NULL DEFAULT 0")
                )
            if "category_name" not in image_columns:
                conn.execute(text("ALTER TABLE reddit_images ADD COLUMN category_name VARCHAR(255) NULL"))
            if "minio_object_key" not in image_columns:
                conn.execute(text("ALTER TABLE reddit_images ADD COLUMN minio_object_key VARCHAR(1024) NULL"))
            if "minio_sync_status" not in image_columns:
                conn.execute(text("ALTER TABLE reddit_images ADD COLUMN minio_sync_status VARCHAR(32) NULL"))
            if "minio_sync_token" not in image_columns:
                conn.execute(text("ALTER TABLE reddit_images ADD COLUMN minio_sync_token INTEGER NOT NULL DEFAULT 0"))
            if "minio_source_size" not in image_columns:
                conn.execute(text("ALTER TABLE reddit_images ADD COLUMN minio_source_size INTEGER NULL"))
            if "minio_error" not in image_columns:
                conn.execute(text("ALTER TABLE reddit_images ADD COLUMN minio_error TEXT NULL"))
            if "minio_synced_at" not in image_columns:
                conn.execute(text("ALTER TABLE reddit_images ADD COLUMN minio_synced_at DATETIME NULL"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reddit_images_status ON reddit_images (status)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reddit_images_is_deleted ON reddit_images (is_deleted)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reddit_images_is_favorite ON reddit_images (is_favorite)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reddit_images_minio_sync_status ON reddit_images (minio_sync_status)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reddit_images_is_watched ON reddit_images (is_watched)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reddit_images_is_reddit_saved ON reddit_images (is_reddit_saved)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reddit_images_sort_timestamp ON reddit_images (sort_timestamp)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reddit_images_post_id ON reddit_images (post_id)"))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_reddit_images_category_name ON reddit_images (category_name)")
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_reddit_images_category_feed "
                "ON reddit_images (category_name, is_deleted, status, is_watched, sort_timestamp)"
            )
        )

        from app.services.category_blacklist import category_name_from_original_path

        pending_rows = conn.execute(
            text("SELECT id, original_path FROM reddit_images WHERE category_name IS NULL")
        ).fetchall()
        for image_id, original_path in pending_rows:
            category_name = category_name_from_original_path(str(original_path or ""))
            if not category_name:
                continue
            conn.execute(
                text("UPDATE reddit_images SET category_name = :category_name WHERE id = :id"),
                {"category_name": category_name, "id": image_id},
            )
