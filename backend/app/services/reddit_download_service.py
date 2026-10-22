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
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.models import AppConfig, Job, RedditImage, RedditMediaHistory, Video
from app.services.category_blacklist import (
    extract_download_root,
    extract_path_badges,
    load_blacklisted_categories,
    normalize_category_name,
)
from app.services.file_utils import normalize_path
from app.services.ingest_service import IngestService
from app.services.image_ingest_service import ImageIngestService
from app.services.reddit_service import RedditConfigError, RedditService


logger = logging.getLogger("reddit_download")

REDDIT_DOWNLOADED_SUBREDDITS_KEY = "reddit_downloaded_subreddits"
REDDIT_DOWNLOADED_IMAGE_SUBREDDITS_KEY = "reddit_downloaded_image_subreddits"
REDDIT_IDLE_AUTO_DOWNLOAD_ENABLED_KEY = "reddit_idle_auto_download_enabled"
REDDIT_IDLE_AUTO_IMAGE_DOWNLOAD_ENABLED_KEY = "reddit_idle_auto_image_download_enabled"
REDDIT_IDLE_IMAGE_COUNT_ENABLED_KEY = "reddit_idle_image_count_enabled"
REDDIT_USER_YEAR_IMAGE_COUNTS_KEY = "reddit_user_year_image_counts_v1"
REDDIT_SUBSCRIPTIONS_CACHE_KEY = "reddit_subscriptions_cache_v1"
REDDIT_MIN_DOWNLOAD_FREE_GB_KEY = "reddit_min_download_free_gb"
AUTO_IDLE_THRESHOLD_SECONDS = 5 * 60
AUTO_POLL_INTERVAL_SECONDS = 15
AUTO_PER_SUBREDDIT_LIMIT = 5
AUTO_SUBREDDIT_PREFIX = "u_"
IMAGE_COUNT_IDLE_THRESHOLD_SECONDS = 60
IMAGE_COUNT_POLL_INTERVAL_SECONDS = 15
IMAGE_COUNT_PERIOD_MONTHS = 12
IMAGE_COUNT_PERIOD_DAYS = 365
IMAGE_COUNT_MAX_PAGES = 180
IMAGE_COUNT_CONCURRENCY = 8
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
        image_ingest_service: ImageIngestService,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._reddit = reddit_service
        self._ingest = ingest_service
        self._image_ingest = image_ingest_service
        self._settings = settings
        self._lock = Lock()
        self._task: asyncio.Task | None = None
        self._auto_task: asyncio.Task | None = None
        self._image_count_monitor_task: asyncio.Task | None = None
        self._status: dict[str, Any] = self._build_idle_status()
        self._image_count_scan: dict[str, Any] = {
            "active": False,
            "current": None,
            "message": None,
        }
        self._last_busy_at = utcnow()
        self._last_auto_attempt_at: datetime | None = None
        self._image_count_scan_active = False
        self._immediate_image_task: asyncio.Task | None = None

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

    @staticmethod
    def _normalize_history_media_type(value: str | None) -> str:
        normalized = str(value or "video").strip().lower()
        if normalized in {"image", "images"}:
            return "image"
        return "video"

    @classmethod
    def _build_history_entry(
        cls,
        post: dict[str, Any],
        media_type: str = "video",
    ) -> dict[str, str | None] | None:
        normalized_media_type = cls._normalize_history_media_type(media_type)
        if normalized_media_type == "image":
            media_url = cls._normalize_history_video_url(str(post.get("image_url") or "").strip())
        else:
            media_url = cls._normalize_history_video_url(str(post.get("video_url") or "").strip())
        if not media_url:
            return None

        post_id = str(post.get("post_id") or "").strip()
        item_source = f"{post_id}|{media_url}" if post_id else media_url
        subreddit = cls.normalize_subreddit_name(str(post.get("subreddit") or "")).lower()
        return {
            "item_key": hashlib.sha1(item_source.encode("utf-8")).hexdigest(),
            "video_url_key": hashlib.sha1(media_url.encode("utf-8")).hexdigest(),
            "subreddit": subreddit,
            "post_id": post_id or None,
            "post_url": str(post.get("post_url") or "").strip() or None,
            "video_url": media_url,
            "title": str(post.get("title") or "").strip() or None,
            "media_type": normalized_media_type,
            "is_reddit_saved": bool(post.get("is_reddit_saved")),
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
            "media_type": "videos",
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
        auto_image_enabled = self.is_auto_image_download_enabled()
        status["auto_enabled"] = auto_enabled
        status["auto_image_enabled"] = auto_image_enabled
        status["idle_image_count_enabled"] = self.is_idle_image_count_enabled()
        status["user_year_image_counts"] = self.get_user_year_image_counts_map()
        with self._lock:
            status["image_count_scan"] = copy.deepcopy(self._image_count_scan)
        if not auto_enabled and not auto_image_enabled:
            self._apply_auto_disabled_status(status)
        return status

    @staticmethod
    def _apply_auto_disabled_status(status: dict[str, Any]) -> None:
        current_state = str(status.get("state") or "").lower()
        if current_state not in {"idle", "auto_waiting", "auto_disabled"}:
            return
        status["task_id"] = None
        status["state"] = "auto_disabled"
        status["message"] = (
            "Auto-download quando ocioso desativado (videos e imagens). "
            "Downloads manuais continuam disponiveis."
        )
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
            if normalized or self.is_auto_image_download_enabled():
                self._set_auto_waiting_status(idle_seconds=0)
            else:
                self._set_auto_disabled_status()

        if current != normalized:
            action = "ativado" if normalized else "desativado"
            self._append_log(f"[AUTO] Auto-download de videos quando ocioso {action} pelo admin.")
        return normalized

    def is_auto_image_download_enabled(self) -> bool:
        with self._session_factory() as session:
            row = session.scalar(
                select(AppConfig).where(AppConfig.key == REDDIT_IDLE_AUTO_IMAGE_DOWNLOAD_ENABLED_KEY)
            )
            raw_value = row.value if row else None
        return parse_config_bool(raw_value, default=True)

    def set_auto_image_download_enabled(self, enabled: bool) -> bool:
        normalized = bool(enabled)
        current = self.is_auto_image_download_enabled()
        serialized = "true" if normalized else "false"
        with self._session_factory() as session:
            row = session.scalar(
                select(AppConfig).where(AppConfig.key == REDDIT_IDLE_AUTO_IMAGE_DOWNLOAD_ENABLED_KEY)
            )
            if row:
                row.value = serialized
            else:
                session.add(AppConfig(key=REDDIT_IDLE_AUTO_IMAGE_DOWNLOAD_ENABLED_KEY, value=serialized))
            session.commit()

        self._last_auto_attempt_at = None
        if normalized:
            # Consider already idle so the monitor can resume quickly if immediate start is busy.
            self._last_busy_at = utcnow() - timedelta(seconds=AUTO_IDLE_THRESHOLD_SECONDS)
        else:
            self._last_busy_at = utcnow()
        current_state = str(self._clone_status().get("state") or "").lower()
        if current_state in {"idle", "auto_waiting", "auto_disabled"}:
            if normalized or self.is_auto_download_enabled():
                idle_seconds = AUTO_IDLE_THRESHOLD_SECONDS if normalized else 0
                self._set_auto_waiting_status(idle_seconds=idle_seconds)
            else:
                self._set_auto_disabled_status()

        if current != normalized:
            action = "ativado" if normalized else "desativado"
            self._append_log(f"[AUTO] Auto-download de imagens quando ocioso {action} pelo admin.")
        return normalized

    def schedule_auto_image_download_now(self) -> dict[str, Any]:
        """Persist-and-return quickly; kick image download in a background task."""
        pending = self._immediate_image_task
        if pending and not pending.done():
            self._append_log("[AUTO] Auto-download de imagens ja esta sendo iniciado em segundo plano.")
            return self.get_status()

        download_task = self._task
        if download_task and not download_task.done():
            self._append_log(
                "[AUTO] Auto-download de imagens ativado, mas ja existe download em execucao; "
                "as imagens pendentes entram assim que a fila atual terminar."
            )
            return self.get_status()

        self._append_log(
            "[AUTO] Auto-download de imagens ativado; iniciando download em segundo plano..."
        )
        self._immediate_image_task = asyncio.create_task(
            self._run_auto_image_download_now(),
            name="reddit-auto-image-now",
        )
        return self.get_status()

    async def _run_auto_image_download_now(self) -> None:
        task = self._task
        if task and not task.done():
            self._append_log(
                "[AUTO] Auto-download de imagens aguardando: ja existe download em execucao."
            )
            return

        try:
            subreddit_names = await self._load_auto_image_subreddit_names()
        except RedditConfigError as exc:
            self._append_log(f"[AUTO] Auto-download de imagens ativado, mas Reddit nao configurado: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Falha ao carregar subreddits para auto-download imediato de imagens")
            self._append_log(f"[AUTO] Falha ao carregar subreddits u_ para imagens: {exc}")
            return

        if not subreddit_names:
            self._append_log(
                "[AUTO] Auto-download de imagens ativado, mas nao ha usuarios u_ pendentes "
                "sem download de imagens."
            )
            return

        self._append_log(
            "[AUTO] Iniciando agora o download de todas as imagens do ultimo ano "
            f"(ainda nao baixadas) para {len(subreddit_names)} subreddit(s) {AUTO_SUBREDDIT_PREFIX}."
        )
        self._last_auto_attempt_at = utcnow()
        try:
            await self.start_download(
                subreddit_names=subreddit_names,
                per_subreddit_limit=None,
                period_months=12,
                trigger="idle_auto_images",
                media_type="images",
            )
        except RuntimeError:
            self._append_log(
                "[AUTO] Nao foi possivel iniciar agora: ja existe download em execucao."
            )
        except InsufficientDiskSpaceError as exc:
            self._append_log(f"[AUTO] Download de imagens bloqueado por espaco insuficiente: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Falha ao iniciar auto-download imediato de imagens")
            self._append_log(f"[AUTO] Falha ao iniciar download imediato de imagens: {exc}")

    def is_idle_image_count_enabled(self) -> bool:
        with self._session_factory() as session:
            row = session.scalar(select(AppConfig).where(AppConfig.key == REDDIT_IDLE_IMAGE_COUNT_ENABLED_KEY))
            raw_value = row.value if row else None
        return parse_config_bool(raw_value, default=False)

    def set_idle_image_count_enabled(self, enabled: bool) -> bool:
        normalized = bool(enabled)
        current = self.is_idle_image_count_enabled()
        serialized = "true" if normalized else "false"
        with self._session_factory() as session:
            row = session.scalar(select(AppConfig).where(AppConfig.key == REDDIT_IDLE_IMAGE_COUNT_ENABLED_KEY))
            if row:
                row.value = serialized
            else:
                session.add(AppConfig(key=REDDIT_IDLE_IMAGE_COUNT_ENABLED_KEY, value=serialized))
            session.commit()

        self._last_busy_at = utcnow()
        if current != normalized:
            action = "ativada" if normalized else "desativada"
            self._append_log(
                f"[IMG-COUNT] Verificacao ociosa de imagens/ano ({action}) pelo admin."
            )
        return normalized

    def get_user_year_image_counts_map(self) -> dict[str, int]:
        stored = self._load_user_year_image_counts()
        result: dict[str, int] = {}
        for key, payload in stored.items():
            if not isinstance(payload, dict):
                continue
            count = payload.get("count")
            try:
                result[key] = max(0, int(count))
            except (TypeError, ValueError):
                continue
        return result

    def _load_user_year_image_counts(self) -> dict[str, dict[str, Any]]:
        with self._session_factory() as session:
            row = session.scalar(select(AppConfig).where(AppConfig.key == REDDIT_USER_YEAR_IMAGE_COUNTS_KEY))
            if not row or not row.value:
                return {}
            try:
                payload = json.loads(row.value)
            except json.JSONDecodeError:
                return {}
        if not isinstance(payload, dict):
            return {}
        normalized: dict[str, dict[str, Any]] = {}
        for raw_key, raw_value in payload.items():
            key = self.normalize_subreddit_name(str(raw_key or "")).lower()
            if not key.startswith(AUTO_SUBREDDIT_PREFIX):
                continue
            if not isinstance(raw_value, dict):
                continue
            normalized[key] = raw_value
        return normalized

    def _save_user_year_image_count(self, subreddit: str, count: int) -> None:
        key = self.normalize_subreddit_name(subreddit).lower()
        if not key.startswith(AUTO_SUBREDDIT_PREFIX):
            return
        stored = self._load_user_year_image_counts()
        stored[key] = {
            "count": max(0, int(count)),
            "verified_at": utcnow().isoformat(),
            "period_months": IMAGE_COUNT_PERIOD_MONTHS,
            "period_days": IMAGE_COUNT_PERIOD_DAYS,
        }
        serialized = json.dumps(stored, ensure_ascii=True, separators=(",", ":"))
        with self._session_factory() as session:
            row = session.scalar(select(AppConfig).where(AppConfig.key == REDDIT_USER_YEAR_IMAGE_COUNTS_KEY))
            if row:
                row.value = serialized
            else:
                session.add(AppConfig(key=REDDIT_USER_YEAR_IMAGE_COUNTS_KEY, value=serialized))
            session.commit()

    def _is_user_year_image_count_verified(self, subreddit: str) -> bool:
        key = self.normalize_subreddit_name(subreddit).lower()
        return key in self._load_user_year_image_counts()

    def _set_image_count_scan(self, *, active: bool, current: str | None = None, message: str | None = None) -> None:
        with self._lock:
            self._image_count_scan_active = bool(active)
            self._image_count_scan = {
                "active": bool(active),
                "current": current,
                "message": message,
            }

    async def start(self) -> None:
        self._last_auto_attempt_at = None
        video_auto = self.is_auto_download_enabled()
        image_auto = self.is_auto_image_download_enabled()
        if video_auto or image_auto:
            self._set_auto_waiting_status(idle_seconds=0)
            armed: list[str] = []
            if video_auto:
                armed.append(f"ultimos {AUTO_PER_SUBREDDIT_LIMIT} videos")
            if image_auto:
                armed.append("todas as imagens do ultimo ano ainda nao baixadas")
            self._append_log(
                "Auto-download armado: quando o backend ficar 5 min ocioso, vou baixar "
                f"{' / '.join(armed)} de cada subreddit com prefixo {AUTO_SUBREDDIT_PREFIX}."
            )
        else:
            self._set_auto_disabled_status()
            self._append_log("[AUTO] Inicializado em modo desativado pelo admin.")

        auto_task = self._auto_task
        if auto_task is None or auto_task.done():
            self._auto_task = asyncio.create_task(
                self._run_auto_download_monitor(),
                name="reddit-download-auto-monitor",
            )

        # Contagem de imagens/ano e manual (botao no admin), nao mais por ociosidade.
        if self.is_idle_image_count_enabled():
            self.set_idle_image_count_enabled(False)
        self._append_log(
            "[IMG-COUNT] Use o botao 'Verificar qtd. imagens' no admin para contar imagens (1 ano) dos usuarios u_."
        )

    def get_downloaded_subreddits(self) -> set[str]:
        with self._session_factory() as session:
            return self._load_downloaded_names(
                session,
                require_prefix=AUTO_SUBREDDIT_PREFIX,
            )

    def get_downloaded_image_subreddits(self) -> set[str]:
        with self._session_factory() as session:
            return self._load_downloaded_image_names(
                session,
                require_prefix=AUTO_SUBREDDIT_PREFIX,
            )

    def get_downloaded_categories(self) -> set[str]:
        with self._session_factory() as session:
            return self._load_downloaded_names(
                session,
                exclude_prefix=AUTO_SUBREDDIT_PREFIX,
            )

    @staticmethod
    def _load_name_set_from_config(session: Session, key: str) -> set[str]:
        row = session.scalar(select(AppConfig).where(AppConfig.key == key))
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
            normalized = RedditDownloadService.normalize_subreddit_name(item).lower()
            if normalized:
                result.add(normalized)
        return result

    def _load_downloaded_subreddits_from_config(self, session: Session) -> set[str]:
        return self._load_name_set_from_config(session, REDDIT_DOWNLOADED_SUBREDDITS_KEY)

    def _load_downloaded_image_subreddits_from_config(self, session: Session) -> set[str]:
        return self._load_name_set_from_config(session, REDDIT_DOWNLOADED_IMAGE_SUBREDDITS_KEY)

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
            category, _, _ = extract_path_badges(normalized_path)
            normalized_category = normalize_category_name(category or "")
            if not normalized_category:
                continue
            if require_prefix and not normalized_category.startswith(require_prefix):
                continue
            if exclude_prefix and normalized_category.startswith(exclude_prefix):
                continue
            result.add(normalized_category)
        return result

    def _load_cataloged_image_subreddits_from_catalog(
        self,
        session: Session,
        *,
        require_prefix: str | None = None,
        exclude_prefix: str | None = None,
    ) -> set[str]:
        stmt = select(RedditImage.original_path).where(RedditImage.is_deleted.is_(False))
        result: set[str] = set()
        for original_path in session.scalars(stmt):
            normalized_path = normalize_path(str(original_path))
            if not extract_download_root(normalized_path, self._settings.reddit_download_dir):
                continue
            category, _, _ = extract_path_badges(normalized_path)
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

    def _load_downloaded_image_names(
        self,
        session: Session,
        *,
        require_prefix: str | None = None,
        exclude_prefix: str | None = None,
    ) -> set[str]:
        result = self._load_cataloged_image_subreddits_from_catalog(
            session,
            require_prefix=require_prefix,
            exclude_prefix=exclude_prefix,
        )
        for normalized_name in self._load_downloaded_image_subreddits_from_config(session):
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

    def _merge_downloaded_image_subreddits(self, names: set[str]) -> None:
        if not names:
            return

        with self._session_factory() as session:
            current = self._load_downloaded_image_subreddits_from_config(session)
            merged = sorted(current.union({n.lower() for n in names if n}))
            serialized = json.dumps(merged)
            row = session.scalar(
                select(AppConfig).where(AppConfig.key == REDDIT_DOWNLOADED_IMAGE_SUBREDDITS_KEY)
            )
            if row:
                row.value = serialized
            else:
                session.add(AppConfig(key=REDDIT_DOWNLOADED_IMAGE_SUBREDDITS_KEY, value=serialized))
            session.commit()

    def clear_media_history(self, media_type: str | None = None) -> int:
        normalized_media_type = (
            self._normalize_history_media_type(media_type) if media_type is not None else None
        )
        with self._session_factory() as session:
            count_stmt = select(func.count(RedditMediaHistory.id))
            delete_stmt = delete(RedditMediaHistory)
            if normalized_media_type is not None:
                count_stmt = count_stmt.where(RedditMediaHistory.media_type == normalized_media_type)
                delete_stmt = delete_stmt.where(RedditMediaHistory.media_type == normalized_media_type)
            total = int(session.scalar(count_stmt) or 0)
            if total <= 0:
                return 0
            session.execute(delete_stmt)
            session.commit()
            return total

    def clear_downloaded_image_subreddits(self) -> bool:
        with self._session_factory() as session:
            row = session.scalar(
                select(AppConfig).where(AppConfig.key == REDDIT_DOWNLOADED_IMAGE_SUBREDDITS_KEY)
            )
            if not row:
                return False
            session.delete(row)
            session.commit()
            return True

    def _filter_history_hits(
        self,
        posts: list[dict[str, Any]],
        media_type: str = "video",
    ) -> tuple[list[dict[str, Any]], int]:
        if not posts:
            return [], 0

        item_keys: list[str] = []
        video_url_keys: list[str] = []
        for post in posts:
            history_entry = self._build_history_entry(post, media_type=media_type)
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
                history_entry = self._build_history_entry(
                    post,
                    media_type=str(post.get("_media_type") or "video"),
                )
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
                is_reddit_saved = bool(history_entry.get("is_reddit_saved"))
                if row:
                    row.video_url_key = str(history_entry.get("video_url_key") or "")
                    row.subreddit = str(history_entry.get("subreddit") or "")
                    row.post_id = history_entry.get("post_id")
                    row.post_url = history_entry.get("post_url")
                    row.video_url = str(history_entry.get("video_url") or "")
                    row.title = history_entry.get("title")
                    row.media_type = str(history_entry.get("media_type") or "video")
                    if is_reddit_saved:
                        row.is_reddit_saved = True
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
                        media_type=str(history_entry.get("media_type") or "video"),
                        is_reddit_saved=is_reddit_saved,
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

    def _remove_item(self, subreddit: str) -> bool:
        normalized = self.normalize_subreddit_name(subreddit).lower()
        with self._lock:
            items: list[dict[str, Any]] = self._status["items"]
            for index, item in enumerate(items):
                if self.normalize_subreddit_name(str(item.get("name") or "")).lower() != normalized:
                    continue
                del items[index]
                return True
        return False

    def _prune_zero_image_count_items(self) -> int:
        """Drop finished users with 0 imgs/year from the modal list."""
        removed = 0
        with self._lock:
            if str(self._status.get("media_type") or "") != "image_count":
                return 0
            items: list[dict[str, Any]] = self._status["items"]
            kept: list[dict[str, Any]] = []
            for item in items:
                status = str(item.get("status") or "").lower()
                planned = int(item.get("planned") or 0)
                completed = int(item.get("completed") or 0)
                if status == "done" and max(planned, completed) <= 0:
                    removed += 1
                    continue
                kept.append(item)
            self._status["items"] = kept
            self._status["total_videos_planned"] = sum(int(item.get("planned") or 0) for item in kept)
        return removed

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
        video_on = self.is_auto_download_enabled()
        image_on = self.is_auto_image_download_enabled()
        targets: list[str] = []
        if video_on:
            targets.append(f"ultimos {AUTO_PER_SUBREDDIT_LIMIT} videos")
        if image_on:
            targets.append("todas as imagens do ultimo ano ainda nao baixadas")
        target_label = " / ".join(targets) if targets else "itens"
        message = (
            "Aguardando backend ficar 5 min ocioso para baixar automaticamente "
            f"{target_label} de cada subreddit {AUTO_SUBREDDIT_PREFIX}."
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

    async def _load_auto_image_subreddit_names(self) -> list[str]:
        missing = self._reddit.missing_config_fields()
        if missing:
            raise RedditConfigError("Reddit env vars ausentes: " + ", ".join(missing))

        blacklisted = await asyncio.to_thread(self._get_blacklisted_categories)
        downloaded = await asyncio.to_thread(self.get_downloaded_image_subreddits)
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

    def _load_category_names_with_images(self) -> set[str]:
        names: set[str] = set()
        with self._session_factory() as session:
            for original_path in session.scalars(
                select(RedditImage.original_path).where(
                    RedditImage.is_deleted.is_(False),
                    RedditImage.status == "ready",
                )
            ):
                category, _, _ = extract_path_badges(str(original_path or ""))
                normalized = normalize_category_name(str(category or "").strip())
                if normalized:
                    names.add(normalized)
        return names

    def _set_preparing_download_status(self, *, message: str, media_type: str, trigger: str) -> None:
        with self._lock:
            if self._status.get("state") == "running":
                raise RuntimeError("Ja existe um download Reddit em execucao.")
            self._status = {
                "task_id": None,
                "state": "loading",
                "message": message,
                "started_at": utcnow(),
                "finished_at": None,
                "total_subreddits": 0,
                "completed_subreddits": 0,
                "total_videos_planned": 0,
                "processed_videos": 0,
                "failed_videos": 0,
                "media_type": media_type,
                "items": [],
                "logs": [],
                "trigger": trigger,
            }
        self._append_log(message)

    async def _load_empty_user_image_subreddit_names(self, limit: int = 10) -> list[str]:
        missing = self._reddit.missing_config_fields()
        if missing:
            raise RedditConfigError("Reddit env vars ausentes: " + ", ".join(missing))

        blacklisted = await asyncio.to_thread(self._get_blacklisted_categories)
        with_images = await asyncio.to_thread(self._load_category_names_with_images)
        already_marked = await asyncio.to_thread(self.get_downloaded_image_subreddits)
        year_counts = await asyncio.to_thread(self.get_user_year_image_counts_map)

        # Preferir cache local (rapido). So cai no Reddit se o cache estiver vazio.
        cached_names = await asyncio.to_thread(self._load_cached_subscription_names)
        if cached_names:
            subscription_names = cached_names
        else:
            subscriptions = await self._reddit.list_subscribed_subreddits()
            subscription_names = [
                self.normalize_subreddit_name(str(item.get("name") or "")).strip()
                for item in subscriptions
            ]

        candidates: list[tuple[int, str]] = []
        seen: set[str] = set()
        for raw_name in subscription_names:
            normalized = self.normalize_subreddit_name(str(raw_name or "")).strip()
            if not normalized:
                continue
            key = normalized.lower()
            if not key.startswith(AUTO_SUBREDDIT_PREFIX):
                continue
            if key in seen:
                continue
            category_key = normalize_category_name(normalized)
            if category_key in blacklisted:
                continue
            if category_key in with_images:
                continue
            if key in already_marked:
                continue
            # Somente perfis com contagem imgs/ano ja verificada entram no lote.
            if key not in year_counts:
                continue
            seen.add(key)
            candidates.append((int(year_counts[key]), normalized))

        candidates.sort(key=lambda pair: (-pair[0], pair[1].lower()))
        capped = max(1, int(limit))
        selected = [name for _, name in candidates[:capped]]
        self._append_log(
            f"Selecao u_ vazios: {len(selected)} de {len(candidates)} candidatos "
            f"(catalogo com imagens={len(with_images)}, marcados={len(already_marked)})."
        )
        if selected:
            preview = ", ".join(selected[:10])
            self._append_log(f"Lote escolhido: {preview}")
        return selected

    async def start_download_for_empty_user_images(self, subreddit_limit: int = 10, per_subreddit_limit: int = 10) -> dict[str, Any]:
        capped_subreddit_limit = max(1, min(int(subreddit_limit or 10), 500))
        capped_per_subreddit_limit = max(1, min(int(per_subreddit_limit or 10), 50))
        self._set_preparing_download_status(
            message=(
                f"Selecionando {capped_subreddit_limit} perfis u_ com mais imgs/ano "
                "e sem imagens baixadas..."
            ),
            media_type="images",
            trigger="manual_empty_user_images",
        )
        try:
            names = await self._load_empty_user_image_subreddit_names(limit=capped_subreddit_limit)
            if not names:
                raise ValueError(
                    "Nenhum perfil u_ com imgs/ano verificada e sem imagens baixadas foi encontrado. "
                    "Rode 'Verificar qtd. imagens' antes."
                )
            return await self.start_download(
                subreddit_names=names,
                per_subreddit_limit=capped_per_subreddit_limit,
                period_months=None,
                trigger="manual_empty_user_images",
                media_type="images",
            )
        except Exception:
            # Se a preparacao falhar, nao deixar o status preso em "loading".
            with self._lock:
                if str(self._status.get("state") or "").lower() == "loading":
                    self._status["state"] = "failed"
                    self._status["finished_at"] = utcnow()
            raise

    async def _run_auto_download_monitor(self) -> None:
        try:
            while True:
                task = self._task
                if task and not task.done():
                    await asyncio.sleep(AUTO_POLL_INTERVAL_SECONDS)
                    continue

                now = utcnow()
                if self._image_count_scan_active:
                    self._last_busy_at = now
                    self._last_auto_attempt_at = None
                    self._set_auto_waiting_status(idle_seconds=0)
                    await asyncio.sleep(AUTO_POLL_INTERVAL_SECONDS)
                    continue

                video_auto_enabled = await asyncio.to_thread(self.is_auto_download_enabled)
                image_auto_enabled = await asyncio.to_thread(self.is_auto_image_download_enabled)
                if not video_auto_enabled and not image_auto_enabled:
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
                video_names: list[str] = []
                image_names: list[str] = []
                try:
                    if video_auto_enabled:
                        video_names = await self._load_auto_subreddit_names()
                    if image_auto_enabled:
                        image_names = await self._load_auto_image_subreddit_names()
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

                media_type = "videos"
                subreddit_names: list[str] = []
                if video_names:
                    media_type = "videos"
                    subreddit_names = video_names
                elif image_names:
                    media_type = "images"
                    subreddit_names = image_names
                else:
                    self._append_log(
                        "[AUTO] Backend ocioso por 5 min, mas nao encontrei subreddits u_ "
                        "pendentes sem download de videos/imagens."
                    )
                    self._set_auto_waiting_status(idle_seconds=idle_seconds)
                    await asyncio.sleep(AUTO_POLL_INTERVAL_SECONDS)
                    continue

                if media_type == "images":
                    self._append_log(
                        "[AUTO] Backend ocioso por 5 min. "
                        "Iniciando download de todas as imagens do ultimo ano "
                        f"(ainda nao baixadas) para {len(subreddit_names)} subreddit(s) {AUTO_SUBREDDIT_PREFIX}."
                    )
                else:
                    self._append_log(
                        "[AUTO] Backend ocioso por 5 min. "
                        f"Iniciando download dos ultimos {AUTO_PER_SUBREDDIT_LIMIT} videos para "
                        f"{len(subreddit_names)} subreddit(s) {AUTO_SUBREDDIT_PREFIX}."
                    )
                try:
                    await self.start_download(
                        subreddit_names=subreddit_names,
                        per_subreddit_limit=None if media_type == "images" else AUTO_PER_SUBREDDIT_LIMIT,
                        period_months=12 if media_type == "images" else None,
                        trigger="idle_auto_images" if media_type == "images" else "idle_auto",
                        media_type=media_type,
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

    def _load_cached_subscription_names(self) -> list[str]:
        with self._session_factory() as session:
            row = session.scalar(select(AppConfig).where(AppConfig.key == REDDIT_SUBSCRIPTIONS_CACHE_KEY))
            if not row or not row.value:
                return []
            try:
                payload = json.loads(row.value)
            except json.JSONDecodeError:
                return []
        rows = payload if isinstance(payload, list) else []
        names: list[str] = []
        seen: set[str] = set()
        for item in rows:
            if not isinstance(item, dict):
                continue
            normalized = self.normalize_subreddit_name(str(item.get("name") or "")).strip()
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            names.append(normalized)
        names.sort(key=str.lower)
        return names

    async def _load_unverified_user_subreddit_names(self) -> list[str]:
        missing = self._reddit.missing_config_fields()
        if missing:
            raise RedditConfigError("Reddit env vars ausentes: " + ", ".join(missing))

        blacklisted = await asyncio.to_thread(self._get_blacklisted_categories)
        verified = await asyncio.to_thread(self._load_user_year_image_counts)
        cached_names = await asyncio.to_thread(self._load_cached_subscription_names)
        if not cached_names:
            subscriptions = await self._reddit.list_subscribed_subreddits()
            cached_names = [
                self.normalize_subreddit_name(str(item.get("name") or "")).strip()
                for item in subscriptions
                if str(item.get("name") or "").strip()
            ]

        names: list[str] = []
        seen: set[str] = set()
        for raw_name in cached_names:
            normalized = self.normalize_subreddit_name(raw_name).strip()
            if not normalized:
                continue
            key = normalized.lower()
            if not key.startswith(AUTO_SUBREDDIT_PREFIX):
                continue
            if key in seen or key in verified:
                continue
            if normalize_category_name(normalized) in blacklisted:
                continue
            seen.add(key)
            names.append(normalized)
        names.sort(key=str.lower)
        return names

    async def start_image_count_scan(self) -> dict[str, Any]:
        with self._lock:
            current_state = str(self._status.get("state") or "").lower()
            if current_state in {"running", "loading"}:
                raise RuntimeError("Ja existe um download/contagem Reddit em execucao.")
            self._status = {
                "task_id": None,
                "state": "loading",
                "message": "Preparando lista de usuarios u_ pendentes de contagem...",
                "started_at": utcnow(),
                "finished_at": None,
                "total_subreddits": 0,
                "completed_subreddits": 0,
                "total_videos_planned": 0,
                "processed_videos": 0,
                "failed_videos": 0,
                "media_type": "image_count",
                "items": [],
                "logs": [],
                "trigger": "manual_image_count",
            }
        self._set_image_count_scan(
            active=True,
            current=None,
            message="Preparando lista de usuarios u_ pendentes...",
        )
        self._append_log("[IMG-COUNT] Preparando lista de usuarios pendentes...")
        self._last_busy_at = utcnow()

        try:
            names = await self._load_unverified_user_subreddit_names()
            if not names:
                with self._lock:
                    self._status["state"] = "failed"
                    self._status["message"] = (
                        "Nenhum usuario u_ pendente de verificacao de imagens (periodo de 1 ano)."
                    )
                    self._status["finished_at"] = utcnow()
                self._set_image_count_scan(
                    active=False,
                    current=None,
                    message=str(self._status.get("message") or ""),
                )
                raise ValueError(str(self._status.get("message") or "Nenhum usuario pendente."))

            task_id = hashlib.sha1(
                f"img-count|{utcnow().isoformat()}|{','.join(names)}".encode("utf-8")
            ).hexdigest()[:16]
            with self._lock:
                self._status = {
                    "task_id": task_id,
                    "state": "running",
                    "message": f"Contagem de imagens iniciada para {len(names)} usuario(s).",
                    "started_at": self._status.get("started_at") or utcnow(),
                    "finished_at": None,
                    "total_subreddits": len(names),
                    "completed_subreddits": 0,
                    "total_videos_planned": 0,
                    "processed_videos": 0,
                    "failed_videos": 0,
                    "media_type": "image_count",
                    "items": [
                        {
                            "name": name,
                            "status": "pending",
                            "planned": 0,
                            "completed": 0,
                            "failed": 0,
                            "marked_downloaded": False,
                        }
                        for name in names
                    ],
                    "logs": list(self._status.get("logs") or []),
                    "trigger": "manual_image_count",
                }
            self._set_image_count_scan(
                active=True,
                current=None,
                message=f"Contando imagens/ano em {len(names)} usuario(s)...",
            )
            self._last_busy_at = utcnow()
            self._append_log(
                f"[IMG-COUNT] Iniciando contagem de imagens (1 ano) para {len(names)} usuario(s) u_ pendente(s)."
            )
            self._task = asyncio.create_task(
                self._run_image_count_scan(names),
                name=f"reddit-image-count-{task_id}",
            )
            self._task.add_done_callback(self._handle_download_task_done)
            return self.get_status()
        except ValueError:
            raise
        except Exception:
            with self._lock:
                if str(self._status.get("state") or "").lower() == "loading":
                    self._status["state"] = "failed"
                    self._status["finished_at"] = utcnow()
            self._set_image_count_scan(active=False, current=None, message="Falha ao preparar contagem.")
            raise

    async def _run_image_count_scan(self, names: list[str]) -> None:
        try:
            self._append_log(
                f"[IMG-COUNT] Contagem paralela com ate {IMAGE_COUNT_CONCURRENCY} usuarios simultaneos."
            )
            cutoff_datetime = utcnow() - timedelta(days=IMAGE_COUNT_PERIOD_DAYS)
            semaphore = asyncio.Semaphore(IMAGE_COUNT_CONCURRENCY)
            timeout = httpx.Timeout(
                connect=20.0,
                read=float(self._settings.reddit_timeout_seconds),
                write=30.0,
                pool=30.0,
            )

            async def count_one(name: str, shared_client: httpx.AsyncClient) -> None:
                async with semaphore:
                    self._update_item(name, status="scanning")
                    self._set_message(f"Contando imagens de 1 ano em {name}...")

                    def report_progress(progress: dict[str, Any], *, user: str = name) -> None:
                        found = int(progress.get("found") or 0)
                        page = int(progress.get("page") or 0)
                        self._last_busy_at = utcnow()
                        self._update_item(user, planned=found)
                        self._sync_total_videos_planned()
                        # Throttle summary/toast updates under concurrency.
                        if page > 1 and page % 3 != 0 and not progress.get("reached_cutoff"):
                            return
                        detail = f"{user}: pagina {page}, {found} imagem(ns)/ano."
                        self._set_image_count_scan(active=True, current=user, message=detail)
                        self._set_message(detail)

                    try:
                        if await asyncio.to_thread(self._is_user_year_image_count_verified, name):
                            counts = await asyncio.to_thread(self.get_user_year_image_counts_map)
                            count = int(counts.get(name.lower(), 0))
                        else:
                            count = await self._reddit.count_image_posts_in_period(
                                subreddit=name,
                                cutoff_datetime=cutoff_datetime,
                                max_pages=IMAGE_COUNT_MAX_PAGES,
                                progress_callback=report_progress,
                                client=shared_client,
                            )
                            await asyncio.to_thread(self._save_user_year_image_count, name, count)

                        self._increment("completed_subreddits", 1)
                        if count <= 0:
                            self._remove_item(name)
                            self._sync_total_videos_planned()
                            completed = int(self._clone_status().get("completed_subreddits") or 0)
                            self._set_message(
                                f"{name}: 0 imagens/ano (removido do modal). "
                                f"Progresso {completed}/{len(names)}."
                            )
                        else:
                            self._update_item(
                                name,
                                status="done",
                                planned=count,
                                completed=count,
                                failed=0,
                            )
                            self._increment("processed_videos", count)
                            self._sync_total_videos_planned()
                            completed = int(self._clone_status().get("completed_subreddits") or 0)
                            self._set_message(
                                f"{name}: {count} imagem(ns)/ano. Progresso {completed}/{len(names)}."
                            )
                        if completed % 10 == 0 or completed == len(names):
                            self._append_log(
                                f"[IMG-COUNT] Progresso {completed}/{len(names)} "
                                f"(ultimo: {name} = {count})."
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        self._update_item(name, status="failed", failed=1)
                        self._increment("failed_videos", 1)
                        self._increment("completed_subreddits", 1)
                        self._append_log(f"[IMG-COUNT] Falha em {name}: {exc}")
                        self._set_message(f"Falha ao contar imagens em {name}.")

            async with httpx.AsyncClient(
                timeout=timeout,
                limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
            ) as shared_client:
                tasks = [
                    asyncio.create_task(count_one(name, shared_client), name=f"img-count-{name}")
                    for name in names
                ]
                try:
                    await asyncio.gather(*tasks)
                except asyncio.CancelledError:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    with suppress(asyncio.CancelledError):
                        await asyncio.gather(*tasks, return_exceptions=True)
                    raise

            removed_zeros = self._prune_zero_image_count_items()
            if removed_zeros > 0:
                self._append_log(
                    f"[IMG-COUNT] Removidos {removed_zeros} usuario(s) com 0 imagens/ano do modal."
                )

            with self._lock:
                failed = int(self._status.get("failed_videos") or 0)
                processed = int(self._status.get("processed_videos") or 0)
                kept_users = len(self._status.get("items") or [])
                self._status["state"] = "completed_with_errors" if failed > 0 else "completed"
                self._status["message"] = (
                    f"Contagem concluida: {processed} imagem(ns) em {kept_users} usuario(s) com imagens; "
                    f"{failed} falha(s); zeros ocultos no modal."
                )
                self._status["finished_at"] = utcnow()
            self._set_image_count_scan(
                active=False,
                current=None,
                message=str(self._clone_status().get("message") or ""),
            )
            self._append_log(f"[IMG-COUNT] {self._clone_status().get('message')}")
        except asyncio.CancelledError:
            self._set_image_count_scan(active=False, current=None, message="Contagem interrompida.")
            with self._lock:
                if self._status.get("state") == "running":
                    self._status["state"] = "stopped"
                    self._status["message"] = "Contagem de imagens interrompida."
                    self._status["finished_at"] = utcnow()
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Falha na contagem manual de imagens Reddit")
            self._set_image_count_scan(active=False, current=None, message=f"Falha: {exc}")
            with self._lock:
                self._status["state"] = "failed"
                self._status["message"] = f"Falha na contagem de imagens: {exc}"
                self._status["finished_at"] = utcnow()
            self._append_log(f"[IMG-COUNT] Falha geral: {exc}")

    async def _next_unverified_user_subreddit(self) -> str | None:
        missing = self._reddit.missing_config_fields()
        if missing:
            raise RedditConfigError("Reddit env vars ausentes: " + ", ".join(missing))

        blacklisted = await asyncio.to_thread(self._get_blacklisted_categories)
        verified = await asyncio.to_thread(self._load_user_year_image_counts)
        cached_names = await asyncio.to_thread(self._load_cached_subscription_names)
        if not cached_names:
            subscriptions = await self._reddit.list_subscribed_subreddits()
            cached_names = [
                self.normalize_subreddit_name(str(item.get("name") or "")).strip()
                for item in subscriptions
                if str(item.get("name") or "").strip()
            ]

        for raw_name in cached_names:
            normalized = self.normalize_subreddit_name(raw_name).strip()
            if not normalized:
                continue
            key = normalized.lower()
            if not key.startswith(AUTO_SUBREDDIT_PREFIX):
                continue
            if key in verified:
                continue
            if normalize_category_name(normalized) in blacklisted:
                continue
            return normalized
        return None

    async def _verify_user_year_image_count(self, subreddit: str) -> int:
        normalized = self.normalize_subreddit_name(subreddit).strip()
        key = normalized.lower()
        if not key.startswith(AUTO_SUBREDDIT_PREFIX):
            raise ValueError("Somente usuarios u_ sao verificados.")
        if await asyncio.to_thread(self._is_user_year_image_count_verified, normalized):
            counts = await asyncio.to_thread(self.get_user_year_image_counts_map)
            return int(counts.get(key, 0))

        cutoff_datetime = utcnow() - timedelta(days=IMAGE_COUNT_PERIOD_DAYS)
        self._last_busy_at = utcnow()
        self._set_image_count_scan(
            active=True,
            current=normalized,
            message=f"Contando imagens de 1 ano em {normalized}...",
        )
        self._append_log(f"[IMG-COUNT] Verificando imagens (1 ano) de {normalized}...")

        def report_progress(progress: dict[str, Any]) -> None:
            found = int(progress.get("found") or 0)
            page = int(progress.get("page") or 0)
            self._last_busy_at = utcnow()
            detail = f"{normalized}: pagina {page}, {found} imagem(ns) no periodo de 1 ano."
            self._set_image_count_scan(
                active=True,
                current=normalized,
                message=detail,
            )
            self._set_message(detail)
            self._update_item(normalized, planned=found)
            self._sync_total_videos_planned()

        try:
            posts_count = await self._reddit.count_image_posts_in_period(
                subreddit=normalized,
                cutoff_datetime=cutoff_datetime,
                max_pages=IMAGE_COUNT_MAX_PAGES,
                progress_callback=report_progress,
            )
            count = int(posts_count)
            await asyncio.to_thread(self._save_user_year_image_count, normalized, count)
            self._append_log(f"[IMG-COUNT] {normalized}: {count} imagem(ns) no periodo de 1 ano.")
            self._set_image_count_scan(
                active=False,
                current=None,
                message=f"Ultima verificacao: {normalized} = {count} imagens/ano.",
            )
            self._last_busy_at = utcnow()
            return count
        except Exception:
            self._set_image_count_scan(
                active=False,
                current=None,
                message=f"Falha ao verificar {normalized}.",
            )
            self._last_busy_at = utcnow()
            raise

    async def _run_idle_image_count_monitor(self) -> None:
        try:
            while True:
                now = utcnow()
                enabled = await asyncio.to_thread(self.is_idle_image_count_enabled)
                if not enabled:
                    if self._image_count_scan_active:
                        self._set_image_count_scan(active=False, current=None, message=None)
                    await asyncio.sleep(IMAGE_COUNT_POLL_INTERVAL_SECONDS)
                    continue

                download_task = self._task
                if download_task and not download_task.done():
                    self._last_busy_at = now
                    await asyncio.sleep(IMAGE_COUNT_POLL_INTERVAL_SECONDS)
                    continue

                has_jobs = await asyncio.to_thread(self._has_pending_or_running_jobs)
                if has_jobs:
                    self._last_busy_at = now
                    await asyncio.sleep(IMAGE_COUNT_POLL_INTERVAL_SECONDS)
                    continue

                idle_seconds = max(0, int((now - self._last_busy_at).total_seconds()))
                if idle_seconds < IMAGE_COUNT_IDLE_THRESHOLD_SECONDS:
                    await asyncio.sleep(IMAGE_COUNT_POLL_INTERVAL_SECONDS)
                    continue

                verified_in_burst = 0
                while True:
                    enabled = await asyncio.to_thread(self.is_idle_image_count_enabled)
                    if not enabled:
                        break

                    download_task = self._task
                    if download_task and not download_task.done():
                        self._last_busy_at = utcnow()
                        break

                    has_jobs = await asyncio.to_thread(self._has_pending_or_running_jobs)
                    if has_jobs:
                        self._last_busy_at = utcnow()
                        break

                    try:
                        next_name = await self._next_unverified_user_subreddit()
                    except RedditConfigError as exc:
                        self._append_log(f"[IMG-COUNT] Ignorado: {exc}")
                        break
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Falha ao listar usuarios u_ para contagem de imagens")
                        self._append_log(f"[IMG-COUNT] Falha ao listar usuarios: {exc}")
                        break

                    if not next_name:
                        if verified_in_burst == 0:
                            self._set_image_count_scan(
                                active=False,
                                current=None,
                                message="Nenhum usuario u_ pendente de verificacao.",
                            )
                        break

                    try:
                        await self._verify_user_year_image_count(next_name)
                        verified_in_burst += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Falha ao contar imagens de usuario Reddit %s", next_name)
                        self._append_log(f"[IMG-COUNT] Falha em {next_name}: {exc}")
                        # Keep burst going for next users; failed one stays unverified.
                        await asyncio.sleep(2)
                        continue

                    # Small pause between users to keep Reddit API polite.
                    await asyncio.sleep(2)

                self._last_busy_at = utcnow()
                await asyncio.sleep(IMAGE_COUNT_POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            self._set_image_count_scan(active=False, current=None, message=None)
            raise

    def _mark_existing_catalog_as_reddit_saved(
        self,
        posts: list[dict[str, Any]],
        media_type: str,
    ) -> int:
        """Mark already-downloaded catalog rows as reddit-saved without re-downloading."""
        post_ids = sorted(
            {
                str(post.get("post_id") or "").strip()
                for post in posts
                if str(post.get("post_id") or "").strip()
            }
        )
        if not post_ids:
            return 0

        marked = 0
        normalized_media_type = self._normalize_history_media_type(media_type)
        with self._session_factory() as session:
            if normalized_media_type == "image":
                for chunk in self._iter_chunks(post_ids):
                    rows = list(
                        session.scalars(
                            select(RedditImage).where(
                                RedditImage.post_id.in_(chunk),
                                RedditImage.is_deleted.is_(False),
                            )
                        )
                    )
                    for row in rows:
                        if not row.is_reddit_saved:
                            row.is_reddit_saved = True
                            marked += 1
            else:
                for post_id in post_ids:
                    # Filenames are "{post_id}_{hash}.ext" under .../videos/...
                    like_unix = f"%/{post_id}_%"
                    like_windows = f"%\\\\{post_id}_%"
                    rows = list(
                        session.scalars(
                            select(Video).where(
                                Video.is_deleted.is_(False),
                                or_(
                                    Video.original_path.like(like_unix),
                                    Video.original_path.like(like_windows),
                                ),
                            )
                        )
                    )
                    for row in rows:
                        if not row.is_reddit_saved:
                            row.is_reddit_saved = True
                            marked += 1

            # Also flip history rows for these post IDs.
            for chunk in self._iter_chunks(post_ids):
                history_rows = list(
                    session.scalars(
                        select(RedditMediaHistory).where(RedditMediaHistory.post_id.in_(chunk))
                    )
                )
                for row in history_rows:
                    if not row.is_reddit_saved:
                        row.is_reddit_saved = True
            session.commit()
        return marked

    def _partition_saved_posts_by_history(
        self,
        posts: list[dict[str, Any]],
        media_type: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return (new_posts, already_downloaded_posts)."""
        if not posts:
            return [], []
        new_posts, skipped = self._filter_history_hits(posts, media_type=media_type)
        if skipped <= 0:
            return new_posts, []
        new_keys = {
            str((post.get("_history_entry") or {}).get("item_key") or "")
            for post in new_posts
            if isinstance(post.get("_history_entry"), dict)
        }
        already: list[dict[str, Any]] = []
        for post in posts:
            history_entry = post.get("_history_entry")
            if not isinstance(history_entry, dict):
                history_entry = self._build_history_entry(post, media_type=media_type)
                if history_entry:
                    post["_history_entry"] = history_entry
            if not isinstance(history_entry, dict):
                continue
            item_key = str(history_entry.get("item_key") or "")
            if item_key and item_key not in new_keys:
                already.append(post)
        return new_posts, already

    async def start_saved_download(self, media_type: str = "both") -> dict[str, Any]:
        missing = self._reddit.missing_config_fields()
        if missing:
            raise RedditConfigError("Reddit env vars ausentes: " + ", ".join(missing))

        normalized_media_type = str(media_type or "both").strip().lower()
        if normalized_media_type not in {"videos", "images", "both"}:
            raise ValueError("media_type invalido. Use videos, images ou both.")
        if normalized_media_type in {"videos", "both"}:
            self._ingest.ensure_media_tools_available()

        self._ensure_minimum_free_space()
        task_id = hashlib.sha1(f"{utcnow().isoformat()}|saved|{normalized_media_type}".encode("utf-8")).hexdigest()[:16]

        with self._lock:
            if self._status.get("state") in {"running", "loading"}:
                raise RuntimeError("Ja existe um download Reddit em execucao.")
            self._status = {
                "task_id": task_id,
                "state": "running",
                "message": "Sincronizacao de Meus Salvos iniciada.",
                "started_at": utcnow(),
                "finished_at": None,
                "total_subreddits": 1,
                "completed_subreddits": 0,
                "total_videos_planned": 0,
                "processed_videos": 0,
                "failed_videos": 0,
                "media_type": normalized_media_type,
                "items": [
                    {
                        "name": "Meus Salvos",
                        "status": "pending",
                        "planned": 0,
                        "completed": 0,
                        "failed": 0,
                        "marked_downloaded": False,
                    }
                ],
                "logs": [],
                "trigger": "manual_saved",
                "saved_sync": {
                    "listing_items": 0,
                    "posts": 0,
                    "videos_found": 0,
                    "images_found": 0,
                    "already_marked": 0,
                    "api_limit_likely_reached": False,
                },
            }
        self._last_busy_at = utcnow()
        self._last_auto_attempt_at = None
        self._append_log(
            "Iniciando sincronizacao de Meus Salvos "
            f"(media_type={normalized_media_type}; limite API ~1000 itens)."
        )
        self._task = asyncio.create_task(
            self._run_saved_download(task_id=task_id, media_type=normalized_media_type),
            name=f"reddit-saved-download-{task_id}",
        )
        self._task.add_done_callback(self._handle_download_task_done)
        return self.get_status()

    async def _run_saved_download(self, task_id: str, media_type: str) -> None:
        timeout = httpx.Timeout(connect=20.0, read=300.0, write=300.0, pool=30.0)
        bucket_name = "Meus Salvos"
        already_marked_total = 0
        listing_meta: dict[str, Any] = {
            "listing_items": 0,
            "posts": 0,
            "videos_found": 0,
            "images_found": 0,
            "api_limit_likely_reached": False,
        }
        try:
            self._update_item(bucket_name, status="loading")
            self._set_message("Buscando posts salvos da conta Reddit...")

            def report_progress(progress: dict[str, Any]) -> None:
                page = int(progress.get("page") or 0)
                found = int(progress.get("found") or 0)
                listing_items = int(progress.get("listing_items") or 0)
                posts = int(progress.get("posts") or 0)
                videos_found = int(progress.get("videos_found") or 0)
                images_found = int(progress.get("images_found") or 0)
                self._update_item(bucket_name, status="scanning", planned=found)
                self._sync_total_videos_planned()
                self._set_message(
                    f"Buscando Meus Salvos: pagina {page}, {listing_items} item(ns) listados, "
                    f"{found} midia(s) encontrada(s)."
                )
                self._append_log(
                    f"[Meus Salvos] Pagina {page}: {listing_items} listados, {posts} posts, "
                    f"{videos_found} video(s), {images_found} imagem(ns)."
                )
                with self._lock:
                    self._status["saved_sync"] = {
                        "listing_items": listing_items,
                        "posts": posts,
                        "videos_found": videos_found,
                        "images_found": images_found,
                        "already_marked": already_marked_total,
                        "api_limit_likely_reached": listing_items >= 900,
                    }

            listing = await self._reddit.list_saved_media_posts(
                media_type=media_type,
                progress_callback=report_progress,
            )
            video_posts = list(listing.get("videos") or [])
            image_posts = list(listing.get("images") or [])
            listing_meta = {
                "listing_items": int(listing.get("total_listing_items") or 0),
                "posts": int(listing.get("total_posts") or 0),
                "videos_found": len(video_posts),
                "images_found": len(image_posts),
                "api_limit_likely_reached": bool(listing.get("api_limit_likely_reached")),
            }
            with self._lock:
                self._status["saved_sync"] = {
                    **listing_meta,
                    "already_marked": already_marked_total,
                }

            media_passes: list[tuple[str, list[dict[str, Any]]]] = []
            if media_type in {"videos", "both"}:
                media_passes.append(("videos", video_posts))
            if media_type in {"images", "both"}:
                media_passes.append(("images", image_posts))

            planned_total = sum(len(posts) for _, posts in media_passes)
            self._update_item(bucket_name, status="downloading", planned=planned_total)
            self._sync_total_videos_planned()
            self._append_log(
                f"[Meus Salvos] API retornou {listing_meta['listing_items']} item(ns), "
                f"{listing_meta['posts']} post(s), "
                f"{listing_meta['videos_found']} video(s), {listing_meta['images_found']} imagem(ns)."
                + (
                    " Possivel teto ~1000 da API atingido."
                    if listing_meta["api_limit_likely_reached"]
                    else ""
                )
            )

            if planned_total == 0:
                self._update_item(bucket_name, status="completed")
                self._increment("completed_subreddits", 1)
                with self._lock:
                    self._status["state"] = "completed"
                    self._status["message"] = (
                        "Nenhuma midia suportada encontrada nos posts salvos listados pela API."
                    )
                    self._status["finished_at"] = utcnow()
                return

            completed = 0
            failed = 0
            async with httpx.AsyncClient(timeout=timeout) as client:
                for current_media_type, posts in media_passes:
                    item_label = "video" if current_media_type == "videos" else "imagem"
                    new_posts, already_posts = self._partition_saved_posts_by_history(
                        posts,
                        media_type=current_media_type,
                    )
                    if already_posts:
                        marked = await asyncio.to_thread(
                            self._mark_existing_catalog_as_reddit_saved,
                            already_posts,
                            current_media_type,
                        )
                        already_marked_total += marked
                        completed += len(already_posts)
                        self._increment("processed_videos", len(already_posts))
                        self._update_item(bucket_name, completed=completed, failed=failed)
                        self._append_log(
                            f"[Meus Salvos] {len(already_posts)} {item_label}(s) ja baixado(s); "
                            f"{marked} marcado(s) como Meus Salvos."
                        )
                        with self._lock:
                            self._status["saved_sync"] = {
                                **listing_meta,
                                "already_marked": already_marked_total,
                            }

                    for idx, post in enumerate(new_posts, start=1):
                        self._ensure_minimum_free_space()
                        subreddit = self.normalize_subreddit_name(str(post.get("subreddit") or "")).strip()
                        if not subreddit:
                            subreddit = "saved"
                        if normalize_category_name(subreddit) in self._get_blacklisted_categories():
                            failed += 1
                            self._increment("failed_videos", 1)
                            self._update_item(bucket_name, completed=completed, failed=failed)
                            self._append_log(
                                f"[Meus Salvos] Ignorado {item_label} {post.get('post_id')}: "
                                f"subreddit {subreddit} na blacklist."
                            )
                            continue

                        self._append_log(
                            f"[Meus Salvos] Baixando {item_label} {idx}/{len(new_posts)} "
                            f"(r/{subreddit}, post {post.get('post_id') or '?'})..."
                        )
                        self._set_message(
                            f"Baixando {item_label} {idx}/{len(new_posts)} de Meus Salvos..."
                        )
                        post["is_reddit_saved"] = True
                        post["_media_type"] = "image" if current_media_type == "images" else "video"
                        try:
                            if current_media_type == "videos":
                                source_path = await self._download_video_file(
                                    client=client,
                                    subreddit=subreddit,
                                    post=post,
                                )
                                await asyncio.to_thread(
                                    lambda path=str(source_path): self._ingest.process_source_now(
                                        path,
                                        None,
                                        True,
                                        True,
                                        is_reddit_saved=True,
                                    )
                                )
                            else:
                                source_path = await self._download_image_file(
                                    client=client,
                                    subreddit=subreddit,
                                    post=post,
                                )
                                await asyncio.to_thread(
                                    lambda path=str(source_path), p=post: self._image_ingest.process_source_now(
                                        path,
                                        post_id=str(p.get("post_id") or "").strip() or None,
                                        post_url=str(p.get("post_url") or "").strip() or None,
                                        title=str(p.get("title") or "").strip() or None,
                                        is_reddit_saved=True,
                                    )
                                )
                            completed += 1
                            self._increment("processed_videos", 1)
                            self._update_item(bucket_name, completed=completed, failed=failed)
                            try:
                                self._record_downloaded_posts([post])
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "Failed to persist reddit media history for saved post_id=%s error=%s",
                                    post.get("post_id"),
                                    exc,
                                )
                                self._append_log(
                                    f"[Meus Salvos] Aviso: falha ao salvar historico do post "
                                    f"{post.get('post_id') or '?'}: {exc}"
                                )
                        except Exception as exc:  # noqa: BLE001
                            failed += 1
                            self._increment("failed_videos", 1)
                            self._update_item(bucket_name, completed=completed, failed=failed)
                            self._append_log(
                                f"[Meus Salvos] Erro no {item_label} {idx}/{len(new_posts)}: {exc}"
                            )

            self._update_item(
                bucket_name,
                status="completed_with_errors" if failed > 0 else "completed",
                marked_downloaded=True,
            )
            self._increment("completed_subreddits", 1)
            limit_note = (
                " API pode ter atingido o teto de ~1000 itens."
                if listing_meta["api_limit_likely_reached"]
                else ""
            )
            summary = (
                f"Meus Salvos finalizado: {completed} sucesso(s), {failed} falha(s), "
                f"{already_marked_total} ja existentes marcados. "
                f"Listados pela API: {listing_meta['listing_items']} item(ns)."
                f"{limit_note}"
            )
            with self._lock:
                self._status["saved_sync"] = {
                    **listing_meta,
                    "already_marked": already_marked_total,
                }
                self._status["state"] = "completed_with_errors" if failed > 0 else "completed"
                self._status["message"] = summary
                self._status["finished_at"] = utcnow()
            self._append_log(summary)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Falha no pipeline de Meus Salvos")
            with self._lock:
                self._status["state"] = "failed"
                self._status["message"] = f"Falha geral em Meus Salvos: {exc}"
                self._status["finished_at"] = utcnow()
            self._append_log(f"Falha geral em Meus Salvos: {exc}")

    async def start_download(
        self,
        subreddit_names: list[str],
        per_subreddit_limit: int | None = 10,
        period_months: int | None = None,
        trigger: str = "manual",
        media_type: str = "videos",
    ) -> dict[str, Any]:
        missing = self._reddit.missing_config_fields()
        if missing:
            raise RedditConfigError("Reddit env vars ausentes: " + ", ".join(missing))

        normalized_media_type = str(media_type or "videos").strip().lower()
        if normalized_media_type not in {"videos", "images", "both"}:
            raise ValueError("media_type invalido. Use videos, images ou both.")
        if normalized_media_type in {"videos", "both"}:
            self._ingest.ensure_media_tools_available()

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
        if normalized_period_months is None and per_subreddit_limit is not None:
            limit = max(1, min(int(per_subreddit_limit or 10), 50))
        task_id = hashlib.sha1(f"{utcnow().isoformat()}|{normalized_names}".encode("utf-8")).hexdigest()[:16]

        with self._lock:
            if self._status.get("state") == "running":
                raise RuntimeError("Ja existe um download Reddit em execucao.")
            auto_trigger = trigger in {"idle_auto", "idle_auto_images"}
            self._status = {
                "task_id": task_id,
                "state": "running",
                "message": "Download automatico iniciado." if auto_trigger else "Download iniciado.",
                "started_at": utcnow(),
                "finished_at": None,
                "total_subreddits": len(normalized_names),
                "completed_subreddits": 0,
                "total_videos_planned": 0,
                "processed_videos": 0,
                "failed_videos": 0,
                "media_type": normalized_media_type,
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

        label = (
            "videos"
            if normalized_media_type == "videos"
            else "imagens"
            if normalized_media_type == "images"
            else "itens"
        )
        if normalized_period_months is None:
            if limit is None:
                self._append_log(
                    f"Iniciando download de todas as {label} para {len(normalized_names)} subreddit(s)."
                )
            else:
                self._append_log(
                    f"Iniciando download dos ultimos {limit} {label} para {len(normalized_names)} subreddit(s)."
                )
        else:
            self._append_log(
                f"Iniciando download de {label} do periodo de {normalized_period_months} mes(es) "
                f"para {len(normalized_names)} subreddit(s)."
            )
        self._task = asyncio.create_task(
            self._run_download(
                task_id=task_id,
                subreddit_names=normalized_names,
                limit=limit,
                period_months=normalized_period_months,
                media_type=normalized_media_type,
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

    async def _download_image_file(
        self,
        client: httpx.AsyncClient,
        subreddit: str,
        post: dict[str, Any],
    ) -> Path:
        created_at_utc = self._parse_created_at(str(post.get("created_at_utc") or ""))
        created_local_date = created_at_utc.astimezone().date().isoformat()
        subreddit_dir = self._safe_subreddit_path_part(subreddit)
        target_dir = self._settings.reddit_download_dir / subreddit_dir / "images" / created_local_date
        target_dir.mkdir(parents=True, exist_ok=True)

        image_url = str(post.get("image_url") or "").strip()
        if not image_url:
            raise ValueError("URL de imagem ausente.")

        parsed_url = urlparse(image_url)
        ext = Path(parsed_url.path).suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
            ext = ".jpg"
        file_name = self._build_file_name(str(post.get("post_id") or ""), image_url, ext)
        target_path = target_dir / file_name
        temp_path = target_dir / f"{file_name}.part"

        if target_path.exists():
            return target_path

        headers = {"User-Agent": self._settings.reddit_user_agent}
        bytes_since_space_check = 0
        try:
            async with client.stream("GET", image_url, headers=headers, follow_redirects=True) as response:
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
        media_type: str = "videos",
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
        elif limit is None:
            # Full catalog scan for idle auto-download of all images.
            max_pages = 200
        media_types = (
            ["videos", "images"]
            if media_type == "both"
            else ["images"] if media_type == "images" else ["videos"]
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                for subreddit in subreddit_names:
                    if normalize_category_name(subreddit) in self._get_blacklisted_categories():
                        self._update_item(subreddit, status="skipped_blacklisted")
                        self._increment("completed_subreddits", 1)
                        self._append_log(f"[{subreddit}] Ignorado: categoria em blacklist.")
                        continue

                    subreddit_failed = False
                    subreddit_had_success = False

                    for pass_index, current_media_type in enumerate(media_types):
                        is_last_pass = pass_index == len(media_types) - 1
                        media_label = "videos" if current_media_type == "videos" else "imagens"
                        item_label = "video" if current_media_type == "videos" else "imagem"

                        self._update_item(subreddit, status="loading")
                        self._set_message(f"Preparando busca de {media_label} em r/{subreddit}...")
                        if period_months is not None:
                            self._append_log(
                                f"[{subreddit}] Buscando {media_label} dos ultimos {period_months} mes(es)..."
                            )
                        elif limit is None:
                            self._append_log(f"[{subreddit}] Buscando todas as {media_label}...")
                        else:
                            self._append_log(f"[{subreddit}] Buscando {media_label} recentes...")

                        def report_progress(progress: dict[str, Any], *, label: str = media_label) -> None:
                            found = int(progress.get("found") or 0)
                            page = int(progress.get("page") or 0)
                            oldest_raw = str(progress.get("oldest_created_at") or "").strip()
                            oldest_label = oldest_raw.split("T", maxsplit=1)[0] if oldest_raw else ""
                            reached_cutoff = bool(progress.get("reached_cutoff"))
                            detail = (
                                f"[{subreddit}] Busca de {label} em andamento: pagina {page}, {found} item(ns) encontrado(s)"
                                + (f", mais antigo {oldest_label}" if oldest_label else "")
                                + (" (limite do periodo atingido)" if reached_cutoff else "")
                            )
                            self._update_item(subreddit, status="scanning", planned=found)
                            self._sync_total_videos_planned()
                            scope = "no periodo" if period_months is not None else "encontrado(s)"
                            self._set_message(
                                f"Buscando {label} em r/{subreddit}: pagina {page}, {found} item(ns) {scope}."
                            )
                            self._append_log(detail)

                        show_scan_progress = period_months is not None or limit is None
                        try:
                            if current_media_type == "videos":
                                posts = await self._reddit.list_latest_video_posts(
                                    subreddit=subreddit,
                                    limit=limit,
                                    cutoff_datetime=cutoff_datetime,
                                    max_pages=max_pages,
                                    progress_callback=report_progress if show_scan_progress else None,
                                )
                            else:
                                posts = await self._reddit.list_latest_image_posts(
                                    subreddit=subreddit,
                                    limit=limit,
                                    cutoff_datetime=cutoff_datetime,
                                    max_pages=max_pages,
                                    progress_callback=report_progress if show_scan_progress else None,
                                )
                        except Exception as exc:  # noqa: BLE001
                            self._update_item(subreddit, status="failed")
                            self._increment("failed_videos", 1)
                            self._set_message(f"Falha ao buscar {media_label} em r/{subreddit}.")
                            self._append_log(f"[{subreddit}] Falha ao buscar {media_label}: {exc}")
                            subreddit_failed = True
                            if is_last_pass:
                                self._increment("completed_subreddits", 1)
                            continue

                        posts, skipped_due_to_history = self._filter_history_hits(
                            posts,
                            media_type=current_media_type,
                        )
                        planned = len(posts)
                        self._update_item(subreddit, status="downloading", planned=planned)
                        self._sync_total_videos_planned()
                        if skipped_due_to_history > 0:
                            downloaded_to_mark.add(self.normalize_subreddit_name(subreddit).lower())
                            self._update_item(subreddit, marked_downloaded=True)
                            self._append_log(
                                f"[{subreddit}] Ignorados {skipped_due_to_history} {item_label}(s) ja baixados anteriormente."
                            )
                        self._set_message(f"Baixando {planned} {item_label}(s) de r/{subreddit}...")
                        if planned == 0:
                            if skipped_due_to_history > 0:
                                self._append_log(
                                    f"[{subreddit}] Nenhum {item_label} novo; tudo ja estava no historico."
                                )
                            else:
                                # Scan ok sem itens: ainda marca como tentado para o
                                # botao de u_ vazios / auto-imagens nao repetir o mesmo perfil.
                                downloaded_to_mark.add(self.normalize_subreddit_name(subreddit).lower())
                                self._update_item(subreddit, marked_downloaded=True)
                                self._append_log(
                                    f"[{subreddit}] Nenhum {item_label} encontrado; marcado como tentado."
                                )
                            if is_last_pass:
                                self._update_item(subreddit, status="completed")
                                self._increment("completed_subreddits", 1)
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
                            self._append_log(f"[{subreddit}] Baixando {item_label} {idx}/{planned}...")
                            self._set_message(f"Baixando {item_label} {idx}/{planned} de r/{subreddit}...")
                            try:
                                if current_media_type == "videos":
                                    source_path = await self._download_video_file(
                                        client=client,
                                        subreddit=subreddit,
                                        post=post,
                                    )
                                    await asyncio.to_thread(
                                        self._ingest.process_source_now,
                                        str(source_path),
                                        None,
                                        True,
                                        True,
                                    )
                                else:
                                    source_path = await self._download_image_file(
                                        client=client,
                                        subreddit=subreddit,
                                        post=post,
                                    )
                                    await asyncio.to_thread(
                                        lambda: self._image_ingest.process_source_now(
                                            str(source_path),
                                            post_id=str(post.get("post_id") or "").strip() or None,
                                            post_url=str(post.get("post_url") or "").strip() or None,
                                            title=str(post.get("title") or "").strip() or None,
                                        )
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
                                self._append_log(f"[{subreddit}] Erro no {item_label} {idx}/{planned}: {exc}")

                        if skipped_due_to_blacklist:
                            if is_last_pass:
                                self._increment("completed_subreddits", 1)
                            continue

                        if completed > 0 or skipped_due_to_history > 0:
                            downloaded_to_mark.add(self.normalize_subreddit_name(subreddit).lower())
                            self._update_item(subreddit, marked_downloaded=True)
                            subreddit_had_success = True

                        if is_last_pass:
                            if failed > 0 and not subreddit_had_success and subreddit_failed:
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
                if media_type == "images":
                    await asyncio.to_thread(self._merge_downloaded_image_subreddits, downloaded_to_mark)
                else:
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
