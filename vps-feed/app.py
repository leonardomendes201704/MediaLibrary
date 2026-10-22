from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import threading
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from minio import Minio
from minio.error import S3Error
from pydantic import BaseModel, Field

logger = logging.getLogger("secretfeed")

PUBLIC_HOST = "secretfeed.insta-ads.online"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
BACKUP_IMAGE_EXT = IMAGE_EXT | {".heic", ".heif"}
VIDEO_EXT = {".mp4", ".webm"}
BACKUP_VIDEO_EXT = VIDEO_EXT | {".mov", ".m4v"}
BACKUP_MEDIA_EXT = BACKUP_IMAGE_EXT | BACKUP_VIDEO_EXT
ALLOWED_EXT = IMAGE_EXT | VIDEO_EXT
SEED_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_BACKUP_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".m4v": "video/mp4",
}
STATIC_DIR = Path(__file__).resolve().parent / "static"
NO_STORE = {"Cache-Control": "no-store"}


def env_text(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def allowed_hosts() -> set[str]:
    hosts = {PUBLIC_HOST}
    for part in env_text("FEED_ALLOWED_HOSTS", "localhost,127.0.0.1").split(","):
        host = part.strip().lower()
        if host:
            hosts.add(host)
    return hosts


def media_type_for(key: str) -> str:
    return "video" if Path(key).suffix.lower() in VIDEO_EXT else "image"


class Catalog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._client: Minio | None = None
        self.catalog: list[str] = []
        self.subreddits: dict[str, str] = {}
        self.seeds: dict[str, tuple[list[str], float]] = {}
        self.ready = False

    def client(self) -> Minio:
        if self._client is not None:
            return self._client
        endpoint = env_text("MINIO_ENDPOINT").removeprefix("https://").removeprefix("http://").strip("/")
        if not endpoint or not env_text("MINIO_ACCESS_KEY") or not env_text("MINIO_SECRET_KEY"):
            raise RuntimeError("MinIO nao configurado")
        self._client = Minio(
            endpoint,
            access_key=env_text("MINIO_ACCESS_KEY"),
            secret_key=env_text("MINIO_SECRET_KEY"),
            secure=env_bool("MINIO_SECURE", True),
            region=env_text("MINIO_REGION", "us-east-1") or None,
        )
        return self._client

    def refresh(self) -> int:
        self._client = None
        bucket = env_text("MINIO_BUCKET", "medialibrary")
        client = self.client()
        keys: list[str] = []
        for prefix in ("images/", "videos/"):
            for obj in client.list_objects(bucket, prefix=prefix, recursive=True):
                if getattr(obj, "is_dir", False):
                    continue
                name = obj.object_name or ""
                if Path(name).suffix.lower() not in ALLOWED_EXT:
                    continue
                if not obj.size:
                    continue
                keys.append(name)
        labels = self._load_subreddits(client, bucket)
        with self._lock:
            self.catalog = keys
            self.subreddits = labels
            self.ready = True
        return len(keys)

    def subreddit_for(self, key: str) -> str:
        with self._lock:
            return self.subreddits.get(key, "")

    def _load_subreddits(self, client: Minio, bucket: str) -> dict[str, str]:
        try:
            response = client.get_object(bucket, "meta/subreddits.json")
        except S3Error as exc:
            if getattr(exc, "code", "") in {"NoSuchKey", "NoSuchObject"}:
                return {}
            logger.exception("Failed to read subreddit index")
            return {}
        try:
            payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            logger.exception("Invalid subreddit index")
            return {}
        finally:
            response.close()
            response.release_conn()
        if not isinstance(payload, dict):
            return {}
        labels: dict[str, str] = {}
        for key, value in payload.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            name = value.strip()
            if not name or len(name) > 80:
                continue
            if not (key.startswith("images/") or key.startswith("videos/")):
                continue
            labels[key] = name
        return labels

    def count(self) -> int:
        with self._lock:
            return len(self.catalog)

    def order_for(self, seed: str, exclude: set[str] | None = None) -> list[str]:
        now = time.time()
        blocked = exclude or set()
        with self._lock:
            self._prune_seeds(now)
            catalog_keys = list(self.catalog)
            cached = self.seeds.get(seed)
            if cached is None:
                order = [key for key in catalog_keys if key not in blocked]
                random.Random(seed).shuffle(order)
            else:
                order = list(cached[0])
                known = set(order)
                fresh = [key for key in catalog_keys if key not in known and key not in blocked]
                if fresh:
                    random.Random(f"{seed}:append:{len(order)}").shuffle(fresh)
                    order.extend(fresh)
            self.seeds[seed] = (order, now)
            return order

    def presign(self, key: str) -> str:
        hours = max(1, env_int("PRESIGN_HOURS", 6))
        return self.client().presigned_get_object(
            env_text("MINIO_BUCKET", "medialibrary"),
            key,
            expires=timedelta(hours=hours),
        )

    def _prune_seeds(self, now: float) -> None:
        expired = [key for key, (_, seen_at) in self.seeds.items() if now - seen_at > 86400]
        for key in expired:
            del self.seeds[key]
        overflow = len(self.seeds) - 200
        if overflow <= 0:
            return
        oldest = sorted(self.seeds.items(), key=lambda item: item[1][1])
        for key, _ in oldest[:overflow]:
            del self.seeds[key]


class BackupCatalog:
    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog
        self._lock = threading.Lock()
        self.catalog: list[str] = []
        self.seeds: dict[str, tuple[list[str], float]] = {}
        self.ready = False

    def enabled(self) -> bool:
        return env_bool("BACKUP_ENABLED", True) and bool(env_text("BACKUP_BUCKET", "phone-backup"))

    def bucket(self) -> str:
        return env_text("BACKUP_BUCKET", "phone-backup")

    def refresh(self) -> int:
        if not self.enabled():
            with self._lock:
                self.catalog = []
                self.ready = True
            return 0
        self._catalog._client = None
        bucket = self.bucket()
        client = self._catalog.client()
        keys: list[str] = []
        for obj in client.list_objects(bucket, prefix="backup/", recursive=True):
            if getattr(obj, "is_dir", False):
                continue
            name = obj.object_name or ""
            if not name.startswith("backup/"):
                continue
            if Path(name).suffix.lower() not in BACKUP_MEDIA_EXT:
                continue
            if not obj.size:
                continue
            keys.append(name)
        with self._lock:
            self.catalog = keys
            self.ready = True
        return len(keys)

    def count(self) -> int:
        with self._lock:
            return len(self.catalog)

    def order_for(self, seed: str, exclude: set[str] | None = None) -> list[str]:
        now = time.time()
        blocked = exclude or set()
        with self._lock:
            self._prune_seeds(now)
            catalog_keys = list(self.catalog)
            cached = self.seeds.get(seed)
            if cached is None:
                order = [key for key in catalog_keys if key not in blocked]
                random.Random(seed).shuffle(order)
            else:
                order = list(cached[0])
                known = set(order)
                fresh = [key for key in catalog_keys if key not in known and key not in blocked]
                if fresh:
                    random.Random(f"{seed}:append:{len(order)}").shuffle(fresh)
                    order.extend(fresh)
            self.seeds[seed] = (order, now)
            return order

    def presign(self, key: str) -> str:
        hours = max(1, env_int("PRESIGN_HOURS", 6))
        return self._catalog.client().presigned_get_object(
            self.bucket(),
            key,
            expires=timedelta(hours=hours),
        )

    def _prune_seeds(self, now: float) -> None:
        expired = [key for key, (_, seen_at) in self.seeds.items() if now - seen_at > 86400]
        for key in expired:
            del self.seeds[key]
        overflow = len(self.seeds) - 200
        if overflow <= 0:
            return
        oldest = sorted(self.seeds.items(), key=lambda item: item[1][1])
        for key, _ in oldest[:overflow]:
            del self.seeds[key]


class BackupUploader:
    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog
        self._bucket_ready = False
        self._bucket_lock = threading.Lock()

    def enabled(self) -> bool:
        return env_bool("BACKUP_ENABLED", True) and bool(env_text("BACKUP_BUCKET", "phone-backup"))

    def bucket(self) -> str:
        return env_text("BACKUP_BUCKET", "phone-backup")

    def max_file_bytes(self) -> int:
        return max(1, env_int("BACKUP_MAX_FILE_MB", 25)) * 1024 * 1024

    def max_video_bytes(self) -> int:
        return max(1, env_int("BACKUP_MAX_VIDEO_MB", 150)) * 1024 * 1024

    def max_bytes_for(self, filename: str) -> int:
        ext = Path(filename).suffix.lower()
        if ext in BACKUP_VIDEO_EXT:
            return self.max_video_bytes()
        return self.max_file_bytes()

    def max_files(self) -> int:
        return max(1, min(200, env_int("BACKUP_MAX_FILES", 100)))

    def presign_minutes(self) -> int:
        return max(5, env_int("BACKUP_PRESIGN_MINUTES", 30))

    def ensure_bucket(self) -> None:
        if not self.enabled() or self._bucket_ready:
            return
        with self._bucket_lock:
            if self._bucket_ready:
                return
            client = self._catalog.client()
            bucket = self.bucket()
            if not client.bucket_exists(bucket):
                region = env_text("MINIO_REGION", "us-east-1") or None
                client.make_bucket(bucket, location=region)
                logger.info("Created backup bucket %s", bucket)
            self._bucket_ready = True

    def config(self) -> dict[str, object]:
        extensions = sorted(BACKUP_MEDIA_EXT)
        return {
            "enabled": self.enabled(),
            "bucket": self.bucket(),
            "max_file_bytes": self.max_file_bytes(),
            "max_video_bytes": self.max_video_bytes(),
            "max_files": self.max_files(),
            "extensions": extensions,
            "concurrency": 4,
        }

    def create_batch(self, files: list[BackupFileMeta]) -> list[dict[str, str]]:
        if not self.enabled():
            raise HTTPException(status_code=503, detail="backup desabilitado")
        if len(files) > self.max_files():
            raise HTTPException(status_code=400, detail=f"maximo de {self.max_files()} arquivos por lote")
        self.ensure_bucket()
        client = self._catalog.client()
        bucket = self.bucket()
        expires = timedelta(minutes=self.presign_minutes())
        day = date.today().isoformat()
        items: list[dict[str, str]] = []
        for meta in files:
            limit = self.max_bytes_for(meta.name)
            if meta.size > limit:
                raise HTTPException(status_code=400, detail=f"arquivo grande: {meta.name}")
            ext = Path(meta.name).suffix.lower()
            if ext not in BACKUP_MEDIA_EXT:
                raise HTTPException(status_code=400, detail=f"tipo nao permitido: {meta.name}")
            key = self._object_key(day, meta.name)
            content_type = self._content_type(meta, ext)
            upload_url = client.presigned_put_object(bucket, key, expires=expires)
            items.append(
                {
                    "name": meta.name,
                    "key": key,
                    "upload_url": upload_url,
                    "content_type": content_type,
                }
            )
        return items

    def _object_key(self, day: str, filename: str) -> str:
        ext = Path(filename).suffix.lower()
        if ext not in BACKUP_MEDIA_EXT:
            ext = ".jpg"
        stem = Path(filename).stem
        safe = _SAFE_NAME.sub("_", stem).strip("._") or "file"
        if len(safe) > 120:
            safe = safe[:120]
        token = uuid.uuid4().hex[:8]
        return f"backup/{day}/{token}_{safe}{ext}"

    def _content_type(self, meta: BackupFileMeta, ext: str) -> str:
        hinted = (meta.content_type or "").split(";", 1)[0].strip().lower()
        if hinted.startswith("image/") or hinted.startswith("video/"):
            return hinted
        return _BACKUP_CONTENT_TYPES.get(ext, "application/octet-stream")


catalog = Catalog()
backup = BackupUploader(catalog)
backup_catalog = BackupCatalog(catalog)
app = FastAPI(title="Secret Feed", docs_url=None, redoc_url=None)


@app.middleware("http")
async def restrict_host(request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    host = request.headers.get("host", "").split(":")[0].strip().lower()
    if host not in allowed_hosts():
        return Response(status_code=404)
    response = await call_next(request)
    response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    return response


@app.on_event("startup")
async def on_startup() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    interval = max(30, env_int("CATALOG_REFRESH_SECONDS", 300))

    async def refresh_loop() -> None:
        while True:
            try:
                count = await asyncio.to_thread(catalog.refresh)
                logger.info("Catalog ready with %d object(s)", count)
            except Exception:
                logger.exception("Catalog refresh failed")
            await asyncio.sleep(interval)

    asyncio.create_task(refresh_loop())

    backup_interval = max(30, env_int("BACKUP_CATALOG_REFRESH_SECONDS", env_int("CATALOG_REFRESH_SECONDS", 300)))

    async def backup_refresh_loop() -> None:
        while True:
            if not backup_catalog.enabled():
                await asyncio.sleep(backup_interval)
                continue
            try:
                count = await asyncio.to_thread(backup_catalog.refresh)
                logger.info("Backup catalog ready with %d object(s)", count)
            except Exception:
                logger.exception("Backup catalog refresh failed")
            await asyncio.sleep(backup_interval)

    asyncio.create_task(backup_refresh_loop())

    if backup.enabled():
        try:
            await asyncio.to_thread(backup.ensure_bucket)
            logger.info("Backup bucket ready: %s", backup.bucket())
        except Exception:
            logger.exception("Backup bucket setup failed")


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "ready": catalog.ready,
        "count": catalog.count(),
        "backup_enabled": backup.enabled(),
        "backup_view_ready": backup_catalog.ready,
        "backup_count": backup_catalog.count(),
    }


class FeedIn(BaseModel):
    cursor: int = Field(default=0, ge=0)
    seed: str | None = None
    limit: int = Field(default=6, ge=1, le=12)
    exclude: list[str] = Field(default_factory=list, max_length=20000)


class BackupFileMeta(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    size: int = Field(..., ge=1)
    content_type: str = Field(default="", max_length=128)


class BackupBatchIn(BaseModel):
    files: list[BackupFileMeta] = Field(..., min_length=1, max_length=200)


def clean_backup_exclude(keys: list[str]) -> set[str]:
    blocked: set[str] = set()
    for key in keys:
        if not isinstance(key, str):
            continue
        name = key.strip()
        if not name or len(name) > 500 or ".." in name or "\\" in name or "\n" in name:
            continue
        if not name.startswith("backup/"):
            continue
        if Path(name).suffix.lower() not in BACKUP_MEDIA_EXT:
            continue
        blocked.add(name)
    return blocked


def clean_exclude(keys: list[str]) -> set[str]:
    blocked: set[str] = set()
    for key in keys:
        if not isinstance(key, str):
            continue
        name = key.strip()
        if not name or len(name) > 500 or ".." in name or "\\" in name or "\n" in name:
            continue
        if not (name.startswith("images/") or name.startswith("videos/")):
            continue
        if Path(name).suffix.lower() not in ALLOWED_EXT:
            continue
        blocked.add(name)
    return blocked


def feed_payload(cursor: int, seed: str | None, limit: int, exclude: set[str]) -> JSONResponse:
    if not catalog.ready:
        return JSONResponse(
            {"detail": "catalogo ainda carregando"},
            status_code=503,
            headers={**NO_STORE, "Retry-After": "2"},
        )
    active_seed = seed if seed and SEED_RE.fullmatch(seed) else uuid.uuid4().hex
    order = catalog.order_for(active_seed, exclude)
    page = order[cursor : cursor + limit]
    items = []
    for key in page:
        try:
            url = catalog.presign(key)
        except Exception:
            logger.exception("Failed to presign %s", key)
            continue
        items.append(
            {
                "key": key,
                "type": media_type_for(key),
                "url": url,
                "subreddit": catalog.subreddit_for(key),
            }
        )
    next_index = cursor + len(page)
    next_cursor = next_index if next_index < len(order) else None
    return JSONResponse(
        {
            "seed": active_seed,
            "next_cursor": next_cursor,
            "end_cursor": next_index,
            "total": len(order),
            "catalog_total": catalog.count(),
            "items": items,
        },
        headers=NO_STORE,
    )


@app.get("/api/feed")
def get_feed(
    cursor: int = Query(default=0, ge=0),
    seed: str | None = Query(default=None),
    limit: int = Query(default=6, ge=1, le=12),
) -> JSONResponse:
    return feed_payload(cursor, seed, limit, set())


@app.post("/api/feed")
def post_feed(body: FeedIn) -> JSONResponse:
    return feed_payload(body.cursor, body.seed, body.limit, clean_exclude(body.exclude))


@app.get("/api/backup/config")
def backup_config() -> JSONResponse:
    return JSONResponse(backup.config(), headers=NO_STORE)


@app.post("/api/backup/batch")
def backup_batch(body: BackupBatchIn) -> JSONResponse:
    items = backup.create_batch(body.files)
    return JSONResponse({"items": items}, headers=NO_STORE)


def backup_feed_payload(cursor: int, seed: str | None, limit: int, exclude: set[str]) -> JSONResponse:
    if not backup_catalog.enabled():
        return JSONResponse(
            {"detail": "backup desabilitado"},
            status_code=503,
            headers={**NO_STORE, "Retry-After": "2"},
        )
    if not backup_catalog.ready:
        return JSONResponse(
            {"detail": "catalogo de backup ainda carregando"},
            status_code=503,
            headers={**NO_STORE, "Retry-After": "2"},
        )
    active_seed = seed if seed and SEED_RE.fullmatch(seed) else uuid.uuid4().hex
    order = backup_catalog.order_for(active_seed, exclude)
    page = order[cursor : cursor + limit]
    items = []
    for key in page:
        try:
            url = backup_catalog.presign(key)
        except Exception:
            logger.exception("Failed to presign backup %s", key)
            continue
        items.append({"key": key, "type": media_type_for(key), "url": url})
    next_index = cursor + len(page)
    next_cursor = next_index if next_index < len(order) else None
    return JSONResponse(
        {
            "seed": active_seed,
            "next_cursor": next_cursor,
            "end_cursor": next_index,
            "total": len(order),
            "catalog_total": backup_catalog.count(),
            "items": items,
        },
        headers=NO_STORE,
    )


@app.get("/api/backup/feed")
def get_backup_feed(
    cursor: int = Query(default=0, ge=0),
    seed: str | None = Query(default=None),
    limit: int = Query(default=6, ge=1, le=12),
) -> JSONResponse:
    return backup_feed_payload(cursor, seed, limit, set())


@app.post("/api/backup/feed")
def post_backup_feed(body: FeedIn) -> JSONResponse:
    return backup_feed_payload(body.cursor, body.seed, body.limit, clean_backup_exclude(body.exclude))


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
