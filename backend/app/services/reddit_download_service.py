from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import shutil
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.models import AppConfig, Job, RedditMediaHistory, Video
from app.services.category_blacklist import (
    extract_download_root,
    extract_path_badges,
    load_blacklisted_categories,
    normalize_category_name,
)
from app.services.file_utils import normalize_path
from app.services.ingest_service import IngestService
from app.services.reddit_service import RedditConfigError, RedditService


logger = logging.getLogger("reddit_download")

REDDIT_DOWNLOADED_SUBREDDITS_KEY = "reddit_downloaded_subreddits"
REDDIT_IDLE_AUTO_DOWNLOAD_ENABLED_KEY = "reddit_idle_auto_download_enabled"
REDDIT_MIN_DOWNLOAD_FREE_GB_KEY = "reddit_min_download_free_gb"
AUTO_IDLE_THRESHOLD_SECONDS = 5 * 60
AUTO_POLL_INTERVAL_SECONDS = 15
AUTO_PER_SUBREDDIT_LIMIT = 5
AUTO_SUBREDDIT_PREFIX = "u_"
DEFAULT_MIN_DOWNLOAD_FREE_BYTES = 8 * 1024 * 1024 * 1024
DOWNLOAD_DISK_RECHECK_BYTES = 16 * 1024 * 1024
AUTO_RETRY_INTERVAL_SECONDS = AUTO_IDLE_THRESHOLD_SECONDS


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_config_bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def parse_config_float(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        normalized = float(value.strip())
    except (AttributeError, ValueError):
        return default
    if normalized <= 0:
        return default
    return normalized


class InsufficientDiskSpaceError(RuntimeError):
    pass


class RedditDownloadService:
    def __init__(
        self,
        session_factory: sessionmaker,
        reddit_service: RedditService,
        ingest_service: IngestService,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._reddit = reddit_service
        self._ingest = ingest_service
        self._settings = settings
        self._lock = Lock()
        self._task: asyncio.Task | None = None
        self._auto_task: asyncio.Task | None = None
        self._status: dict[str, Any] = self._build_idle_status()
        self._last_busy_at = utcnow()
        self._last_auto_attempt_at: datetime | None = None

    @staticmethod
    def normalize_subreddit_name(value: str) -> str:
        normalized = value.strip()
        if normalized.lower().startswith("r/"):
            normalized = normalized[2:]
        return normalized

    @staticmethod
    def _normalize_history_video_url(value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return ""
        parsed = urlparse(normalized)
        if not parsed.scheme or not parsed.netloc:
            return normalized
        query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
        return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "", "", query, ""))

    @classmethod
    def _build_history_entry(cls, post: dict[str, Any]) -> dict[str, str | None] | None:
        video_url = cls._normalize_history_video_url(str(post.get("video_url") or "").strip())
        if not video_url:
            return None

        post_id = str(post.get("post_id") or "").strip()
        item_source = f"{post_id}|{video_url}" if post_id else video_url
        subreddit = cls.normalize_subreddit_name(str(post.get("subreddit") or "")).lower()
        return {
            "item_key": hashlib.sha1(item_source.encode("utf-8")).hexdigest(),
            "video_url_key": hashlib.sha1(video_url.encode("utf-8")).hexdigest(),
            "subreddit": subreddit,
            "post_id": post_id or None,
            "post_url": str(post.get("post_url") or "").strip() or None,
            "video_url": video_url,
            "title": str(post.get("title") or "").strip() or None,
        }

    @staticmethod
    def _iter_chunks(values: list[str], chunk_size: int = 400):
        for start in range(0, len(values), chunk_size):
            yield values[start : start + chunk_size]

    @staticmethod
    def _build_idle_status() -> dict[str, Any]:
        return {
            "task_id": None,
            "state": "idle",
            "message": "Nenhum download ativo.",
            "started_at": None,
            "finished_at": None,
            "total_subreddits": 0,
            "completed_subreddits": 0,
            "total_videos_planned": 0,
            "processed_videos": 0,
            "failed_videos": 0,
            "items": [],
            "logs": [],
            "trigger": None,
        }

    def _clone_status(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._status)

    def get_status(self) -> dict[str, Any]:
        status = self._clone_status()
        auto_enabled = self.is_auto_download_enabled()
        status["auto_enabled"] = auto_enabled
        if not auto_enabled:
            self._apply_auto_disabled_status(status)
        return status

    @staticmethod
    def _apply_auto_disabled_status(status: dict[str, Any]) -> None:
        current_state = str(status.get("state") or "").lower()
        if current_state not in {"idle", "auto_waiting", "auto_disabled"}:
            return
        status["task_id"] = None
        status["state"] = "auto_disabled"
        status["message"] = "Auto-download quando ocioso desativado. Downloads manuais continuam disponiveis."
        status["started_at"] = None
        status["finished_at"] = None
        status["total_subreddits"] = 0
        status["completed_subreddits"] = 0
        status["total_videos_planned"] = 0
        status["processed_videos"] = 0
        status["failed_videos"] = 0
        status["items"] = []
        status["trigger"] = None

    def _set_auto_disabled_status(self) -> None:
        with self._lock:
            self._apply_auto_disabled_status(self._status)

    def is_auto_download_enabled(self) -> bool:
        with self._session_factory() as session:
            row = session.scalar(select(AppConfig).where(AppConfig.key == REDDIT_IDLE_AUTO_DOWNLOAD_ENABLED_KEY))
            raw_value = row.value if row else None
        return parse_config_bool(raw_value, default=True)

    def set_auto_download_enabled(self, enabled: bool) -> bool:
        normalized = bool(enabled)
        current = self.is_auto_download_enabled()
        serialized = "true" if normalized else "false"
        with self._session_factory() as session:
            row = session.scalar(select(AppConfig).where(AppConfig.key == REDDIT_IDLE_AUTO_DOWNLOAD_ENABLED_KEY))
            if row:
                row.value = serialized
            else:
                session.add(AppConfig(key=REDDIT_IDLE_AUTO_DOWNLOAD_ENABLED_KEY, value=serialized))
            session.commit()

        self._last_busy_at = utcnow()
        self._last_auto_attempt_at = None
        current_state = str(self._clone_status().get("state") or "").lower()
        if current_state in {"idle", "auto_waiting", "auto_disabled"}:
            if normalized:
                self._set_auto_waiting_status(idle_seconds=0)
            else:
                self._set_auto_disabled_status()

        if current != normalized:
            action = "ativado" if normalized else "desativado"
            self._append_log(f"[AUTO] Auto-download quando ocioso {action} pelo admin.")
        return normalized

    async def start(self) -> None:
        task = self._auto_task
        if task and not task.done():
            return
        self._last_auto_attempt_at = None
        if self.is_auto_download_enabled():
            self._set_auto_waiting_status(idle_seconds=0)
            self._append_log(
                "Auto-download armado: quando o backend ficar 5 min ocioso, vou baixar os ultimos "
                f"{AUTO_PER_SUBREDDIT_LIMIT} videos de cada subreddit com prefixo {AUTO_SUBREDDIT_PREFIX}."
            )
        else:
            self._set_auto_disabled_status()
            self._append_log("[AUTO] Inicializado em modo desativado pelo admin.")
        self._auto_task = asyncio.create_task(
            self._run_auto_download_monitor(),
            name="reddit-download-auto-monitor",
        )

    def get_downloaded_subreddits(self) -> set[str]:
        with self._session_factory() as session:
            return self._load_downloaded_names(
                session,
                require_prefix=AUTO_SUBREDDIT_PREFIX,
            )

    def get_downloaded_categories(self) -> set[str]:
        with self._session_factory() as session:
            return self._load_downloaded_names(
                session,
                exclude_prefix=AUTO_SUBREDDIT_PREFIX,
            )

    def _load_downloaded_subreddits_from_config(self, session: Session) -> set[str]:
        row = session.scalar(select(AppConfig).where(AppConfig.key == REDDIT_DOWNLOADED_SUBREDDITS_KEY))
        if not row:
            return set()
        try:
            payload = row.value
            if not payload:
                return set()
            data = json.loads(payload)
        except Exception:
            return set()
        if not isinstance(data, list):
            return set()
        result: set[str] = set()
        for item in data:
            if not isinstance(item, str):
                continue
            normalized = self.normalize_subreddit_name(item).lower()
            if normalized:
                result.add(normalized)
        return result

    def _load_cataloged_subreddits_from_catalog(
        self,
        session: Session,
        *,
        require_prefix: str | None = None,
        exclude_prefix: str | None = None,
    ) -> set[str]:
        stmt = select(Video.original_path).where(Video.is_deleted.is_(False))
        result: set[str] = set()
        for original_path in session.scalars(stmt):
            normalized_path = normalize_path(str(original_path))
            if not extract_download_root(normalized_path, self._settings.reddit_download_dir):
                continue
            category, _ = extract_path_badges(normalized_path)
            normalized_category = normalize_category_name(category or "")
            if not normalized_category:
                continue
            if require_prefix and not normalized_category.startswith(require_prefix):
                continue
            if exclude_prefix and normalized_category.startswith(exclude_prefix):
                continue
            result.add(normalized_category)
        return result

    def _load_downloaded_names(
        self,
        session: Session,
        *,
        require_prefix: str | None = None,
        exclude_prefix: str | None = None,
    ) -> set[str]:
        result = self._load_cataloged_subreddits_from_catalog(
            session,
            require_prefix=require_prefix,
            exclude_prefix=exclude_prefix,
        )
        for normalized_name in self._load_downloaded_subreddits_from_config(session):
            if require_prefix and not normalized_name.startswith(require_prefix):
                continue
            if exclude_prefix and normalized_name.startswith(exclude_prefix):
                continue
            result.add(normalized_name)
        return result

    def _merge_downloaded_subreddits(self, names: set[str]) -> None:
        if not names:
            return

        with self._session_factory() as session:
            current = self._load_downloaded_subreddits_from_config(session)
            merged = sorted(current.union({n.lower() for n in names if n}))
            serialized = json.dumps(merged)
            row = session.scalar(select(AppConfig).where(AppConfig.key == REDDIT_DOWNLOADED_SUBREDDITS_KEY))
            if row:
                row.value = serialized
            else:
                session.add(AppConfig(key=REDDIT_DOWNLOADED_SUBREDDITS_KEY, value=serialized))
            session.commit()

    def clear_media_history(self) -> int:
        with self._session_factory() as session:
            total = int(session.scalar(select(func.count(RedditMediaHistory.id))) or 0)
            if total <= 0:
                return 0
            session.execute(delete(RedditMediaHistory))
            session.commit()
            return total

    def _filter_history_hits(self, posts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        if not posts:
            return [], 0

        item_keys: list[str] = []
        video_url_keys: list[str] = []
        for post in posts:
            history_entry = self._build_history_entry(post)
            if not history_entry:
                continue
            post["_history_entry"] = history_entry
            item_keys.append(str(history_entry["item_key"] or ""))
            video_url_keys.append(str(history_entry["video_url_key"] or ""))

        if not item_keys and not video_url_keys:
            return posts, 0

        existing_item_keys: set[str] = set()
        existing_video_url_keys: set[str] = set()
        with self._session_factory() as session:
            for chunk in self._iter_chunks(item_keys):
                stmt = select(RedditMediaHistory.item_key).where(RedditMediaHistory.item_key.in_(chunk))
                existing_item_keys.update(str(value) for value in session.scalars(stmt))
            for chunk in self._iter_chunks(video_url_keys):
                stmt = select(RedditMediaHistory.video_url_key).where(RedditMediaHistory.video_url_key.in_(chunk))
                existing_video_url_keys.update(str(value) for value in session.scalars(stmt))

        filtered: list[dict[str, Any]] = []
        skipped = 0
        for post in posts:
            history_entry = post.get("_history_entry")
            if isinstance(history_entry, dict):
                item_key = str(history_entry.get("item_key") or "")
                video_url_key = str(history_entry.get("video_url_key") or "")
                if item_key in existing_item_keys or video_url_key in existing_video_url_keys:
                    skipped += 1
                    continue
            filtered.append(post)
        return filtered, skipped

    def _record_downloaded_posts(self, posts: list[dict[str, Any]]) -> None:
        entries_by_key: dict[str, dict[str, str | None]] = {}
        for post in posts:
            history_entry = post.get("_history_entry")
            if not isinstance(history_entry, dict):
                history_entry = self._build_history_entry(post)
            if not isinstance(history_entry, dict):
                continue
            item_key = str(history_entry.get("item_key") or "")
            if not item_key:
                continue
            entries_by_key[item_key] = history_entry

        if not entries_by_key:
            return

        with self._session_factory() as session:
            existing_by_key: dict[str, RedditMediaHistory] = {}
            for chunk in self._iter_chunks(list(entries_by_key.keys())):
                stmt = select(RedditMediaHistory).where(RedditMediaHistory.item_key.in_(chunk))
                for row in session.scalars(stmt):
                    existing_by_key[row.item_key] = row

            now = utcnow()
            for item_key, history_entry in entries_by_key.items():
                row = existing_by_key.get(item_key)
                if row:
                    row.video_url_key = str(history_entry.get("video_url_key") or "")
                    row.subreddit = str(history_entry.get("subreddit") or "")
                    row.post_id = history_entry.get("post_id")
                    row.post_url = history_entry.get("post_url")
                    row.video_url = str(history_entry.get("video_url") or "")
                    row.title = history_entry.get("title")
                    row.updated_at = now
                    continue

                session.add(
                    RedditMediaHistory(
                        item_key=item_key,
                        video_url_key=str(history_entry.get("video_url_key") or ""),
                        subreddit=str(history_entry.get("subreddit") or ""),
                        post_id=history_entry.get("post_id"),
                        post_url=history_entry.get("post_url"),
                        video_url=str(history_entry.get("video_url") or ""),
                        title=history_entry.get("title"),
                    )
                )
            session.commit()

    def _get_blacklisted_categories(self) -> set[str]:
        with self._session_factory() as session:
            return load_blacklisted_categories(session)

    @staticmethod
    def _format_bytes(value: int) -> str:
        normalized = max(0, int(value))
        units = ["B", "KB", "MB", "GB", "TB"]
        amount = float(normalized)
        idx = 0
        while amount >= 1024 and idx < len(units) - 1:
            amount /= 1024
            idx += 1
        if idx == 0:
            return f"{int(amount)} {units[idx]}"
        return f"{amount:.1f} {units[idx]}"

    def _download_storage_targets(self) -> list[tuple[str, Path]]:
        candidates = [
            ("downloads Reddit", self._settings.reddit_download_dir),
            ("videos otimizados", self._settings.media_output_dir),
            ("thumbnails", self._settings.thumbnail_dir),
        ]
        unique_targets: dict[str, tuple[str, Path]] = {}
        for label, raw_path in candidates:
            target = raw_path
            target.mkdir(parents=True, exist_ok=True)
            try:
                resolved = target.resolve()
            except OSError:
                resolved = target
            key = (resolved.anchor or str(resolved)).lower()
            if key not in unique_targets:
                unique_targets[key] = (label, resolved)
        return list(unique_targets.values())

    def _get_min_download_free_bytes(self) -> int:
        default_gb = DEFAULT_MIN_DOWNLOAD_FREE_BYTES / (1024 ** 3)
        with self._session_factory() as session:
            row = session.scalar(select(AppConfig).where(AppConfig.key == REDDIT_MIN_DOWNLOAD_FREE_GB_KEY))
            raw_value = row.value if row else None
        configured_gb = parse_config_float(raw_value, default=default_gb)
        return max(1, int(configured_gb * (1024 ** 3)))

    def _ensure_minimum_free_space(self) -> None:
        min_download_free_bytes = self._get_min_download_free_bytes()
        for label, target in self._download_storage_targets():
            free_bytes = shutil.disk_usage(target).free
            if free_bytes >= min_download_free_bytes:
                continue
            raise InsufficientDiskSpaceError(
                f"Espaco insuficiente para downloads em {label}: "
                f"{self._format_bytes(free_bytes)} livres; minimo exigido "
                f"{self._format_bytes(min_download_free_bytes)}."
            )

    @staticmethod
    def _safe_subreddit_path_part(value: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value.strip())
        return cleaned or "unknown_subreddit"

    @staticmethod
    def _parse_created_at(value: str | None) -> datetime:
        if value:
            try:
                normalized = value.replace("Z", "+00:00")
                parsed = datetime.fromisoformat(normalized)
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except ValueError:
                pass
        return utcnow()

    @staticmethod
    def _build_file_name(post_id: str, video_url: str, extension: str) -> str:
        digest = hashlib.sha1(f"{post_id}|{video_url}".encode("utf-8")).hexdigest()[:16]
        stem = post_id.strip() if post_id.strip() else "post"
        stem = re.sub(r"[^a-zA-Z0-9_.-]+", "_", stem)
        return f"{stem}_{digest}{extension}"

    def _append_log(self, message: str) -> None:
        timestamp = utcnow().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        with self._lock:
            logs: list[str] = self._status["logs"]
            logs.append(line)
            if len(logs) > 400:
                del logs[:-400]

    def _update_item(self, subreddit: str, **changes: Any) -> None:
        normalized = self.normalize_subreddit_name(subreddit).lower()
        with self._lock:
            items: list[dict[str, Any]] = self._status["items"]
            for item in items:
                if self.normalize_subreddit_name(str(item.get("name") or "")).lower() != normalized:
                    continue
                item.update(changes)
                return

    def _increment(self, key: str, value: int = 1) -> None:
        with self._lock:
            self._status[key] = int(self._status.get(key, 0) or 0) + value

    def _set_message(self, message: str) -> None:
        with self._lock:
            self._status["message"] = message

    def _sync_total_videos_planned(self) -> None:
        with self._lock:
            items: list[dict[str, Any]] = self._status["items"]
            self._status["total_videos_planned"] = sum(int(item.get("planned") or 0) for item in items)

    @staticmethod
    def _format_duration(total_seconds: int) -> str:
        normalized_seconds = max(0, int(total_seconds))
        minutes, seconds = divmod(normalized_seconds, 60)
        if minutes > 0:
            return f"{minutes}m {seconds:02d}s"
        return f"{seconds}s"

    def _set_auto_waiting_status(self, idle_seconds: int) -> None:
        remaining_seconds = max(0, AUTO_IDLE_THRESHOLD_SECONDS - max(0, int(idle_seconds)))
        message = (
            "Aguardando backend ficar 5 min ocioso para baixar automaticamente os ultimos "
            f"{AUTO_PER_SUBREDDIT_LIMIT} videos de cada subreddit {AUTO_SUBREDDIT_PREFIX}."
            f" Ocioso ha {self._format_duration(idle_seconds)}; faltam {self._format_duration(remaining_seconds)}."
        )
        with self._lock:
            if self._status.get("state") == "running":
                return
            self._status["task_id"] = None
            self._status["state"] = "auto_waiting"
            self._status["message"] = message
            self._status["started_at"] = None
            self._status["finished_at"] = None
            self._status["total_subreddits"] = 0
            self._status["completed_subreddits"] = 0
            self._status["total_videos_planned"] = 0
            self._status["processed_videos"] = 0
            self._status["failed_videos"] = 0
            self._status["items"] = []
            self._status["trigger"] = None

    def _has_pending_or_running_jobs(self) -> bool:
        with self._session_factory() as session:
            count = session.scalar(
                select(func.count(Job.id)).where(Job.status.in_(["pending", "running"]))
            )
            return int(count or 0) > 0

    async def _load_auto_subreddit_names(self) -> list[str]:
        missing = self._reddit.missing_config_fields()
        if missing:
            raise RedditConfigError("Reddit env vars ausentes: " + ", ".join(missing))

        blacklisted = await asyncio.to_thread(self._get_blacklisted_categories)
        downloaded = await asyncio.to_thread(self.get_downloaded_subreddits)
        subscriptions = await self._reddit.list_subscribed_subreddits()
        names: list[str] = []
        seen: set[str] = set()
        for item in subscriptions:
            normalized = self.normalize_subreddit_name(str(item.get("name") or "")).strip()
            if not normalized:
                continue
            key = normalized.lower()
            if not key.startswith(AUTO_SUBREDDIT_PREFIX):
                continue
            if key in seen:
                continue
            if key in downloaded:
                continue
            if normalize_category_name(normalized) in blacklisted:
                continue
            seen.add(key)
            names.append(normalized)
        names.sort(key=str.lower)
        return names

    async def _load_empty_category_subreddit_names(self) -> list[str]:
        missing = self._reddit.missing_config_fields()
        if missing:
            raise RedditConfigError("Reddit env vars ausentes: " + ", ".join(missing))

        blacklisted = await asyncio.to_thread(self._get_blacklisted_categories)
        downloaded = await asyncio.to_thread(self.get_downloaded_categories)
        subscriptions = await self._reddit.list_subscribed_subreddits()
        names: list[str] = []
        seen: set[str] = set()
        for item in subscriptions:
            normalized = self.normalize_subreddit_name(str(item.get("name") or "")).strip()
            if not normalized:
                continue
            key = normalized.lower()
            if key.startswith(AUTO_SUBREDDIT_PREFIX):
                continue
            if key in seen:
                continue
            if key in downloaded:
                continue
            if normalize_category_name(normalized) in blacklisted:
                continue
            seen.add(key)
            names.append(normalized)
        names.sort(key=str.lower)
        return names

    async def start_download_for_empty_categories(self) -> dict[str, Any]:
        category_names = await self._load_empty_category_subreddit_names()
        if not category_names:
            raise ValueError("Nenhuma categoria sem videos salvos foi encontrada.")
        return await self.start_download(
            subreddit_names=category_names,
            per_subreddit_limit=AUTO_PER_SUBREDDIT_LIMIT,
            period_months=None,
            trigger="manual",
        )

    async def _run_auto_download_monitor(self) -> None:
        try:
            while True:
                task = self._task
                if task and not task.done():
                    await asyncio.sleep(AUTO_POLL_INTERVAL_SECONDS)
                    continue

                now = utcnow()
                auto_enabled = await asyncio.to_thread(self.is_auto_download_enabled)
                if not auto_enabled:
                    self._last_busy_at = now
                    self._last_auto_attempt_at = None
                    self._set_auto_disabled_status()
                    await asyncio.sleep(AUTO_POLL_INTERVAL_SECONDS)
                    continue

                has_jobs = await asyncio.to_thread(self._has_pending_or_running_jobs)
                if has_jobs:
                    self._last_busy_at = now
                    self._last_auto_attempt_at = None
                    self._set_auto_waiting_status(idle_seconds=0)
                    await asyncio.sleep(AUTO_POLL_INTERVAL_SECONDS)
                    continue

                idle_seconds = max(0, int((now - self._last_busy_at).total_seconds()))
                self._set_auto_waiting_status(idle_seconds=idle_seconds)
                if idle_seconds < AUTO_IDLE_THRESHOLD_SECONDS:
                    await asyncio.sleep(AUTO_POLL_INTERVAL_SECONDS)
                    continue

                if self._last_auto_attempt_at is not None:
                    seconds_since_attempt = max(0, int((now - self._last_auto_attempt_at).total_seconds()))
                    if seconds_since_attempt < AUTO_RETRY_INTERVAL_SECONDS:
                        await asyncio.sleep(AUTO_POLL_INTERVAL_SECONDS)
                        continue

                self._last_auto_attempt_at = now
                try:
                    subreddit_names = await self._load_auto_subreddit_names()
                except RedditConfigError as exc:
                    self._append_log(f"[AUTO] Ignorado: {exc}")
                    self._set_auto_waiting_status(idle_seconds=idle_seconds)
                    await asyncio.sleep(AUTO_POLL_INTERVAL_SECONDS)
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Falha ao carregar subreddits para auto-download Reddit")
                    self._append_log(f"[AUTO] Falha ao carregar subreddits u_: {exc}")
                    self._set_auto_waiting_status(idle_seconds=idle_seconds)
                    await asyncio.sleep(AUTO_POLL_INTERVAL_SECONDS)
                    continue

                if not subreddit_names:
                    self._append_log(
                        "[AUTO] Backend ocioso por 5 min, mas nao encontrei subreddits u_ pendentes sem download."
                    )
                    self._set_auto_waiting_status(idle_seconds=idle_seconds)
                    await asyncio.sleep(AUTO_POLL_INTERVAL_SECONDS)
                    continue

                self._append_log(
                    "[AUTO] Backend ocioso por 5 min. "
                    f"Iniciando download dos ultimos {AUTO_PER_SUBREDDIT_LIMIT} videos para "
                    f"{len(subreddit_names)} subreddit(s) {AUTO_SUBREDDIT_PREFIX}."
                )
                try:
                    await self.start_download(
                        subreddit_names=subreddit_names,
                        per_subreddit_limit=AUTO_PER_SUBREDDIT_LIMIT,
                        period_months=None,
                        trigger="idle_auto",
                    )
                except RuntimeError:
                    pass
                except InsufficientDiskSpaceError as exc:
                    self._append_log(f"[AUTO] Download bloqueado por espaco insuficiente: {exc}")
                    self._set_auto_waiting_status(idle_seconds=idle_seconds)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Falha ao iniciar auto-download Reddit")
                    self._append_log(f"[AUTO] Falha ao iniciar auto-download: {exc}")
                    self._set_auto_waiting_status(idle_seconds=idle_seconds)

                await asyncio.sleep(AUTO_POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise

    async def start_download(
        self,
        subreddit_names: list[str],
        per_subreddit_limit: int | None = 10,
        period_months: int | None = None,
        trigger: str = "manual",
    ) -> dict[str, Any]:
        missing = self._reddit.missing_config_fields()
        if missing:
            raise RedditConfigError("Reddit env vars ausentes: " + ", ".join(missing))

        normalized_names = []
        seen: set[str] = set()
        for raw_name in subreddit_names:
            normalized = self.normalize_subreddit_name(str(raw_name)).strip()
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized_names.append(normalized)

        if not normalized_names:
            raise ValueError("Nenhum subreddit selecionado.")

        blacklisted = self._get_blacklisted_categories()
        normalized_names = [
            name for name in normalized_names if normalize_category_name(name) not in blacklisted
        ]

        if not normalized_names:
            raise ValueError("Todos os subreddits selecionados estao na blacklist.")

        self._ensure_minimum_free_space()

        allowed_period_months = {1, 2, 3, 6, 12}
        normalized_period_months = None
        if period_months is not None:
            normalized_period_months = int(period_months)
            if normalized_period_months not in allowed_period_months:
                raise ValueError("Periodo invalido. Use 1, 2, 3, 6 ou 12 meses.")

        limit = None
        if normalized_period_months is None:
            limit = max(1, min(int(per_subreddit_limit or 10), 50))
        task_id = hashlib.sha1(f"{utcnow().isoformat()}|{normalized_names}".encode("utf-8")).hexdigest()[:16]

        with self._lock:
            if self._status.get("state") == "running":
                raise RuntimeError("Ja existe um download Reddit em execucao.")
            self._status = {
                "task_id": task_id,
                "state": "running",
                "message": "Download automatico iniciado." if trigger == "idle_auto" else "Download iniciado.",
                "started_at": utcnow(),
                "finished_at": None,
                "total_subreddits": len(normalized_names),
                "completed_subreddits": 0,
                "total_videos_planned": 0,
                "processed_videos": 0,
                "failed_videos": 0,
                "items": [
                    {
                        "name": name,
                        "status": "pending",
                        "planned": 0,
                        "completed": 0,
                        "failed": 0,
                        "marked_downloaded": False,
                    }
                    for name in normalized_names
                ],
                "logs": [],
                "trigger": trigger,
            }
        self._last_busy_at = utcnow()
        self._last_auto_attempt_at = None

        if normalized_period_months is None:
            self._append_log(
                f"Iniciando download dos ultimos {limit} videos para {len(normalized_names)} subreddit(s)."
            )
        else:
            self._append_log(
                f"Iniciando download do periodo de {normalized_period_months} mes(es) para {len(normalized_names)} subreddit(s)."
            )
        self._task = asyncio.create_task(
            self._run_download(
                task_id=task_id,
                subreddit_names=normalized_names,
                limit=limit,
                period_months=normalized_period_months,
            ),
            name=f"reddit-download-{task_id}",
        )
        self._task.add_done_callback(self._handle_download_task_done)
        return self.get_status()

    def _handle_download_task_done(self, task: asyncio.Task) -> None:
        with self._lock:
            if self._task is task:
                self._task = None
        self._last_busy_at = utcnow()
        self._last_auto_attempt_at = None

    async def stop_download(
        self,
        reason: str = "Download interrompido manualmente pelo admin.",
    ) -> dict[str, Any]:
        task = self._task
        was_running = False
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._task = None
        with self._lock:
            if self._status.get("state") == "running":
                was_running = True
                self._status["state"] = "stopped"
                self._status["message"] = reason
                self._status["finished_at"] = utcnow()
        if was_running:
            self._append_log(reason)
            self._last_busy_at = utcnow()
            self._last_auto_attempt_at = None
        return self.get_status()

    async def stop(self) -> None:
        auto_task = self._auto_task
        if auto_task and not auto_task.done():
            auto_task.cancel()
            with suppress(asyncio.CancelledError):
                await auto_task
        self._auto_task = None
        await self.stop_download(reason="Download interrompido no shutdown.")
        with self._lock:
            if self._status.get("state") in {"auto_waiting", "auto_disabled"}:
                self._status["state"] = "stopped"
                self._status["message"] = "Auto-download interrompido no shutdown."
                self._status["finished_at"] = utcnow()

    async def _download_video_file(
        self,
        client: httpx.AsyncClient,
        subreddit: str,
        post: dict[str, Any],
    ) -> Path:
        created_at_utc = self._parse_created_at(str(post.get("created_at_utc") or ""))
        created_local_date = created_at_utc.astimezone().date().isoformat()
        subreddit_dir = self._safe_subreddit_path_part(subreddit)
        target_dir = self._settings.reddit_download_dir / subreddit_dir / "videos" / created_local_date
        target_dir.mkdir(parents=True, exist_ok=True)

        video_url = str(post.get("video_url") or "").strip()
        if not video_url:
            raise ValueError("URL de video ausente.")

        parsed_url = urlparse(video_url)
        ext = Path(parsed_url.path).suffix.lower()
        if ext not in {".mp4", ".m4v", ".mov", ".webm"}:
            ext = ".mp4"
        file_name = self._build_file_name(str(post.get("post_id") or ""), video_url, ext)
        target_path = target_dir / file_name
        temp_path = target_dir / f"{file_name}.part"

        if target_path.exists():
            return target_path

        headers = {"User-Agent": self._settings.reddit_user_agent}
        bytes_since_space_check = 0
        try:
            async with client.stream("GET", video_url, headers=headers, follow_redirects=True) as response:
                response.raise_for_status()
                with temp_path.open("wb") as output:
                    async for chunk in response.aiter_bytes(chunk_size=256 * 1024):
                        if chunk:
                            output.write(chunk)
                            bytes_since_space_check += len(chunk)
                            if bytes_since_space_check >= DOWNLOAD_DISK_RECHECK_BYTES:
                                self._ensure_minimum_free_space()
                                bytes_since_space_check = 0

            temp_path.replace(target_path)
        except Exception:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            raise
        return target_path

    async def _run_download(
        self,
        task_id: str,
        subreddit_names: list[str],
        limit: int | None,
        period_months: int | None,
    ) -> None:
        downloaded_to_mark: set[str] = set()
        timeout = httpx.Timeout(connect=20.0, read=300.0, write=300.0, pool=30.0)
        cutoff_datetime = None
        max_pages = None
        if period_months is not None:
            period_days_map = {
                1: 30,
                2: 60,
                3: 90,
                6: 180,
                12: 365,
            }
            cutoff_datetime = utcnow() - timedelta(days=period_days_map.get(period_months, 30))
            max_pages_map = {
                1: 40,
                2: 60,
                3: 80,
                6: 120,
                12: 180,
            }
            max_pages = max_pages_map.get(period_months, 40)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                for subreddit in subreddit_names:
                    if normalize_category_name(subreddit) in self._get_blacklisted_categories():
                        self._update_item(subreddit, status="skipped_blacklisted")
                        self._increment("completed_subreddits", 1)
                        self._append_log(f"[{subreddit}] Ignorado: categoria em blacklist.")
                        continue

                    self._update_item(subreddit, status="loading")
                    self._set_message(f"Preparando busca em r/{subreddit}...")
                    if period_months is None:
                        self._append_log(f"[{subreddit}] Buscando videos recentes...")
                    else:
                        self._append_log(f"[{subreddit}] Buscando videos dos ultimos {period_months} mes(es)...")

                    def report_progress(progress: dict[str, Any]) -> None:
                        found = int(progress.get("found") or 0)
                        page = int(progress.get("page") or 0)
                        oldest_raw = str(progress.get("oldest_created_at") or "").strip()
                        oldest_label = oldest_raw.split("T", maxsplit=1)[0] if oldest_raw else ""
                        reached_cutoff = bool(progress.get("reached_cutoff"))
                        detail = (
                            f"[{subreddit}] Busca em andamento: pagina {page}, {found} video(s) encontrado(s)"
                            + (f", mais antigo {oldest_label}" if oldest_label else "")
                            + (" (limite do periodo atingido)" if reached_cutoff else "")
                        )
                        self._update_item(subreddit, status="scanning", planned=found)
                        self._sync_total_videos_planned()
                        self._set_message(
                            f"Buscando em r/{subreddit}: pagina {page}, {found} video(s) encontrado(s) no periodo."
                        )
                        self._append_log(detail)

                    try:
                        posts = await self._reddit.list_latest_video_posts(
                            subreddit=subreddit,
                            limit=limit,
                            cutoff_datetime=cutoff_datetime,
                            max_pages=max_pages,
                            progress_callback=report_progress if period_months is not None else None,
                        )
                    except Exception as exc:  # noqa: BLE001
                        self._update_item(subreddit, status="failed")
                        self._increment("failed_videos", 1)
                        self._set_message(f"Falha ao buscar videos em r/{subreddit}.")
                        self._append_log(f"[{subreddit}] Falha ao buscar videos: {exc}")
                        self._increment("completed_subreddits", 1)
                        continue

                    posts, skipped_due_to_history = self._filter_history_hits(posts)
                    planned = len(posts)
                    self._update_item(subreddit, status="downloading", planned=planned)
                    self._sync_total_videos_planned()
                    if skipped_due_to_history > 0:
                        downloaded_to_mark.add(self.normalize_subreddit_name(subreddit).lower())
                        self._update_item(subreddit, marked_downloaded=True)
                        self._append_log(
                            f"[{subreddit}] Ignorados {skipped_due_to_history} video(s) ja baixados anteriormente."
                        )
                    self._set_message(f"Baixando {planned} video(s) de r/{subreddit}...")
                    if planned == 0:
                        self._update_item(subreddit, status="completed")
                        if skipped_due_to_history > 0:
                            self._set_message(
                                f"Nenhum video novo encontrado em r/{subreddit}; {skipped_due_to_history} ja estavam no historico."
                            )
                            self._append_log(
                                f"[{subreddit}] Nenhum video novo para baixar; tudo ja estava no historico."
                            )
                        else:
                            self._set_message(f"Nenhum video encontrado em r/{subreddit}.")
                        self._increment("completed_subreddits", 1)
                        if skipped_due_to_history == 0:
                            self._append_log(f"[{subreddit}] Nenhum video encontrado.")
                        continue

                    completed = 0
                    failed = 0
                    skipped_due_to_blacklist = False
                    for idx, post in enumerate(posts, start=1):
                        if normalize_category_name(subreddit) in self._get_blacklisted_categories():
                            skipped_due_to_blacklist = True
                            self._update_item(subreddit, status="skipped_blacklisted")
                            self._set_message(f"Download de r/{subreddit} interrompido por blacklist.")
                            self._append_log(f"[{subreddit}] Interrompido: categoria entrou na blacklist.")
                            break
                        self._ensure_minimum_free_space()
                        self._append_log(f"[{subreddit}] Baixando video {idx}/{planned}...")
                        self._set_message(f"Baixando video {idx}/{planned} de r/{subreddit}...")
                        try:
                            source_path = await self._download_video_file(client=client, subreddit=subreddit, post=post)
                            await asyncio.to_thread(
                                self._ingest.process_source_now,
                                str(source_path),
                                None,
                                True,
                                True,
                            )
                            completed += 1
                            self._increment("processed_videos", 1)
                            self._update_item(subreddit, completed=completed, failed=failed)
                            try:
                                self._record_downloaded_posts([post])
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "Failed to persist reddit media history for subreddit=%s post_id=%s error=%s",
                                    subreddit,
                                    post.get("post_id"),
                                    exc,
                                )
                                self._append_log(
                                    f"[{subreddit}] Aviso: falha ao salvar historico do post {post.get('post_id') or '?'}: {exc}"
                                )
                        except Exception as exc:  # noqa: BLE001
                            failed += 1
                            self._increment("failed_videos", 1)
                            self._update_item(subreddit, completed=completed, failed=failed)
                            self._append_log(f"[{subreddit}] Erro no video {idx}/{planned}: {exc}")

                    if skipped_due_to_blacklist:
                        self._increment("completed_subreddits", 1)
                        continue

                    if completed > 0 or skipped_due_to_history > 0:
                        downloaded_to_mark.add(self.normalize_subreddit_name(subreddit).lower())
                        self._update_item(subreddit, marked_downloaded=True)

                    if failed > 0 and completed == 0:
                        self._update_item(subreddit, status="failed")
                    elif failed > 0:
                        self._update_item(subreddit, status="completed_with_errors")
                    else:
                        self._update_item(subreddit, status="completed")

                    self._increment("completed_subreddits", 1)
                    self._set_message(
                        f"r/{subreddit} concluido: {completed} sucesso(s), {failed} falha(s)."
                    )
                    self._append_log(
                        f"[{subreddit}] Concluido: {completed} sucesso(s), {failed} falha(s)."
                    )

            if downloaded_to_mark:
                await asyncio.to_thread(self._merge_downloaded_subreddits, downloaded_to_mark)

            with self._lock:
                failed_videos = int(self._status.get("failed_videos", 0) or 0)
                self._status["state"] = "completed_with_errors" if failed_videos > 0 else "completed"
                self._status["message"] = (
                    "Download finalizado com erros."
                    if failed_videos > 0
                    else "Download finalizado com sucesso."
                )
                self._status["finished_at"] = utcnow()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Falha no pipeline de download Reddit")
            with self._lock:
                self._status["state"] = "failed"
                self._status["message"] = f"Falha geral: {exc}"
                self._status["finished_at"] = utcnow()
            self._append_log(f"Falha geral no download Reddit: {exc}")
